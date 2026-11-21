from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from .database import ReportReader, open_readonly, write_lock
from .document_store import copy_documents, document_report, document_status
from .service import (
    MAX_RANGE_DAYS,
    AnalyticsService,
    _money,
    _parse_date,
    _quantity,
    _validated_limit,
    dish_sort,
)

MAX_SALES_ROWS = 100_000
MODE = "iiko_sales_snapshot"
SALES_DEFINITION = (
    "Sales after discounts as reported by iiko. Refund and tax treatment follows the source report."
)
RUBLE_SUFFIX = re.compile(r"\s*(?:₽|RUB|руб\.?|р\.?)\s*$", re.IGNORECASE)
STOCK_SCHEMA = """CREATE TABLE IF NOT EXISTS stock_snapshot (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL, value_minor INTEGER NOT NULL
) STRICT"""


def number_units(value: object, places: int, label: str) -> int:
    raw = str(value).strip().replace("\u00a0", " ").replace("\u202f", " ")
    if " " in raw:
        if not re.fullmatch(r"[+-]?\d{1,3}(?: \d{3})+(?:[.,]\d{1,12})?", raw):
            raise ValueError(f"{label} has invalid thousands grouping")
        raw = raw.replace(" ", "")
    if not re.fullmatch(r"[+-]?\d{1,12}(?:[.,]\d{1,12})?", raw):
        raise ValueError(f"{label} must be a number with spaces as the only supported thousands separator")
    try:
        amount = Decimal(raw.replace(",", ".")) * 10**places
    except InvalidOperation:
        raise ValueError(f"{label} is not a supported number") from None
    if amount != amount.to_integral_value():
        raise ValueError(f"{label} has more than {places} decimal places")
    if abs(amount) > 10**13:
        raise ValueError(f"{label} exceeds the supported amount")
    return int(amount)


def strip_ruble_suffix(value: object, currency: str) -> object:
    if not isinstance(value, str) or not RUBLE_SUFFIX.search(value):
        return value
    if currency != "RUB":
        raise ValueError("the report has a ruble amount but the selected currency is not RUB")
    return RUBLE_SUFFIX.sub("", value)


def report_date(value: object) -> date:
    if isinstance(value, datetime):
        if value.time() != datetime.min.time():
            raise ValueError("expected an accounting date without a time")
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    try:
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", raw):
            return datetime.strptime(raw, "%d.%m.%Y").date()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T00:00:00(?:\.0+)?)?", raw):
            return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    raise ValueError("business date must be a calendar date (YYYY-MM-DD or DD.MM.YYYY)")


def report_text(value: object, label: str, *, limit: int = 160) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    if not text or len(text) > limit or not text.isprintable():
        raise ValueError(f"{label} must be printable text of 1 to {limit} characters")
    return text


@dataclass(frozen=True)
class SalesRow:
    day: date
    product_id: str
    name: str
    quantity_milli: int
    sales_minor: int

    @classmethod
    def from_values(cls, day: object, code: object, name: object, qty: object, sales: object) -> SalesRow:
        if type(code) is int:
            code = str(code)
        return cls(
            report_date(day), report_text(code, "product code", limit=80), report_text(name, "dish name"),
            number_units(qty, 3, "quantity"), number_units(sales, 2, "sales amount"),
        )


@dataclass(frozen=True)
class SalesPeriod:
    start: date
    end: date
    expected_minor: int
    restaurant: str
    currency: str

    def __post_init__(self) -> None:
        if not 1 <= (self.end - self.start).days + 1 <= MAX_RANGE_DAYS:
            raise ValueError(f"coverage must be between 1 and {MAX_RANGE_DAYS} days")
        report_text(self.restaurant, "restaurant name")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("currency must be a three-letter code, such as RUB")


