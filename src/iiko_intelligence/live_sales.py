"""Read sales directly from iiko SALES OLAP for the authenticated organization."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_HALF_UP
from functools import cached_property
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal.windows_tz import win_tz

from .cloud_reports import CloudReports
from .cloud_sales import (COLUMNS, DAY, DEPARTMENT, DISH_ID, DISH_NAME, ORDERS, QUANTITY, REPORT, SALES,
                          CloudHTTPError, _object_array, _windows, column_permissions, olap_request, require_columns)
from .sales_snapshot import MAX_SALES_ROWS, SALES_DEFINITION, SalesRow, number_units, report_date, report_text
from .service import MAX_RANGE_DAYS, _money, _parse_date, _quantity

METRIC = "sales_after_discounts"
RELATIVE = {"today": "today", "сегодня": "today", "yesterday": "yesterday", "вчера": "yesterday"}


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


@dataclass
class Period:
    windows: list[Window]
    daily: dict[date, int]
    orders: dict[date, int] | None
    requested_period: str
    zone: tzinfo | None

    def info(self) -> dict:
        first, last = self.windows[0].start, self.windows[-1].end
        result = {"from": first.isoformat(), "to": last.isoformat(), "period_type": "CUSTOM",
                  "days_with_rows": len(self.daily), "calendar_days": (last - first).days + 1}
        if self.zone is not None:
            result.update(requested_period=self.requested_period, restaurant_timezone=str(self.zone))
        return result


@dataclass
class Dish:
    name: str
    day: date
    sales: int = 0
    quantity: int = 0


class LiveSales:
    def __init__(self, api: CloudReports, department: str | None = None, restaurant_timezone: str | None = None):
        self.api = api
        self.timezone_name = restaurant_timezone
        self.reference_time = datetime.now(timezone.utc)
        self.department = None if department is None else report_text(department, "outlet name")
        try:
            self.columns = column_permissions(api.read(COLUMNS, {"reportType": "SALES"}))
        except CloudHTTPError as exc:
            if exc.status != 503:
                raise
            # The report endpoint independently authorizes its own request.
            self.columns = None
        self.with_orders = False
        if self.columns is not None:
            flags = {DAY: ("canGroup", "canFilter"), SALES: ("canAggregate",)}
            if self.department:
                flags[DEPARTMENT] = ("canGroup", "canFilter")
            require_columns(self.columns, flags)
            self.with_orders = self.columns.get(ORDERS, {}).get("canAggregate") is True

    @cached_property
    def restaurant_zone(self) -> tzinfo:
        if self.timezone_name is not None:
            return parse_zone(self.timezone_name)
        try:
            zones = {parse_zone(name) for name in self.api.terminal_timezones()}
        except ValueError as exc:
            raise ValueError(f"{exc} Supply restaurant_timezone for this outlet or give explicit dates.") from None
        if len(zones) != 1:
            raise ValueError("No single restaurant time zone found. Supply restaurant_timezone for this outlet or give explicit dates.")
        return zones.pop()

    def context(self) -> dict:
        result = {"dataset_mode": "live", "restaurant": self.api.restaurant, "department": self.department,
                  "currency": self.api.currency, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "date_field": DAY, "metric": METRIC, "metric_definition": SALES_DEFINITION}
        if self.columns is None:
            result["report_note"] = "iiko returned HTTP 503 for its field list. The direct SALES report omits optional order counts."
        return result

    def rows(self, window: Window, groups: list[str], measures: list[str]):
        request = olap_request(window.filter(), self.department, groups, measures)
        for row in _object_array(self.api.read(REPORT, request), "data", "sales report"):
            day = report_date(row.get(DAY))
            if not window.start <= day <= window.end or (self.department is not None and row.get(DEPARTMENT) != self.department):
                raise ValueError("iiko returned sales outside the requested dates or outlet.")
            yield day, row

    def period(self, first: str, last: str) -> Period:
        requested = RELATIVE.get(first.lower())
        last_relative = RELATIVE.get(last.lower())
        zone = None
        if requested is not None or last_relative is not None:
            if requested != last_relative:
                raise ValueError("Use the same relative day for both bounds or give two explicit dates.")
            zone = self.restaurant_zone
            day = self.reference_time.astimezone(zone).date()
            if requested == "yesterday":
                day -= timedelta(days=1)
            spans = [Window(day, day)]
        else:
            requested = "custom"
            spans = windows(first, last)
        daily = {}
        orders = {} if self.with_orders else None
        for span in spans:
            for day, row in self.rows(span, [], [ORDERS, SALES] if self.with_orders else [SALES]):
                if day in daily:
                    raise ValueError("iiko returned unexpected extra daily totals.")
                daily[day] = number_units(row.get(SALES), 2, "daily sales")
                if orders is not None:
                    orders[day] = number_units(row.get(ORDERS), 0, "order count")
                    if orders[day] < 0:
                        raise ValueError("iiko returned a negative order count.")
        return Period(spans, daily, orders, requested.upper(), zone)

    def summary(self, first: str, last: str) -> dict:
        period = self.period(first, last)
        totals = {METRIC: _money(sum(period.daily.values())) if period.daily else None}
        daily = [{"date": day.isoformat(), METRIC: _money(period.daily[day])} for day in sorted(period.daily)]
        if period.orders is not None and period.daily:
            count = sum(period.orders.values())
            totals["orders"] = count
            totals["average_order"] = (str((Decimal(totals[METRIC]) / count).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)) if count else None)
            for row in daily:
                row["orders"] = period.orders[date.fromisoformat(row["date"])]
        return {**self.context(), "status": "ok" if daily else "no_data",
                "period": period.info(), "totals": totals, "by_day": daily}

    def dishes(self, period: Period) -> dict[str, Dish]:
        if self.columns is not None:
            require_columns(self.columns, {DISH_ID: ("canGroup",), DISH_NAME: ("canGroup",), QUANTITY: ("canAggregate",)})
        totals = defaultdict(int)
        seen = set()
        dishes = {}
        for span in period.windows:
            for day, row in self.rows(span, [DISH_ID, DISH_NAME], [QUANTITY, SALES]):
                sale = SalesRow.from_values(day, row.get(DISH_ID), row.get(DISH_NAME), row.get(QUANTITY), row.get(SALES))
                key = (day, sale.product_id, sale.name)
                if key in seen or len(seen) >= MAX_SALES_ROWS:
                    raise ValueError("Duplicate or excessive iiko dish detail. Try a shorter period.")
                seen.add(key)
                totals[day] += sale.sales_minor
                dish = dishes.setdefault(sale.product_id, Dish(sale.name, day))
                if (day, sale.name) > (dish.day, dish.name):
                    dish.day, dish.name = day, sale.name
                dish.sales += sale.sales_minor
                dish.quantity += sale.quantity_milli
        if totals != period.daily:
            raise ValueError("Dish sales differ from the daily iiko totals. Retry the report.")
        return dishes

    def top(self, first: str, last: str, rank_by: str, limit: int) -> dict:
        period = self.period(first, last)
        dishes = self.dishes(period)
        ranked = sorted(dishes.items(), key=lambda pair: (
            -(pair[1].quantity if rank_by == "quantity" else pair[1].sales), pair[0]))
        return {**self.context(), "status": "ok" if period.daily else "no_data", "period": period.info(),
                "rank_by": rank_by, "detail": {"total_rows": len(dishes), "limit": limit},
                "rows": [{"product_id": key, "name": dish.name, "quantity": _quantity(dish.quantity),
                          METRIC: _money(dish.sales)} for key, dish in ranked[:limit]]}

    def compare(self, a: str, b: str, c: str, d: str, limit: int) -> dict:
        before, after = self.period(a, b), self.period(c, d)
        base, current = sum(before.daily.values()), sum(after.daily.values())
        periods = [p.info() for p in (before, after)]
        overlap = max(0, (min(before.windows[-1].end, after.windows[-1].end) -
                          max(before.windows[0].start, after.windows[0].start)).days + 1)
        result = {**self.context(), "baseline": {**periods[0], "sales": _money(base) if before.daily else None},
                  "current": {**periods[1], "sales": _money(current) if after.daily else None}, "overlap_days": overlap}
        if not before.daily or not after.daily:
            return {**result, "status": "insufficient_data", "change": None, "change_percent": None}
        delta = current - base
        result.update(change=_money(delta), change_percent=str((Decimal(100) * delta / base).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)) if base > 0 else None)
        try:
            previous, latest = self.dishes(before), self.dishes(after)
        except ValueError as exc:
            return {**result, "status": "partial", "dish_detail_reason": str(exc)}
        changes = []
        for key in previous.keys() | latest.keys():
            left, right = previous.get(key), latest.get(key)
            old, new = left.sales if left else 0, right.sales if right else 0
            if old != new:
                changes.append((key, (right or left).name, old, new))
        changes.sort(key=lambda row: (-abs(row[3] - row[2]), row[0]))
        shown = changes[:limit]
        return {**result, "status": "ok", "dish_changes": [
            {"product_id": key, "name": name, "baseline": _money(old), "current": _money(new), "change": _money(new - old)}
            for key, name, old, new in shown], "other_dishes_change": _money(delta - sum(n - o for _, _, o, n in shown))}
