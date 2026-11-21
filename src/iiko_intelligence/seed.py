from __future__ import annotations

import hashlib
import os
import random
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .database import create_database, open_readonly, write_lock


SEED = 20261022
DEFAULT_AS_OF = date(2026, 9, 30)
DEFAULT_DAYS = 90

PRODUCTS = (
    ("dish-borscht", "Borscht", "Soups", 49000, 12600),
    ("dish-salmon", "Salmon with vegetables", "Main courses", 119000, 43800),
    ("dish-pasta", "Mushroom pasta", "Main courses", 76000, 21400),
    ("dish-burger", "House burger", "Main courses", 82000, 26800),
    ("dish-salad", "Seasonal salad", "Starters", 56000, 13700),
    ("dish-dessert", "Honey cake", "Desserts", 43000, 9800),
    ("dish-coffee", "Cappuccino", "Drinks", 29000, 6200),
    ("dish-tea", "Tea pot", "Drinks", 33000, 4100),
)

INVENTORY = (
    ("item-salmon", "Salmon fillet", "kg", 145000),
    ("item-beef", "Beef", "kg", 92000),
    ("item-vegetables", "Fresh vegetables", "kg", 28000),
    ("item-dairy", "Dairy products", "kg", 47000),
    ("item-coffee", "Coffee beans", "kg", 185000),
    ("item-flour", "Flour", "kg", 6500),
)

SUPPLIERS = ("North Foods", "City Produce", "Daily Dairy")
REASONS = ("Spoilage", "Preparation loss", "Quality rejection")


def _dataset_id(as_of: date, days: int) -> str:
    value = f"demo:{SEED}:{as_of.isoformat()}:{days}".encode()
    return "demo-" + hashlib.sha256(value).hexdigest()[:12]


