"""Resolve inclusive restaurant accounting dates before making an iiko request."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import cached_property
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal.windows_tz import win_tz

from .cloud_sales import _windows
from .service import MAX_RANGE_DAYS, _parse_date


def parse_zone(name: str) -> tzinfo:
    name = name.strip()
    if name == "Z":
        return timezone.utc
    # Terminal configurations may use a fixed UTC offset, a Windows name or an IANA name.
    offset = re.fullmatch(r"(?:UTC|GMT)?([+-]?)(\d{1,2}):([0-5]\d)(?::00)?", name, re.IGNORECASE)
    if offset:
        minutes = int(offset[2]) * 60 + int(offset[3])
        if minutes <= 14 * 60:
            return timezone(timedelta(minutes=-minutes if offset[1] == "-" else minutes))
    try:
        return ZoneInfo(win_tz.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown restaurant time zone. Use an IANA name or a UTC offset such as +03:00.") from None


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def filter(self) -> dict:
        return {"periodType": "CUSTOM", "from": self.start.isoformat(), "to": self.end.isoformat(),
                "includeLow": True, "includeHigh": True}


def windows(first: str, last: str) -> list[Window]:
    start, end = _parse_date(first, "date_from"), _parse_date(last, "date_to")
    if not 1 <= (end - start).days + 1 <= MAX_RANGE_DAYS:
        raise ValueError(f"Request an ordered period of up to {MAX_RANGE_DAYS} days.")
    return [Window(a, b) for a, b in _windows(start, end)]


# Preset names are part of the MCP interface. A bare "week" or "month" is ambiguous.
ALIASES = {
    "today": ("сегодня",), "yesterday": ("вчера", "фчера", "вчерашний день"), "day_before_yesterday": ("позавчера", "позафчера"),
    "this_week": ("эта неделя", "эту неделю", "текущая неделя", "текущую неделю", "week to date"),
    "last_week": ("прошлая неделя", "прошлую неделю", "предыдущая неделя", "предыдущую неделю", "previous week"),
    "week_before_last": ("позапрошлая неделя", "позапрошлую неделю"),
    "previous_week_to_date": ("прошлая неделя по тот же день", "last week to date"),
    "month_before_last": ("позапрошлый месяц",),
    "this_month": ("этот месяц", "текущий месяц", "month to date"),
    "last_month": ("прошлый месяц", "предыдущий месяц", "previous month"),
    "previous_month_to_date": ("прошлый месяц по то же число", "last month to date"),
    "this_quarter": ("этот квартал", "текущий квартал", "quarter to date"),
    "last_quarter": ("прошлый квартал", "предыдущий квартал", "previous quarter"),
    "this_year": ("этот год", "текущий год", "year to date"),
    "last_year": ("прошлый год", "предыдущий год", "previous year"),
    "last_7_days": ("последние 7 дней",), "last_30_days": ("последние 30 дней",),
    "last_7_complete_days": ("последние 7 полных дней",),
    "last_30_complete_days": ("последние 30 полных дней",),
}
PRESETS = {alias.replace("_", " "): key for key, aliases in ALIASES.items() for alias in (key, *aliases)}
DAY_OFFSETS = {"today": 0, "yesterday": 1, "day_before_yesterday": 2}


def preset(value: str) -> str | None:
    key = " ".join(value.casefold().replace("_", " ").split())
    key = key.removeprefix("за ")
    if key in PRESETS:
        return PRESETS[key]
    match = re.fullmatch(r"last ([1-9][0-9]{0,2}) (complete )?days?", key)
    if not match:
        match = re.fullmatch(r"последни(?:е|х|й) ([1-9][0-9]{0,2}) (полн(?:ых|ый|ых календарных) )?(?:день|дня|дней)", key)
    if match:
        return f"last_{match[1]}_{'complete_' if match[2] else ''}days"
    return None


def explicit_day(value: str) -> date:
    value = value.strip()
    if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
        day, month, year = value.split(".")
        value = f"{year}-{month}-{day}"
    if value.casefold().strip() in {"неделя", "за неделю", "месяц", "за месяц", "week", "month"}:
        raise ValueError("Clarify the period: previous calendar week/month, current period to date, or rolling 7/30 days.")
    return _parse_date(value, "report date")


def bounds(key: str, today: date) -> tuple[date, date]:
    if key in DAY_OFFSETS:
        day = today - timedelta(days=DAY_OFFSETS[key])
        return day, day
    if key.startswith("last_") and key.endswith("_days"):
        end = today - timedelta(days=int(key.endswith("_complete_days")))
        return end - timedelta(days=int(key.split("_")[1]) - 1), end
    if key in ("last_week", "this_week", "week_before_last", "previous_week_to_date"):
        monday = today - timedelta(days=today.weekday())
        if key == "this_week":
            return monday, today
        if key == "previous_week_to_date":
            return monday - timedelta(days=7), today - timedelta(days=7)
        if key == "week_before_last":
            monday -= timedelta(days=7)
        return monday - timedelta(days=7), monday - timedelta(days=1)
    start = today.replace(day=1)
    if key.endswith("_quarter"):
        start = start.replace(month=1 + 3 * ((today.month - 1) // 3))
    elif key.endswith("_year"):
        start = start.replace(month=1)
    if key.startswith("this_"):
        return start, today
    end = start - timedelta(days=1)
    if key == "last_quarter":
        return end.replace(month=end.month - 2, day=1), end
    if key == "last_year":
        return end.replace(month=1, day=1), end
    if key == "month_before_last":
        end = end.replace(day=1) - timedelta(days=1)
    if key == "previous_month_to_date":
        end = end.replace(day=min(today.day, end.day))
    return end.replace(day=1), end


@dataclass(frozen=True)
class ResolvedPeriod:
    windows: list[Window]
    label: str
    zone: tzinfo | None
    includes_today: bool


class RestaurantCalendar:
    def __init__(self, timezones: Callable[[], Iterable[str]], timezone_name: str | None, reference_time: datetime):
        self.timezones = timezones
        self.timezone_name = timezone_name
        self.reference_time = reference_time

    @cached_property
    def zone(self) -> tzinfo:
        if self.timezone_name is not None:
            return parse_zone(self.timezone_name)
        try:
            zones = {parse_zone(name) for name in self.timezones()}
        except ValueError as exc:
            raise ValueError(f"{exc} Supply restaurant_timezone for this outlet or give explicit dates.") from None
        if len(zones) != 1:
            raise ValueError("No single restaurant time zone found. Supply restaurant_timezone for this outlet or give explicit dates.")
        return zones.pop()

    def single_day(self, value: str) -> date:
        key = preset(value)
        if key in DAY_OFFSETS:
            return bounds(key, self.reference_time.astimezone(self.zone).date())[0]
        if key:
            raise ValueError("This report requires a single accounting date.")
        return explicit_day(value)

    def timestamp(self, value: str, *, local_date_needed: bool = False) -> str:
        key = value.strip().casefold()
        if key in ("now", "сейчас"):
            return (self.reference_time.astimezone(self.zone) if local_date_needed else self.reference_time).isoformat()
        if key in ("end_of_yesterday", "на конец вчерашнего дня"):
            yesterday = self.single_day("yesterday")
            # One millisecond before the following local midnight, including offset changes.
            tomorrow = datetime.combine(yesterday + timedelta(days=1), time.min, self.zone)
            return (tomorrow.astimezone(timezone.utc) - timedelta(milliseconds=1)).astimezone(self.zone).isoformat(timespec="milliseconds")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is None or "T" not in value or parsed.utcoffset() is None:
            raise ValueError("Specify now, end_of_yesterday or an ISO timestamp with a UTC offset.")
        if local_date_needed and (self.timezone_name is not None or parsed.utcoffset() == timedelta(0)):
            return parsed.astimezone(self.zone).isoformat()
        return value

    def resolve(self, first: str, last: str) -> ResolvedPeriod:
        left, right = preset(first), preset(last)
        if not left and not right:
            start, end = explicit_day(first), explicit_day(last)
            # Explicit dates never require permission to read terminal groups.
            zone = self.zone if self.timezone_name is not None else None
            today = self.reference_time.astimezone(zone).date() if zone else None
            label = "CUSTOM"
        else:
            if left != right and any(key and key not in DAY_OFFSETS for key in (left, right)):
                raise ValueError("Pass the same named period as both bounds, or use individual dates, today or yesterday.")
            zone = self.zone
            today = self.reference_time.astimezone(zone).date()
            start = bounds(left, today)[0] if left else explicit_day(first)
            end = bounds(right, today)[1] if right else explicit_day(last)
            label = left.upper() if left == right else "CUSTOM"
        spans = windows(start.isoformat(), end.isoformat())
        return ResolvedPeriod(spans, label, zone, today is not None and start <= today <= end)