def save_snapshot(
    target: Path, period: SalesPeriod, rows: list[SalesRow], *, provider: str,
    daily_orders: dict[date, int] | None = None, cloud_scope: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if not rows or len(rows) > MAX_SALES_ROWS:
        raise ValueError(f"expected between 1 and {MAX_SALES_ROWS:,} sales rows")
    seen: set[tuple[date, str]] = set()
    names: dict[str, str] = {}
    for row in rows:
        if not period.start <= row.day <= period.end:
            raise ValueError("report includes rows outside the supplied coverage")
        key = row.day, row.product_id
        if key in seen:
            raise ValueError("duplicate day and product code; export a single row per product and accounting day")
        seen.add(key)
        if names.setdefault(row.product_id, row.name) != row.name:
            raise ValueError("a product code has more than one name; use a single consistent name within the period")

    actual = sum(row.sales_minor for row in rows)
    if daily_orders is not None and (set(daily_orders) != {row.day for row in rows}
            or any(type(count) is not int or count < 0 for count in daily_orders.values())):
        raise ValueError("order counts must cover exactly the reported days and be nonnegative integers")
    if actual != period.expected_minor:
        raise ValueError(f"sales total {_money(actual)} does not match iiko total {_money(period.expected_minor)}")

    dataset_id = f"sales-{uuid4().hex[:12]}"
    metadata = {
        "dataset_mode": MODE, "dataset_id": dataset_id, "restaurant_name": period.restaurant,
        "currency": period.currency, "coverage_start": period.start.isoformat(),
        "coverage_end": period.end.isoformat(), "provider": provider, "schema_version": "sales-v1",
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reconciled_sales": _money(actual), "daily_orders": "yes" if daily_orders is not None else "no",
    }
    if cloud_scope:
        metadata.update(cloud_organization=cloud_scope[0], cloud_outlet=cloud_scope[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".sales-", suffix=".sqlite", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
                CREATE TABLE sales (
                    day TEXT NOT NULL, product_id TEXT NOT NULL, name TEXT NOT NULL,
                    quantity_milli INTEGER NOT NULL, sales_minor INTEGER NOT NULL,
                    PRIMARY KEY (day, product_id)
                ) STRICT;
                CREATE TABLE daily_orders (day TEXT PRIMARY KEY, orders INTEGER NOT NULL) STRICT;
            """)
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
            connection.executemany(
                "INSERT INTO sales VALUES (?, ?, ?, ?, ?)",
                ((r.day.isoformat(), r.product_id, r.name, r.quantity_milli, r.sales_minor) for r in rows),
            )
            if daily_orders is not None:
                connection.executemany(
                    "INSERT INTO daily_orders VALUES (?, ?)",
                    ((day.isoformat(), orders) for day, orders in daily_orders.items()),
                )
        status = SalesSnapshotService(temporary).data_status()
        if not status["reconciliation"]["passed"]:
            raise ValueError("the saved sales rows failed reconciliation")
        with write_lock(target):
            if target.exists():
                with open_readonly(target) as previous:
                    previous_meta = dict(previous.execute("SELECT key, value FROM metadata"))
                    same_cloud_outlet = (not cloud_scope or not previous_meta.get("cloud_organization") or
                                         cloud_scope == (previous_meta["cloud_organization"], previous_meta["cloud_outlet"]))
                    if (previous_meta.get("dataset_mode") == MODE and previous_meta["restaurant_name"] == period.restaurant
                            and previous_meta["currency"] == period.currency and same_cloud_outlet):
                        with closing(sqlite3.connect(temporary)) as new, new:
                            copy_documents(previous, new)
                            if previous_meta.get("stock_as_of"):
                                new.execute(STOCK_SCHEMA)
                                new.executemany("INSERT INTO stock_snapshot VALUES (?, ?, ?, ?, ?)",
                                                previous.execute("SELECT code,name,unit,quantity_milli,value_minor FROM stock_snapshot"))
                                new.executemany("INSERT INTO metadata VALUES (?, ?)",
                                                [(k, v) for k, v in previous_meta.items() if k.startswith("stock_")])
                status = SalesSnapshotService(temporary).data_status()
                if not status.get("stock_snapshot", {}).get("passed", True):
                    raise ValueError("the retained warehouse balance failed reconciliation")
                if not all(s["passed"] for s in status["document_sources"].values()):
                    raise ValueError("the retained document list failed reconciliation")
            os.replace(temporary, target)
    except PermissionError:
        raise ValueError("could not save the dataset; finish any open report query and retry") from None
    finally:
        temporary.unlink(missing_ok=True)
    return status


class SalesSnapshotService(ReportReader):
    dish_sales_sql = "SELECT day, product_id, name, sales_minor AS amount FROM sales"

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
        return dict(connection.execute("SELECT key, value FROM metadata"))

    @staticmethod
    def _period(metadata: dict[str, str], date_from: str, date_to: str) -> tuple[str, str]:
        start = _parse_date(date_from, "date_from")
        end = _parse_date(date_to, "date_to")
        first, last = metadata["coverage_start"], metadata["coverage_end"]
        if end < start:
            raise ValueError("date_to must not be before date_from")
        if not first <= start.isoformat() <= end.isoformat() <= last:
            raise ValueError(f"period outside snapshot coverage ({first} to {last}); import a report for those dates")
        return start.isoformat(), end.isoformat()

    @staticmethod
    def _result(metadata: dict[str, str], **content: Any) -> dict[str, Any]:
        return {
            "status": "complete", "dataset_mode": MODE, "dataset_id": metadata["dataset_id"],
            "restaurant": {"name": metadata["restaurant_name"]}, "currency": metadata["currency"],
            "coverage": {"from": metadata["coverage_start"], "to": metadata["coverage_end"]},
            "source": {"provider": metadata["provider"], "imported_at": metadata["imported_at"],
                       **({"outlet": metadata["cloud_outlet"]} if metadata.get("cloud_outlet") else {}),
                       "freshness": "snapshot", "metric": SALES_DEFINITION},
            **content,
        }

    def capabilities(self) -> dict[str, Any]:
        with self._read() as connection:
            metadata = self._metadata(connection)
            documents = document_status(connection)
        return self._result(metadata, tools={
            "sales_summary": "available", "top_dishes": "available", "compare_sales_periods": "available",
            "order_count": "available" if metadata["daily_orders"] == "yes" else "unavailable",
            "stock_balances": "available" if "stock_as_of" in metadata else "unavailable",
            **{kind: "available" if kind in documents else "unavailable" for kind in ("purchases", "writeoffs")},
        })

    def data_status(self) -> dict[str, Any]:
        with self._read() as connection:
            metadata = self._metadata(connection)
            count, total = connection.execute("SELECT COUNT(*), SUM(sales_minor) FROM sales").fetchone()
            stock = {}
            documents = document_status(connection)
            if "stock_as_of" in metadata:
                items, value = connection.execute("SELECT COUNT(*), SUM(value_minor) FROM stock_snapshot").fetchone()
                stock = {"stock_snapshot": {"as_of": metadata["stock_as_of"], "warehouse": metadata["stock_warehouse"],
                         "items": items, "value": _money(value), "iiko_total": metadata["stock_total"],
                         "passed": _money(value) == metadata["stock_total"], "imported_at": metadata["stock_imported_at"]}}
        actual = _money(total)
        return self._result(metadata, **stock, document_sources=documents, records={"dish_day_rows": count}, reconciliation={
            "snapshot_sales_after_discounts": actual, "iiko_report_total": metadata["reconciled_sales"],
            "passed": actual == metadata["reconciled_sales"],
        })

    def sales_summary(self, date_from: str, date_to: str) -> dict[str, Any]:
        with self._read() as connection:
            metadata = self._metadata(connection)
            bounds = self._period(metadata, date_from, date_to)
            days = connection.execute("""
                SELECT day, SUM(sales_minor) AS sales FROM sales
                WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day
            """, bounds).fetchall()
            total = sum(row["sales"] for row in days)
            totals: dict[str, Any] = {"sales_after_discounts": _money(total)}
            if metadata["daily_orders"] == "yes":
                orders = connection.execute(
                    "SELECT COALESCE(SUM(orders), 0) FROM daily_orders WHERE day BETWEEN ? AND ?", bounds,
                ).fetchone()[0]
                average = (Decimal(total) / orders).quantize(Decimal("1"), rounding=ROUND_HALF_UP) if orders else None
                totals.update(orders=orders, average_sale_per_order=_money(int(average)) if average is not None else None)
        return self._result(metadata, period={"from": bounds[0], "to": bounds[1], "date_basis": "iiko_accounting_day"},
                            totals=totals, days_with_rows=len(days),
                            days_without_rows=(date.fromisoformat(bounds[1]) - date.fromisoformat(bounds[0])).days + 1 - len(days),
                            by_day=[{"date": row["day"], "sales_after_discounts": _money(row["sales"])}
                                                  for row in days])

    def top_dishes(self, date_from: str, date_to: str, *, limit: int = 10, rank_by: str = "sales") -> dict[str, Any]:
        limit = _validated_limit(limit)
        sort_column = dish_sort(rank_by, sales="sales")
        with self._read() as connection:
            metadata = self._metadata(connection)
            bounds = self._period(metadata, date_from, date_to)
            total = connection.execute(
                "SELECT COUNT(DISTINCT product_id) FROM sales WHERE day BETWEEN ? AND ?", bounds,
            ).fetchone()[0]
            rows = connection.execute(f"""
                SELECT product_id, name, SUM(quantity_milli) AS quantity, SUM(sales_minor) AS sales
                FROM sales WHERE day BETWEEN ? AND ? GROUP BY product_id, name
                ORDER BY {sort_column} DESC, product_id LIMIT ?
            """, (*bounds, limit)).fetchall()
        return self._result(metadata, period={"from": bounds[0], "to": bounds[1]}, ranked_by=rank_by, rows=[{
            "product_id": row["product_id"], "name": row["name"], "quantity": _quantity(row["quantity"]),
            "sales_after_discounts": _money(row["sales"]),
        } for row in rows], detail={"returned_rows": len(rows), "total_rows": total, "truncated": total > len(rows)})

    def _documents(self, kind: str, start: str, end: str, limit: int) -> dict[str, Any]:
        with self._read() as connection:
            metadata = self._metadata(connection)
            result = document_report(connection, kind, start, end, limit)
        return {"dataset_mode": MODE, "restaurant": {"name": metadata["restaurant_name"]},
                "currency": metadata["currency"], **result}

    def purchases(self, date_from: str, date_to: str, *, limit: int = 20) -> dict[str, Any]:
        return self._documents("purchases", date_from, date_to, limit)

    def stock_balances(self, *, limit: int = 50, negative_only: bool = False) -> dict[str, Any]:
        limit = _validated_limit(limit)
        with self._read() as connection:
            metadata = self._metadata(connection)
            if "stock_as_of" not in metadata:
                return self._result(metadata, status="unavailable", capability="stock_balances",
                                    reason="Import one warehouse balance report to see stock.")
            condition = "WHERE quantity_milli < 0" if negative_only else ""
            rows = connection.execute(f"SELECT * FROM stock_snapshot {condition} ORDER BY value_minor DESC, code LIMIT ?", (limit,)).fetchall()
            count = connection.execute(f"SELECT COUNT(*) FROM stock_snapshot {condition}").fetchone()[0]
            value = connection.execute("SELECT SUM(value_minor) FROM stock_snapshot").fetchone()[0]
        return {"status": "complete", "dataset_mode": MODE, "dataset_id": metadata["stock_id"],
                "restaurant": {"name": metadata["restaurant_name"]}, "currency": metadata["currency"],
                "warehouse": metadata["stock_warehouse"], "as_of": metadata["stock_as_of"],
                "source": {"provider": "iikoOffice_stock_export", "imported_at": metadata["stock_imported_at"]},
                "negative_only": negative_only, "total_value": _money(value), "total_value_scope": "whole_warehouse",
                "reconciliation": {"iiko_total": metadata["stock_total"],
                                                                 "passed": _money(value) == metadata["stock_total"]},
                "rows": [{"item_id": r["code"], "item_name": r["name"], "unit": r["unit"],
                          "quantity": _quantity(r["quantity_milli"]), "value": _money(r["value_minor"])} for r in rows],
                "detail": {"returned_rows": len(rows), "total_rows": count, "truncated": count > len(rows)}}

    def writeoffs(self, date_from: str, date_to: str, *, limit: int = 20) -> dict[str, Any]:
        return self._documents("writeoffs", date_from, date_to, limit)


@contextmanager
def open_reports(database: Path) -> Iterator[AnalyticsService | SalesSnapshotService]:
    with open_readonly(database) as connection:
        connection.execute("BEGIN")
        mode = connection.execute("SELECT value FROM metadata WHERE key = 'dataset_mode'").fetchone()
        if mode and mode[0] == MODE:
            yield SalesSnapshotService(database, connection)
        elif mode and mode[0] == "synthetic":
            yield AnalyticsService(database, connection)
        else:
            raise ValueError("the selected dataset is not an iiko sales snapshot or a demo dataset")


def reporting_service(database: Path) -> AnalyticsService | SalesSnapshotService:
    with open_reports(database) as service:
        return type(service)(database)
