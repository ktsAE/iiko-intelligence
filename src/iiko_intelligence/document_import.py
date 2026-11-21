from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .database import write_lock
from .document_store import create_document_tables, document_status
from .office_export import MAX_COLUMNS, MAX_TITLE_ROWS, _columns, _heading, export_rows
from .sales_snapshot import MAX_SALES_ROWS, MODE, SalesPeriod, number_units, report_date, report_text, strip_ruble_suffix
from .service import _money


def import_documents(database: Path, source: Path, kind: str, period: SalesPeriod,
                     expected_count: int, sheet: str | None = None) -> dict:
    if kind not in {"purchases", "writeoffs"} or not 0 <= expected_count <= MAX_SALES_ROWS:
        raise ValueError("choose purchases or writeoffs and a valid iiko document count")
    invoice = kind == "purchases"
    types = {"incoming_invoice", "приходная накладная"} if invoice else {"writeoff_act", "акт списания"}
    fields = (("document_date", "дата документа", "дата"), ("document_number", "номер документа", "№ документа"),
              ("document_type", "тип документа"), ("status", "статус"),
              ("supplier", "поставщик") if invoice else ("warehouse", "склад"),
              ("document_amount", "сумма документа", "сумма, р."))
    columns = ()
    documents = {}
    with export_rows(source, sheet, fields) as table:
        for i, cells in enumerate(table, 1):
            if i > MAX_SALES_ROWS + MAX_TITLE_ROWS or any(v not in (None, "") for v in cells[MAX_COLUMNS:]):
                raise ValueError("document list exceeds supported dimensions")
            if not columns:
                if i > MAX_TITLE_ROWS:
                    break
                columns = _columns(cells, fields)
                continue
            values = [cells[j] if j < len(cells) else None for j in columns]
            if all(v in ("", None) for v in values):
                continue
            day, number, doc_type, status, party, value = values
            try:
                if _heading(doc_type) not in types or _heading(status) not in {"posted", "проведен", "проведена"}:
                    raise ValueError("every row must identify the selected document type and a posted, active document")
                if any(isinstance(v, str) and v.startswith("=") for v in values):
                    raise ValueError("export value-only document cells")
                when = report_date(day)
                if not period.start <= when <= period.end:
                    raise ValueError("document date outside the supplied period")
                ref = report_text(str(number) if type(number) is int else number, "document number", limit=80)
                key = when.isoformat(), ref
                if key in documents:
                    raise ValueError("duplicate document date and number; use one row per document")
                amount = number_units(strip_ruble_suffix(value, period.currency), 2, "document amount")
                if amount < 0:
                    raise ValueError("export positive document amounts; handle returns and reversals separately")
                documents[key] = (report_text(party, "supplier or warehouse"), amount)
            except ValueError as exc:
                raise ValueError(f"document row {i}: {exc}") from None
    if not columns:
        raise ValueError("required document headers: " + ", ".join(f[0] for f in fields))
    amount = sum(v for _, v in documents.values())
    if len(documents) != expected_count or amount != period.expected_minor:
        raise ValueError(f"{len(documents)} documents totaling {_money(amount)} do not match the iiko count and amount")
    if not database.is_file():
        raise ValueError("import sales for this restaurant first")
    with write_lock(database), closing(sqlite3.connect(database)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if (metadata.get("dataset_mode") != MODE or metadata.get("restaurant_name") != period.restaurant
                or metadata.get("currency") != period.currency):
            raise ValueError("document list must match the restaurant and currency of an imported sales report")
        create_document_tables(db)
        db.execute("DELETE FROM report_documents WHERE kind=?", (kind,))
        db.executemany("INSERT INTO report_documents VALUES (?, ?, ?, ?, ?)",
                       ((kind, day, number, party, cost) for (day, number), (party, cost) in documents.items()))
        db.execute("INSERT OR REPLACE INTO document_sets VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (kind, period.start.isoformat(), period.end.isoformat(), expected_count, period.expected_minor,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), f"docs-{uuid4().hex[:12]}"))
        result = document_status(db)[kind]
        if not result["passed"]:
            raise ValueError("stored documents failed reconciliation")
    return result
