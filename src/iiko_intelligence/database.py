from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price_minor INTEGER NOT NULL CHECK (price_minor >= 0),
    estimated_cost_minor INTEGER NOT NULL CHECK (estimated_cost_minor >= 0)
) STRICT;

CREATE TABLE checks (
    id TEXT PRIMARY KEY,
    business_date TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('DINE_IN', 'TAKEAWAY', 'DELIVERY')),
    guests INTEGER NOT NULL CHECK (guests >= 0),
    gross_minor INTEGER NOT NULL CHECK (gross_minor >= 0),
    discount_minor INTEGER NOT NULL CHECK (discount_minor >= 0),
    refund_minor INTEGER NOT NULL CHECK (refund_minor >= 0),
    net_minor INTEGER NOT NULL CHECK (net_minor >= 0),
    CHECK (net_minor = gross_minor - discount_minor - refund_minor)
) STRICT;

CREATE INDEX checks_business_date_idx ON checks (business_date);

CREATE TABLE sales_lines (
    id TEXT PRIMARY KEY,
    check_id TEXT NOT NULL REFERENCES checks(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    gross_minor INTEGER NOT NULL CHECK (gross_minor >= 0),
    discount_minor INTEGER NOT NULL CHECK (discount_minor >= 0),
    refund_minor INTEGER NOT NULL CHECK (refund_minor >= 0),
    net_minor INTEGER NOT NULL CHECK (net_minor >= 0),
    CHECK (net_minor = gross_minor - discount_minor - refund_minor)
) STRICT;

CREATE INDEX sales_lines_check_idx ON sales_lines (check_id);
CREATE INDEX sales_lines_product_idx ON sales_lines (product_id);

CREATE TABLE purchases (
    id TEXT PRIMARY KEY,
    business_date TEXT NOT NULL,
    supplier TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli > 0),
    unit TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0)
) STRICT;

CREATE INDEX purchases_business_date_idx ON purchases (business_date);

CREATE TABLE stock_balances (
    item_id TEXT PRIMARY KEY,
    item_name TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli >= 0),
    unit TEXT NOT NULL,
    value_minor INTEGER NOT NULL CHECK (value_minor >= 0)
) STRICT;

CREATE TABLE writeoffs (
    id TEXT PRIMARY KEY,
    business_date TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli > 0),
    unit TEXT NOT NULL,
    value_minor INTEGER NOT NULL CHECK (value_minor >= 0),
    reason TEXT NOT NULL
) STRICT;

CREATE INDEX writeoffs_business_date_idx ON writeoffs (business_date);
"""


class ReportReader:
    def __init__(self, database: Path, connection: sqlite3.Connection | None = None):
        self.database = database
        self._connection = connection

    def _read(self):
        if self._connection is not None:
            return nullcontext(self._connection)
        return open_readonly(self.database)


@contextmanager
def write_lock(path: Path) -> Iterator[None]:
    """Serialize the final publication step across Windows and POSIX processes."""
    with closing(sqlite3.connect(str(path) + "-lock", timeout=8)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        yield


@contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset not found at {path}. Run 'iiko-intelligence init-demo' first."
        )
    connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def create_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection
