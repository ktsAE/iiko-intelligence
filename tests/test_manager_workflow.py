import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from iiko_intelligence.connect import _config_block, _updated_settings
from iiko_intelligence.document_import import import_documents
from iiko_intelligence.manager_reports import compare_periods, export_brief
from iiko_intelligence.office_export import read_sales_export
from iiko_intelligence.sales_snapshot import (
    SalesPeriod,
    SalesRow,
    open_reports,
    save_snapshot,
)
from iiko_intelligence.seed import seed_demo
from iiko_intelligence.stock_import import import_stock


class ManagerWorkflowTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="iiko manager #")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "manager.sqlite"
        self.period = SalesPeriod(date(2026, 9, 1), date(2026, 9, 4), 11050, "Кафе", "RUB")
        self.rows = [SalesRow.from_values("2026-09-01", "01", "Чай", 8, 20),
                     SalesRow.from_values("2026-09-01", "02", "Обед", 1, 80),
                     SalesRow.from_values("2026-09-02", "01", "Чай", 3, 10.50),
                     SalesRow.from_values("2026-09-03", "01", "Чай", 0, 0)]
        self.save()

    def save(self, **overrides):
        return save_snapshot(self.db, replace(self.period, **overrides), self.rows, provider="iikoOffice_export")

    def test_compare_includes_dropped_dishes_and_signed_remainder(self):
        result = compare_periods(self.db, "2026-09-01", "2026-09-01", "2026-09-02", "2026-09-02", limit=1)
        self.assertEqual(result["change"], "-89.50")
        self.assertEqual(result["change_percent"], "-89.50")
        self.assertEqual(result["dish_changes"][0]["change"], "-80.00")
        self.assertEqual(result["other_dishes_change"], "-9.50")
        with open_reports(self.db) as service:
            self.assertEqual(service.top_dishes("2026-09-01", "2026-09-02", rank_by="quantity")["rows"][0]["name"], "Чай")

    def test_compare_distinguishes_a_zero_day_from_a_missing_day(self):
        for day, change, percent in [(3, "-100.00", "-100.00"), (4, None, None)]:
            result = compare_periods(self.db, "2026-09-01", "2026-09-01", f"2026-09-0{day}", f"2026-09-0{day}")
            self.assertEqual((result["change"], result["change_percent"]), (change, percent))
        zero = compare_periods(self.db, "2026-09-03", "2026-09-03", "2026-09-01", "2026-09-03")
        self.assertIsNone(zero["change_percent"])
        self.assertEqual(zero["overlap_days"], 1)
        with self.assertRaisesRegex(ValueError, "outside"):
            compare_periods(self.db, "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-02")

    def test_exports_plain_text_with_distinct_demo_and_snapshot_labels(self):
        first = export_brief(self.db, self.root, "2026-09-01", "2026-09-02", 1)
        body = first.read_text(encoding="utf-8-sig")
        for value in ("Кафе", "110.50", "2026-09-01", "Обед | 1.000 | 80.00", "показано 1 из 2", "iikoOffice_export"):
            self.assertIn(value, body)
        second = export_brief(self.db, self.root, None, None)
        self.assertNotEqual(first, second)
        demo = self.root / "demo.sqlite"
        seed_demo(demo, as_of=date(2026, 9, 30), days=7)
        self.assertIn("УЧЕБНЫЕ ДАННЫЕ", export_brief(demo, self.root, None, None).read_text(encoding="utf-8-sig"))
        with self.assertRaisesRegex(ValueError, "contains iiko data"):
            seed_demo(self.db, days=7)

    def test_rejects_mismatched_workbook_grand_total(self):
        source = self.root / "wrong.csv"
        source.write_text("business_date,product_code,dish,quantity,sales_after_discounts\n"
                          "2026-09-01,1,Tea,1,100\nTotal,,,,101\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "grand total"):
            read_sales_export(source)

    def test_legacy_biff_file_keeps_excel_date_and_displayed_code(self):
        file = Path(__file__).parent / "fixtures/sales-legacy.xls"
        expected = SalesRow.from_values("2026-09-01", "0007", "Чай", 2.5, 125.50)
        self.assertEqual(read_sales_export(file), [expected])

    def test_stock_scope_reconciliation_and_retention(self):
        file = self.root / "Остатки.xlsx"
        book = Workbook()
        sheet = book.create_sheet("Остатки")
        sheet.append(("Артикул", "Наименование", "Ед. изм.", "Количество", "Себестоимость за ед.", "Себестоимость"))
        sheet.append((7, "Чай", "кг", 2, 300, 600))
        sheet["A2"].number_format = "0000"
        sheet.append(("A", "Сахар", "кг", -0.25, 40, -10))
        sheet.append((None, "Итого", None, None, None, 590))
        book.save(file)
        book.close()
        args = dict(restaurant="Кафе", currency="RUB", warehouse="Кухня", as_of=date(2026, 8, 31), expected_minor=59000)
        self.assertTrue(import_stock(self.db, file, **args)["passed"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            import_stock(self.db, file, **{**args, "expected_minor": 59001})
        with self.assertRaisesRegex(ValueError, "restaurant name"):
            import_stock(self.db, file, **{**args, "restaurant": "Другой"})
        self.save()
        with open_reports(self.db) as service:
            result = service.stock_balances(limit=1)
            self.assertEqual((result["as_of"], result["warehouse"], result["total_value"]), ("2026-08-31", "Кухня", "590.00"))
            self.assertEqual(result["rows"][0]["item_id"], "0007")
            negatives = service.stock_balances(negative_only=True)
            self.assertEqual([r["quantity"] for r in negatives["rows"]], ["-0.250"])
            self.assertEqual(negatives["total_value"], "590.00")
            self.assertEqual(result["detail"], dict(total_rows=2, returned_rows=1, truncated=True))
        self.assertIn("Дата остатков: 2026-08-31", export_brief(self.db, self.root, None, None).read_text(encoding="utf-8-sig"))
        self.save(restaurant="Другой")
        with open_reports(self.db) as service:
            self.assertEqual(service.stock_balances()["status"], "unavailable")

    @unittest.skipIf(os.name == "nt", "Windows may deny replacement of an open database")
    def test_one_request_keeps_a_consistent_snapshot_during_refresh(self):
        with open_reports(self.db) as previous:
            before = previous.data_status()
            after = self.save()
            self.assertEqual(previous.sales_summary("2026-09-01", "2026-09-01")["dataset_id"], before["dataset_id"])
            self.assertNotEqual(after["dataset_id"], before["dataset_id"])

    def test_setup_updates_only_installer_generated_paths(self):
        def params(root, cmd):
            return {"command": str(root / cmd), "args": ["-m", "iiko_intelligence.mcp_server"],
                    "env": {"IIKO_INTELLIGENCE_DB": str(root / "data/manager.sqlite")}}
        old = params(self.root / "old", ".venv/Scripts/python.exe")
        new = params(self.root / "new", "runtime/python.exe")
        previous = 'model = "example"\n\n' + _config_block(old)
        updated = _updated_settings(previous, new)
        self.assertIn('model = "example"', updated)
        self.assertEqual(_updated_settings(updated, new), updated)
        self.assertEqual(_updated_settings(_config_block(params(self.root, ".venv/bin/python3")), new), _config_block(new))
        self.assertIn(new["command"], updated)
        with self.assertRaisesRegex(ValueError, "manual review"):
            _updated_settings(_config_block(params(self.root, "my-custom.exe")), new)

    def test_document_types_have_independent_coverage_and_totals(self):
        for kind, party, typ in [("purchases", "supplier", "incoming_invoice"), ("writeoffs", "warehouse", "writeoff_act")]:
            file = self.root / (kind + ".csv")
            total = "50.00" if kind == "purchases" else "5.00"
            source = f"document_date,document_number,document_type,status,{party},document_amount\n"
            period = SalesPeriod(date(2026, 8, 1), date(2026, 8, 31), int(total.split('.')[0]) * 100, "Кафе", "RUB")
            text = source + f"2026-08-03,0009,{typ},posted,Тест,{total}\n"
            file.write_text(text, encoding="utf-8")
            self.assertTrue(import_documents(self.db, file, kind, period, 1)["passed"])
            for bad, count, error in [(text.replace("posted", "draft"), 1, "posted"),
                                      (text.replace(typ, "return"), 1, "posted"),
                                      (text, 2, "do not match"),
                                      (text + text.splitlines()[-1], 2, "duplicate")]:
                file.write_text(bad, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    import_documents(self.db, file, kind, period, count)
        self.save()
        with open_reports(self.db) as service:
            for method, party, amount in [(service.purchases, "supplier", "50.00"),
                                          (service.writeoffs, "warehouse", "5.00")]:
                doc = method("2026-08-01", "2026-08-31")
                self.assertEqual(doc["totals"], {"documents": 1, "document_amount": amount})
                self.assertEqual(doc["rows"][0]["number"], "0009")
                self.assertEqual(doc["by_" + party][0][party], "Тест")
                with self.assertRaisesRegex(ValueError, "document coverage"):
                    method("2026-09-01", "2026-09-02")
        self.save(restaurant="Другой")
        with open_reports(self.db) as service:
            self.assertEqual(service.data_status()["document_sources"], {})

    def test_empty_posted_register_requires_a_verified_zero(self):
        src = self.root / "zero.csv"
        src.write_text("document_date,document_number,document_type,status,warehouse,document_amount\n")
        result = import_documents(self.db, src, "writeoffs", replace(self.period, expected_minor=0), 0)
        self.assertEqual((result["documents"], result["total"], result["passed"]), (0, "0.00", True))
