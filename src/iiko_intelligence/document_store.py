"""Independent closed-period totals for posted incoming invoices and write-off acts."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from .service import _money, _parse_date, _validated_limit


def create_document_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS document_sets (
        kind TEXT PRIMARY KEY, first_day TEXT NOT NULL, last_day TEXT NOT NULL, expected_count INTEGER NOT NULL,
        expected_minor INTEGER NOT NULL, imported_at TEXT NOT NULL, source_id TEXT NOT NULL
    ) STRICT""")
    db.execute("""CREATE TABLE IF NOT EXISTS report_documents (
        kind TEXT NOT NULL, day TEXT NOT NULL, number TEXT NOT NULL, party TEXT NOT NULL, amount INTEGER NOT NULL,
        PRIMARY KEY(kind, day, number)
    ) STRICT""")


def has_documents(db: sqlite3.Connection) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_sets'").fetchone())


def copy_documents(old: sqlite3.Connection, new: sqlite3.Connection) -> None:
    if has_documents(old):
        create_document_tables(new)
        new.executemany("INSERT INTO document_sets VALUES (?, ?, ?, ?, ?, ?, ?)", old.execute(
            "SELECT kind, first_day, last_day, expected_count, expected_minor, imported_at, source_id FROM document_sets"))
        new.executemany("INSERT INTO report_documents VALUES (?, ?, ?, ?, ?)", old.execute(
            "SELECT kind, day, number, party, amount FROM report_documents"))


def document_status(db: sqlite3.Connection) -> dict[str, Any]:
    if not has_documents(db):
        return {}
    results = {}
    for source in db.execute("SELECT * FROM document_sets"):
        count, amount = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM report_documents WHERE kind=?", (source["kind"],)
        ).fetchone()
        results[source["kind"]] = {
            "coverage": {"from": source["first_day"], "to": source["last_day"]}, "documents": count,
            "total": _money(amount), "iiko_count": source["expected_count"], "iiko_total": _money(source["expected_minor"]),
            "imported_at": source["imported_at"], "source_id": source["source_id"],
            "passed": count == source["expected_count"] and amount == source["expected_minor"],
        }
    return results


def document_report(db: sqlite3.Connection, kind: str, start: str, end: str, limit: int) -> dict[str, Any]:
    limit = _validated_limit(limit)
    source = document_status(db).get(kind)
    if source is None:
        return {"status": "unavailable", "capability": kind, "reason": "Import a reconciled posted-document list for this report."}
    if not source["passed"]:
        raise ValueError("the stored document list failed reconciliation")
    first, last = _parse_date(start, "date_from"), _parse_date(end, "date_to")
    coverage = source["coverage"]
    if not date.fromisoformat(coverage["from"]) <= first <= last <= date.fromisoformat(coverage["to"]):
        raise ValueError(f"period outside the {kind} document coverage ({coverage['from']} to {coverage['to']})")
    params = (kind, first.isoformat(), last.isoformat())
    count, amount = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM report_documents "
                               "WHERE kind=? AND day BETWEEN ? AND ?", params).fetchone()
    rows = db.execute("SELECT day, number, party, amount FROM report_documents WHERE kind=? AND day BETWEEN ? AND ? "
                      "ORDER BY amount DESC, day, number LIMIT ?", (*params, limit)).fetchall()
    party = "supplier" if kind == "purchases" else "warehouse"
    groups = db.execute("SELECT party, COUNT(*) AS documents, SUM(amount) AS amount FROM report_documents "
                        "WHERE kind=? AND day BETWEEN ? AND ? GROUP BY party ORDER BY amount DESC, party LIMIT ?",
                        (*params, limit + 1)).fetchall()
    return {"status": "complete", "dataset_id": source["source_id"], "coverage": coverage,
            "source": {"provider": "iikoOffice_posted_document_export", "imported_at": source["imported_at"]},
            "period": {"from": start, "to": end, "date_basis": "document_date"},
            "granularity": "document", "document_type": "incoming_invoice" if kind == "purchases" else "writeoff_act",
            "totals": {"documents": count, "document_amount": _money(amount)},
            "by_" + party: [{party: g["party"], "documents": g["documents"], "amount": _money(g["amount"])}
                            for g in groups[:limit]], "groups_truncated": len(groups) > limit,
            "rows": [{"date": r["day"], "number": r["number"], party: r["party"], "amount": _money(r["amount"])} for r in rows],
            "detail": {"returned_rows": len(rows), "total_rows": count, "truncated": count > len(rows)}}
