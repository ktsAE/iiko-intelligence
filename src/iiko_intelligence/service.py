from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .database import ReportReader


MAX_RANGE_DAYS = 366
MAX_ROWS = 100


def _money(minor: int) -> str:
    return str((Decimal(minor) / Decimal(100)).quantize(Decimal("0.00")))


def _quantity(milli: int) -> str:
    return str((Decimal(milli) / Decimal(1000)).quantize(Decimal("0.001")))


def _parse_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid YYYY-MM-DD date") from exc


def _validated_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_ROWS}")
    return limit


def dish_sort(rank_by: str, *, sales: str) -> str:
    if rank_by == "quantity":
        return "quantity"
    if rank_by == "sales":
        return sales
    raise ValueError("rank_by must be sales or quantity")


class AnalyticsService(ReportReader):
    dish_sales_sql = """
        SELECT c.business_date AS day, p.id AS product_id, p.name, l.net_minor AS amount
        FROM checks c JOIN sales_lines l ON l.check_id = c.id JOIN products p ON p.id = l.product_id
    """

    def _metadata(self, connection: Any) -> dict[str, str]:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def _period(self, connection: Any, date_from: str, date_to: str) -> tuple[date, date, dict[str, str]]:
        start = _parse_date(date_from, "date_from")
        end = _parse_date(date_to, "date_to")
        if end < start:
            raise ValueError("date_to must not be before date_from")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"date range must be no more than {MAX_RANGE_DAYS} days")
        metadata = self._metadata(connection)
        coverage_start = date.fromisoformat(metadata["coverage_start"])
        coverage_end = date.fromisoformat(metadata["coverage_end"])
        if start < coverage_start or end > coverage_end:
            raise ValueError(
                "requested period is outside dataset coverage "
                f"{coverage_start.isoformat()} to {coverage_end.isoformat()}"
            )
        return start, end, metadata

    @staticmethod
    def _envelope(metadata: dict[str, str], **content: Any) -> dict[str, Any]:
        return {
            "status": "complete",
            "dataset_mode": metadata["dataset_mode"],
            "dataset_id": metadata["dataset_id"],
            "restaurant": {
                "id": metadata["restaurant_id"],
                "name": metadata["restaurant_name"],
            },
            "currency": metadata["currency"],
            "timezone": metadata["timezone"],
            "source": {
                "provider": "synthetic",
                "schema_version": metadata["schema_version"],
                "generated_at": metadata["generated_at"],
            },
            "warning": "Synthetic trial data. It is not connected to iiko.",
            **content,
        }

    def capabilities(self) -> dict[str, Any]:
        with self._read() as connection:
            metadata = self._metadata(connection)
        return self._envelope(
            metadata,
            coverage={"from": metadata["coverage_start"], "to": metadata["coverage_end"]},
            tools={
                "sales_summary": "available",
                "top_dishes": "available",
                "compare_sales_periods": "available",
                "purchases": "available",
                "stock_balances": "available_at_coverage_end",
                "writeoffs": "available",
                "live_iiko": "not_configured",
            },
            semantics={
                "sales": "gross - discounts - refunds, including synthetic tax",
                "average_check": "net sales divided by completed checks",
                "stock": "snapshot only; no live inventory movement ledger",
            },
        )

    def data_status(self) -> dict[str, Any]:
        with self._read() as connection:
            metadata = self._metadata(connection)
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("checks", "sales_lines", "purchases", "stock_balances", "writeoffs")
            }
            mismatch_count = connection.execute(
                """
                SELECT COUNT(*) FROM checks AS c
                LEFT JOIN (
                    SELECT check_id,
                           SUM(gross_minor) AS gross,
                           SUM(discount_minor) AS discounts,
                           SUM(refund_minor) AS refunds,
                           SUM(net_minor) AS net
                    FROM sales_lines GROUP BY check_id
                ) AS l
                  ON l.check_id = c.id
                WHERE l.check_id IS NULL
                   OR c.gross_minor != l.gross
                   OR c.discount_minor != l.discounts
                   OR c.refund_minor != l.refunds
                   OR c.net_minor != l.net
                """
            ).fetchone()[0]
        return self._envelope(
            metadata,
            coverage={"from": metadata["coverage_start"], "to": metadata["coverage_end"]},
            records=counts,
            reconciliation={"check_to_line_mismatches": mismatch_count, "passed": mismatch_count == 0},
            freshness="fixed_demo_snapshot",
        )

    def sales_summary(self, date_from: str, date_to: str) -> dict[str, Any]:
        with self._read() as connection:
            start, end, metadata = self._period(connection, date_from, date_to)
            totals = connection.execute(
                """
                SELECT COUNT(*) AS checks,
                       COALESCE(SUM(guests), 0) AS guests,
                       COALESCE(SUM(gross_minor), 0) AS gross,
                       COALESCE(SUM(discount_minor), 0) AS discounts,
                       COALESCE(SUM(refund_minor), 0) AS refunds,
                       COALESCE(SUM(net_minor), 0) AS net
                FROM checks WHERE business_date BETWEEN ? AND ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()
            channels = connection.execute(
                """
                SELECT channel, COUNT(*) AS checks, SUM(net_minor) AS net
                FROM checks WHERE business_date BETWEEN ? AND ?
                GROUP BY channel ORDER BY net DESC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            days = connection.execute(
                "SELECT business_date, SUM(net_minor) AS net FROM checks WHERE business_date BETWEEN ? AND ? "
                "GROUP BY business_date ORDER BY business_date", (start.isoformat(), end.isoformat()),
            ).fetchall()
        check_count = totals["checks"]
        average_minor = (
            Decimal(totals["net"]) / Decimal(check_count)
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP) if check_count else Decimal(0)
        return self._envelope(
            metadata,
            period={"from": start.isoformat(), "to": end.isoformat(), "date_basis": "business_date"},
            totals={
                "checks": check_count,
                "guests": totals["guests"],
                "gross_sales": _money(totals["gross"]),
                "discounts": _money(totals["discounts"]),
                "refunds": _money(totals["refunds"]),
                "net_sales": _money(totals["net"]),
                "average_check": _money(int(average_minor)),
            },
            by_channel=[
                {"channel": row["channel"], "checks": row["checks"], "net_sales": _money(row["net"])}
                for row in channels
            ],
            by_day=[{"date": row["business_date"], "net_sales": _money(row["net"])} for row in days],
        )

    def top_dishes(self, date_from: str, date_to: str, *, limit: int = 10, rank_by: str = "sales") -> dict[str, Any]:
        limit = _validated_limit(limit)
        sort_column = dish_sort(rank_by, sales="net")
        with self._read() as connection:
            start, end, metadata = self._period(connection, date_from, date_to)
            total_rows = connection.execute(
                """
                SELECT COUNT(DISTINCT l.product_id)
                FROM sales_lines l JOIN checks c ON c.id = l.check_id
                WHERE c.business_date BETWEEN ? AND ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT p.id, p.name, p.category,
                       SUM(l.quantity) AS quantity,
                       SUM(l.net_minor) AS net,
                       SUM(l.quantity * p.estimated_cost_minor) AS estimated_cost
                FROM sales_lines l
                JOIN checks c ON c.id = l.check_id
                JOIN products p ON p.id = l.product_id
                WHERE c.business_date BETWEEN ? AND ?
                GROUP BY p.id, p.name, p.category
                ORDER BY {sort_column} DESC, p.id
                LIMIT ?
                """,
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return self._envelope(
            metadata,
            period={"from": start.isoformat(), "to": end.isoformat()},
            ranked_by=rank_by,
            rows=[
                {
                    "product_id": row["id"],
                    "name": row["name"],
                    "category": row["category"],
                    "quantity": row["quantity"],
                    "net_sales": _money(row["net"]),
                    "estimated_recipe_cost": _money(row["estimated_cost"]),
                    "estimated_contribution": _money(row["net"] - row["estimated_cost"]),
                }
                for row in rows
            ],
            detail={"returned_rows": len(rows), "total_rows": total_rows, "truncated": total_rows > len(rows)},
            cost_note="Costs are synthetic product estimates, not actual inventory-based food cost.",
        )

    def purchases(self, date_from: str, date_to: str, *, limit: int = 20) -> dict[str, Any]:
        limit = _validated_limit(limit)
        with self._read() as connection:
            start, end, metadata = self._period(connection, date_from, date_to)
            total = connection.execute(
                "SELECT COUNT(*) FROM purchases WHERE business_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT * FROM purchases WHERE business_date BETWEEN ? AND ?
                ORDER BY business_date DESC, id LIMIT ?
                """,
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return self._envelope(
            metadata,
            period={"from": start.isoformat(), "to": end.isoformat()},
            rows=[
                {
                    "date": row["business_date"],
                    "supplier": row["supplier"],
                    "item": row["item_name"],
                    "quantity": _quantity(row["quantity_milli"]),
                    "unit": row["unit"],
                    "amount": _money(row["amount_minor"]),
                }
                for row in rows
            ],
            detail={"returned_rows": len(rows), "total_rows": total, "truncated": total > len(rows)},
        )

    def stock_balances(self, *, limit: int = 50, negative_only: bool = False) -> dict[str, Any]:
        limit = _validated_limit(limit)
        with self._read() as connection:
            metadata = self._metadata(connection)
            condition = "WHERE quantity_milli < 0" if negative_only else ""
            total = connection.execute(f"SELECT COUNT(*) FROM stock_balances {condition}").fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM stock_balances {condition} ORDER BY value_minor DESC, item_id LIMIT ?", (limit,)
            ).fetchall()
        return self._envelope(
            metadata,
            as_of=metadata["coverage_end"],
            negative_only=negative_only,
            rows=[
                {
                    "item_id": row["item_id"],
                    "item": row["item_name"],
                    "quantity": _quantity(row["quantity_milli"]),
                    "unit": row["unit"],
                    "value": _money(row["value_minor"]),
                }
                for row in rows
            ],
            detail={"returned_rows": len(rows), "total_rows": total, "truncated": total > len(rows)},
        )

    def writeoffs(self, date_from: str, date_to: str, *, limit: int = 20) -> dict[str, Any]:
        limit = _validated_limit(limit)
        with self._read() as connection:
            start, end, metadata = self._period(connection, date_from, date_to)
            total = connection.execute(
                "SELECT COUNT(*) FROM writeoffs WHERE business_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT * FROM writeoffs WHERE business_date BETWEEN ? AND ?
                ORDER BY value_minor DESC, id LIMIT ?
                """,
                (start.isoformat(), end.isoformat(), limit),
            ).fetchall()
        return self._envelope(
            metadata,
            period={"from": start.isoformat(), "to": end.isoformat()},
            rows=[
                {
                    "date": row["business_date"],
                    "item_id": row["item_id"],
                    "item": row["item_name"],
                    "quantity": _quantity(row["quantity_milli"]),
                    "unit": row["unit"],
                    "value": _money(row["value_minor"]),
                    "reason": row["reason"],
                }
                for row in rows
            ],
            detail={"returned_rows": len(rows), "total_rows": total, "truncated": total > len(rows)},
        )
