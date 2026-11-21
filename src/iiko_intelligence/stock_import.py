from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .database import write_lock
from .office_export import (
    MAX_COLUMNS,
    MAX_TITLE_ROWS,
    TOTAL_LABELS,
    _columns,
    _heading,
    export_rows,
)
from .sales_snapshot import (
    MAX_SALES_ROWS,
    MODE,
    STOCK_SCHEMA,
    SalesSnapshotService,
    number_units,
    report_text,
    strip_ruble_suffix,
)
from .service import _money

FIELDS = (
    ("item_name", "наименование"), ("item_code", "артикул", "код товара"), ("unit", "ед. изм.", "ед. измерения", "ед. изм"),
    ("quantity", "количество"), ("stock_value", "себестоимость", "стоимость остатка"),
)


def import_stock(database: Path, source: Path, *, restaurant: str, currency: str, warehouse: str,
                 as_of: date, expected_minor: int, sheet: str | None = None) -> dict:
    warehouse = report_text(warehouse, "warehouse")
    rows = []
    seen = set()
    columns = ()
    file_total = None
    with export_rows(source, sheet, FIELDS) as table:
        for i, cells in enumerate(table, 1):
            if i > MAX_SALES_ROWS + MAX_TITLE_ROWS + 1 or any(v not in ("", None) for v in cells[MAX_COLUMNS:]):
                raise ValueError("warehouse report exceeds supported dimensions")
            if not columns:
                if i > MAX_TITLE_ROWS:
                    break
                columns = _columns(cells, FIELDS)
                continue
            values = [cells[j] if j < len(cells) else None for j in columns]
            if all(v in (None, "") for v in values):
                continue
            name, code, unit, quantity, value = values
            try:
                if code in (None, "") and _heading(name) in TOTAL_LABELS:
                    if file_total is not None:
                        raise ValueError("expected at most one grand total")
                    file_total = number_units(strip_ruble_suffix(value, currency), 2, "stock grand total")
                    continue
                if any(isinstance(v, str) and v.startswith("=") for v in values):
                    raise ValueError("export value-only stock cells")
                code = report_text(str(code) if type(code) is int else code, "item code", limit=80)
                if code in seen:
                    raise ValueError("duplicate item code; export one row per item for one warehouse")
                seen.add(code)
                rows.append((code, report_text(name, "item name"), report_text(unit, "unit", limit=30),
                             number_units(quantity, 3, "quantity"), number_units(strip_ruble_suffix(value, currency), 2, "value")))
            except ValueError as exc:
                raise ValueError(f"stock report row {i}: {exc}") from None
    if not columns or not rows or len(rows) > MAX_SALES_ROWS:
        raise ValueError("expected a flat warehouse report with item_name, item_code, unit, quantity and stock_value")
    total = sum(row[4] for row in rows)
    if total != expected_minor or file_total not in (None, total):
        raise ValueError(f"stock value {_money(total)} does not match the supplied or exported iiko total")
    if not database.is_file():
        raise ValueError("import sales for this restaurant first")
    metadata = {"stock_id": f"stock-{uuid4().hex[:12]}", "stock_as_of": as_of.isoformat(),
                "stock_warehouse": warehouse, "stock_total": _money(total),
                "stock_imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with write_lock(database), closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        current = dict(connection.execute("SELECT key, value FROM metadata"))
        if (current.get("dataset_mode") != MODE or current.get("restaurant_name") != restaurant
                or current.get("currency") != currency):
            raise ValueError("stock must match the restaurant name and currency of an imported sales report")
        connection.execute(STOCK_SCHEMA)
        connection.execute("DELETE FROM stock_snapshot")
        connection.executemany("INSERT INTO stock_snapshot VALUES (?, ?, ?, ?, ?)", rows)
        connection.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", metadata.items())
        connection.row_factory = sqlite3.Row
        status = SalesSnapshotService(database, connection).data_status()["stock_snapshot"]
        if not status["passed"]:
            raise ValueError("stored stock values failed reconciliation")
    return status
