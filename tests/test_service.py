from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from iiko_intelligence.seed import seed_demo
from iiko_intelligence.database import open_readonly
from iiko_intelligence.service import AnalyticsService


class AnalyticsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "trial.sqlite"
        self.manifest = seed_demo(self.database, as_of=date(2026, 9, 30), days=30)
        self.service = AnalyticsService(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_generation_is_deterministic(self) -> None:
        second = Path(self.temporary.name) / "second.sqlite"
        other = seed_demo(second, as_of=date(2026, 9, 30), days=30)
        self.assertEqual(self.manifest["dataset_id"], other["dataset_id"])
        self.assertEqual(self.manifest["checks"], other["checks"])
        self.assertEqual(self.manifest["sales_lines"], other["sales_lines"])
        with self.database.open("rb") as first_file, second.open("rb") as second_file:
            self.assertEqual(first_file.read(), second_file.read())

    def test_checks_reconcile_to_lines(self) -> None:
        status = self.service.data_status()
        self.assertEqual(status["dataset_mode"], "synthetic")
        self.assertTrue(status["reconciliation"]["passed"])
        self.assertEqual(status["reconciliation"]["check_to_line_mismatches"], 0)

    def test_sales_matches_direct_database_totals(self) -> None:
        result = self.service.sales_summary("2026-09-01", "2026-09-30")
        with sqlite3.connect(self.database) as connection:
            expected = connection.execute(
                "SELECT COUNT(*), SUM(net_minor) FROM checks"
            ).fetchone()
        self.assertEqual(result["totals"]["checks"], expected[0])
        self.assertEqual(
            result["totals"]["net_sales"],
            str((Decimal(expected[1]) / Decimal(100)).quantize(Decimal("0.00"))),
        )
        self.assertIn("Synthetic trial data", result["warning"])

    def test_top_dishes_marks_truncation_without_changing_scope(self) -> None:
        result = self.service.top_dishes("2026-09-01", "2026-09-30", limit=2)
        self.assertEqual(result["detail"]["returned_rows"], 2)
        self.assertGreater(result["detail"]["total_rows"], 2)
        self.assertTrue(result["detail"]["truncated"])

    def test_invalid_ranges_fail_instead_of_returning_zero(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside dataset coverage"):
            self.service.sales_summary("2026-08-01", "2026-08-31")
        with self.assertRaisesRegex(ValueError, "must not be before"):
            self.service.sales_summary("2026-09-30", "2026-09-01")

    def test_invalid_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            self.service.stock_balances(limit=101)

    def test_database_is_opened_read_only(self) -> None:
        with open_readonly(self.database) as connection:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                connection.execute("DELETE FROM checks")


if __name__ == "__main__":
    unittest.main()
