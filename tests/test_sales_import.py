from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from iiko_intelligence.connect import verify_connection
from iiko_intelligence.office_export import AmbiguousSalesSheet, read_sales_export
from iiko_intelligence.sales_snapshot import SalesPeriod, SalesRow, reporting_service, save_snapshot


HEADERS = "Учетный день", "Код блюда", "Блюдо", "Количество блюд", "Сумма со скидкой, р."
ROWS = (
    ("1.09.2026", "B", "Борщ", 4, "1 280,00р."),
    ("01.09.2026", "A", "Чай", "2,5", "430,50"),
    ("2026-09-02", "B", "Борщ", -1, "-320,00"),
)
PERIOD = SalesPeriod(date(2026, 9, 1), date(2026, 9, 2), 139050, "Семейное кафе", "RUB")


class SalesImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="iiko отчеты #")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.snapshot = self.root / "отчет.sqlite"

    def _csv(self) -> Path:
        source = self.root / "sales.csv"
        with source.open("w", encoding="cp1251", newline="") as file:
            writer = csv.writer(file, delimiter=";")
            writer.writerow(["Продажи за 1–2 сентября"])
            writer.writerow(HEADERS)
            writer.writerows(ROWS)
            writer.writerow(["Итого", "", "", "", "1390,50"])
        return source

    def test_csv_import_reconciles_and_returns_ranked_sales_over_mcp(self) -> None:
        cmd = [sys.executable, "-m", "iiko_intelligence.cli", "--db", str(self.snapshot), "import-sales",
               "--file", str(self._csv()), "--from", "2026-09-01", "--to", "2026-09-02",
               "--iiko-total", "1390,50 ₽", "--restaurant", "Семейное кафе", "--currency", "RUB"]
        environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        status = json.loads(subprocess.check_output(cmd, text=True, encoding="utf-8", env=environment))
        self.assertTrue(status["reconciliation"]["passed"])
        service = reporting_service(self.snapshot)
        result = service.sales_summary("2026-09-01", "2026-09-02")
        self.assertEqual(result["totals"], {"sales_after_discounts": "1390.50"})
        self.assertEqual(result["by_day"][1]["sales_after_discounts"], "-320.00")
        dishes = service.top_dishes("2026-09-01", "2026-09-02", limit=1)
        self.assertEqual(dishes["rows"][0]["name"], "Борщ")
        self.assertTrue(dishes["detail"]["truncated"])
        self.assertEqual(service.stock_balances()["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "outside snapshot coverage"):
            service.sales_summary("2026-09-01", "2026-09-03")
        self.assertEqual(verify_connection(self.snapshot)["dataset_mode"], "iiko_sales_snapshot")

    def test_reports_currency_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "ruble amount"):
            read_sales_export(self._csv(), currency="USD")

    def test_failed_validation_preserves_last_snapshot(self) -> None:
        rows = read_sales_export(self._csv())
        save_snapshot(self.snapshot, PERIOD, rows, provider="iikoOffice_export")
        original = self.snapshot.read_bytes()
        bad_period = SalesPeriod(PERIOD.start, PERIOD.end, 2000, PERIOD.restaurant, PERIOD.currency)
        cases = ((bad_period, rows, "does not match"), (PERIOD, rows + rows, "duplicate"),
                 (PERIOD, [SalesRow(date(2026, 9, 3), "B", "Борщ", 1, 0)], "outside"))
        for period, values, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                save_snapshot(self.snapshot, period, values, provider="iikoOffice_export")
        self.assertEqual(self.snapshot.read_bytes(), original)

    def test_xlsx_dates_multisheet_and_value_only(self) -> None:
        source = self.root / "sales.xlsx"
        workbook = Workbook()
        table = workbook.create_sheet("Продажи")
        table.append(HEADERS)
        table.append((date(2026, 9, 1), 414, "Чай", 2, 140.5))
        workbook.save(source)
        parsed = read_sales_export(source, sheet="Продажи")
        self.assertEqual(parsed, [SalesRow(date(2026, 9, 1), "414", "Чай", 2000, 14050)])
        self.assertEqual(read_sales_export(source), parsed)
        table["E2"] = "=SUM(20,120.5)"
        workbook.save(source)
        with self.assertRaisesRegex(ValueError, "formulas are unsupported"):
            read_sales_export(source, sheet="Продажи")
        workbook.close()

    def test_requires_choice_when_two_workbook_sheets_match(self) -> None:
        source = self.root / "branches.xlsx"
        workbook = Workbook()
        for title in ("Север", "Юг"):
            table = workbook.create_sheet(title)
            table.append(HEADERS)
            table.append((date(2026, 9, 1), title, "Чай", 2, 140.5))
        workbook.save(source)
        workbook.close()
        with self.assertRaisesRegex(AmbiguousSalesSheet, "Север, Юг"):
            read_sales_export(source)
        self.assertEqual(read_sales_export(source, sheet="Юг")[0].product_id, "Юг")

    def test_merged_dates_reconcile_to_each_daily_subtotal(self) -> None:
        source = self.root / "grouped.xlsx"
        workbook = Workbook()
        table = workbook.active
        table.append(HEADERS)
        table.append(ROWS[0])
        table.append((None, *ROWS[1][1:]))
        table.merge_cells("A2:A3")
        table.append(("01.09.2026 Всего", None, None, None, "1 710,50р."))
        table.append(ROWS[2])
        table.append(("Итого", None, None, None, "1 390,50р."))
        workbook.save(source)
        rows = read_sales_export(source)
        self.assertEqual([row.day for row in rows], [date(2026, 9, 1)] * 2 + [date(2026, 9, 2)])
        self.assertEqual(sum(row.sales_minor for row in rows), 139050)

        table["E4"] = "1 710,49р."
        workbook.save(source)
        with self.assertRaisesRegex(ValueError, "accounting-day subtotal"):
            read_sales_export(source)

        table.delete_rows(4)
        workbook.save(source)
        with self.assertRaisesRegex(ValueError, "grouped dates need a daily subtotal"):
            read_sales_export(source)
        workbook.close()