def seed_demo(
    path: Path, *, as_of: date = DEFAULT_AS_OF, days: int = DEFAULT_DAYS, preserve_existing: bool = False,
) -> dict[str, object]:
    if not 7 <= days <= 366:
        raise ValueError("days must be between 7 and 366")

    rng = random.Random(SEED)
    start = as_of - timedelta(days=days - 1)
    dataset_id = _dataset_id(as_of, days)
    generated_at = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc).isoformat()
    check_count = 0
    line_count = 0

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".demo-", suffix=".sqlite", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection = None
    try:
        connection = create_database(temporary)
        connection.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?)", PRODUCTS
        )
        metadata = {
            "dataset_id": dataset_id,
            "dataset_mode": "synthetic",
            "restaurant_id": "demo-restaurant-1",
            "restaurant_name": "Demo Restaurant",
            "currency": "RUB",
            "timezone": "Europe/Moscow",
            "coverage_start": start.isoformat(),
            "coverage_end": as_of.isoformat(),
            "generated_at": generated_at,
            "seed": str(SEED),
            "schema_version": "trial-v1",
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items()
        )

        current = start
        while current <= as_of:
            weekday_multiplier = 1.22 if current.weekday() >= 4 else 1.0
            daily_checks = round(rng.randint(34, 47) * weekday_multiplier)
            for sequence in range(daily_checks):
                check_count += 1
                check_id = f"check-{current:%Y%m%d}-{sequence:04d}"
                line_total = rng.randint(1, 4)
                selected = rng.choices(PRODUCTS, weights=(9, 4, 7, 8, 7, 5, 13, 8), k=line_total)
                gross = discount = refund = 0
                line_rows = []
                discount_rate = 10 if rng.random() < 0.12 else 0
                refunded_index = rng.randrange(line_total) if rng.random() < 0.025 else -1
                for index, product in enumerate(selected):
                    quantity = 2 if rng.random() < 0.08 else 1
                    line_gross = product[3] * quantity
                    line_discount = line_gross * discount_rate // 100
                    line_refund = line_gross - line_discount if index == refunded_index else 0
                    line_net = line_gross - line_discount - line_refund
                    line_count += 1
                    line_rows.append(
                        (
                            f"line-{check_count:07d}-{index}",
                            check_id,
                            product[0],
                            quantity,
                            line_gross,
                            line_discount,
                            line_refund,
                            line_net,
                        )
                    )
                    gross += line_gross
                    discount += line_discount
                    refund += line_refund

                hour = rng.choices(
                    population=list(range(10, 24)),
                    weights=(1, 2, 7, 10, 6, 3, 3, 5, 8, 10, 9, 7, 5, 2),
                    k=1,
                )[0]
                opened_at = datetime.combine(
                    current,
                    time(hour=hour, minute=rng.randrange(0, 60)),
                ).isoformat()
                channel = rng.choices(
                    ("DINE_IN", "TAKEAWAY", "DELIVERY"), (72, 13, 15), k=1
                )[0]
                guests = rng.randint(1, 4) if channel == "DINE_IN" else 1
                connection.execute(
                    "INSERT INTO checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        check_id,
                        current.isoformat(),
                        opened_at,
                        channel,
                        guests,
                        gross,
                        discount,
                        refund,
                        gross - discount - refund,
                    ),
                )
                connection.executemany(
                    "INSERT INTO sales_lines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    line_rows,
                )

            if current.weekday() in (0, 3):
                for item_index, item in enumerate(INVENTORY):
                    quantity_milli = rng.randint(8_000, 25_000)
                    amount = quantity_milli * item[3] // 1_000
                    connection.execute(
                        "INSERT INTO purchases VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"purchase-{current:%Y%m%d}-{item_index}",
                            current.isoformat(),
                            SUPPLIERS[item_index % len(SUPPLIERS)],
                            item[1],
                            quantity_milli,
                            item[2],
                            amount,
                        ),
                    )

            if rng.random() < 0.55:
                item_index = rng.randrange(len(INVENTORY))
                item = INVENTORY[item_index]
                quantity_milli = rng.randint(100, 900)
                connection.execute(
                    "INSERT INTO writeoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"writeoff-{current:%Y%m%d}-{item_index}",
                        current.isoformat(),
                        item[0],
                        item[1],
                        quantity_milli,
                        item[2],
                        quantity_milli * item[3] // 1_000,
                        rng.choice(REASONS),
                    ),
                )
            current += timedelta(days=1)

        for index, item in enumerate(INVENTORY):
            quantity_milli = 12_500 + index * 3_125
            connection.execute(
                "INSERT INTO stock_balances VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item[0],
                    item[1],
                    as_of.isoformat(),
                    quantity_milli,
                    item[2],
                    quantity_milli * item[3] // 1_000,
                ),
            )

        reconciliation = connection.execute(
            """
            SELECT COUNT(*) AS mismatches
            FROM checks AS c
            JOIN (
                SELECT check_id,
                       SUM(gross_minor) AS gross,
                       SUM(discount_minor) AS discount,
                       SUM(refund_minor) AS refund,
                       SUM(net_minor) AS net
                FROM sales_lines GROUP BY check_id
            ) AS l ON l.check_id = c.id
            WHERE c.gross_minor != l.gross
               OR c.discount_minor != l.discount
               OR c.refund_minor != l.refund
               OR c.net_minor != l.net
            """
        ).fetchone()[0]
        if reconciliation:
            raise RuntimeError(f"demo generation produced {reconciliation} mismatched checks")

        connection.commit()
        connection.close()
        connection = None
        with write_lock(path):
            if path.exists():
                if preserve_existing:
                    return {"status": "already_present", "database": str(path)}
                with open_readonly(path) as existing:
                    mode = existing.execute("SELECT value FROM metadata WHERE key='dataset_mode'").fetchone()
                    if not mode or mode[0] != "synthetic":
                        raise ValueError("choose a different --db for the demo; the selected database contains iiko data")
            os.replace(temporary, path)
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
    return {
        "dataset_id": dataset_id,
        "database": str(path),
        "coverage_start": start.isoformat(),
        "coverage_end": as_of.isoformat(),
        "checks": check_count,
        "sales_lines": line_count,
    }
