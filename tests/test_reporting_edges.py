"""Regression cases for the manager's everyday questions, exercised through the MCP client."""
from __future__ import annotations

import io
import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from uuid import UUID

from mcp import Client

from iiko_intelligence import cloud_paths as paths, cloud_sales as sales
from iiko_intelligence.cloud_checks import check_access
from iiko_intelligence.cloud_reports import CloudReports
from iiko_intelligence.mcp_server import server
from iiko_intelligence.report_calendar import RestaurantCalendar
from test_cloud_reports import AT, DAY, DISH, ORG, PROFILE, SALT, STORE, Restaurant as Warehouse
from test_live_sales import Restaurant as SalesRestaurant


class CalendarTests(TestCase):
    def setUp(self):
        self.instant = datetime.fromisoformat("2026-11-22T21:04:00+00:00")
        self.terminal = Mock(return_value=["Russian Standard Time"])
        self.calendar = RestaurantCalendar(self.terminal, None, self.instant)

    def assert_period(self, calendar, left, right, first, last):
        period = calendar.resolve(left, right)
        spans = period.windows
        self.assertEqual((spans[0].start.isoformat(), spans[-1].end.isoformat()), (first, last))
        for span in spans:
            self.assertGreaterEqual(span.end, span.start)
            self.assertLessEqual((span.end - span.start).days, 30)
        for a, b in zip(spans, spans[1:]):
            self.assertEqual(b.start - a.end, timedelta(days=1))
        return period

    def test_monday_rolling_periods_and_calendar_boundaries(self):
        for left, right, first, last in [
            (" ЗА прошлую НЕДЕЛЮ ", "last_week", "2026-11-16", "2026-11-22"),
            ("за последние 14 дней", "last_14_days", "2026-11-10", "2026-11-23"),
            ("последние 14 полных дней", "last_14_complete_days", "2026-11-09", "2026-11-22"),
            ("позапрошлую неделю", "week_before_last", "2026-11-09", "2026-11-15"),
            ("позапрошлый месяц", "month_before_last", "2026-09-01", "2026-09-30"),
            ("previous_week_to_date", "last week to date", "2026-11-16", "2026-11-16"),
            ("эта неделя", "this_week", "2026-11-23", "2026-11-23"),
            ("прошлый месяц", "last month", "2026-10-01", "2026-10-31"),
            ("this_month", "этот месяц", "2026-11-01", "2026-11-23"),
            ("за последние 7 дней", "last_7_days", "2026-11-17", "2026-11-23"),
            ("последние 30 полных дней", "last_30_complete_days", "2026-10-24", "2026-11-22"),
            ("20.11.2026", "вчера", "2026-11-20", "2026-11-22"),
            ("позавчера", "today", "2026-11-21", "2026-11-23"),
            ("last quarter", "прошлый квартал", "2026-07-01", "2026-09-30"),
            ("этот квартал", "this_quarter", "2026-10-01", "2026-11-23"),
            ("этот год", "this_year", "2026-01-01", "2026-11-23"),
        ]:
            with self.subTest(left=left):
                self.assert_period(self.calendar, left, right, first, last)
        self.terminal.assert_called_once_with()

    def test_leap_year_comparable_month_and_new_year(self):
        for instant, key, first, last in [
            ("2024-03-31", "previous_month_to_date", "2024-02-01", "2024-02-29"),
            ("2025-03-31", "previous_month_to_date", "2025-02-01", "2025-02-28"),
            ("2027-01-02", "previous_month_to_date", "2026-12-01", "2026-12-02"),
            ("2025-01-01", "last_year", "2024-01-01", "2024-12-31"),
            ("2025-01-01", "last_quarter", "2024-10-01", "2024-12-31"),
            ("2025-01-01", "last_week", "2024-12-23", "2024-12-29"),
        ]:
            with self.subTest(instant=instant, key=key):
                calendar = RestaurantCalendar(self.terminal, "UTC", datetime.fromisoformat(instant).replace(tzinfo=timezone.utc))
                self.assert_period(calendar, key, key, first, last)

    def test_ambiguous_invalid_and_explicit_dates(self):
        for a, b in [("за неделю", "за неделю"), ("01/02/2026", "01/02/2026"), ("31.02.2026", "31.02.2026"),
                     ("last_week", "yesterday"), ("2024-01-01", "2025-01-01"), ("2026-11-22", "2026-11-20")]:
            with self.subTest(bounds=(a, b)), self.assertRaises(ValueError):
                self.calendar.resolve(a, b)
        self.assert_period(self.calendar, "29.02.2024", "01.03.2024", "2024-02-29", "2024-03-01")
        self.terminal.assert_not_called()

    def test_stock_and_recipe_instants_at_offset_change(self):
        for at, expected in [("2026-03-09T05:00:00+00:00", "2026-03-08T23:59:59.999-04:00"),
                             ("2026-11-02T06:00:00+00:00", "2026-11-01T23:59:59.999-05:00")]:
            calendar = RestaurantCalendar(self.terminal, "America/New_York", datetime.fromisoformat(at))
            self.assertEqual(calendar.timestamp("end_of_yesterday"), expected)
        self.assertEqual(self.calendar.timestamp("сейчас"), self.instant.isoformat())
        self.terminal.assert_not_called()
        self.assertEqual(self.calendar.single_day("вчера").isoformat(), "2026-11-22")
        self.assertEqual(self.calendar.timestamp("now", local_date_needed=True)[:10], "2026-11-23")
        self.assertEqual(self.calendar.timestamp("2026-11-22T21:04:00Z", local_date_needed=True)[:10], "2026-11-23")
        with self.assertRaisesRegex(ValueError, "single"):
            self.calendar.single_day("this_week")
        with self.assertRaisesRegex(ValueError, "offset"):
            self.calendar.timestamp("yesterday")


class WarehouseTests(TestCase):
    def setUp(self):
        self.upstream = Warehouse()
        self.api = CloudReports(PROFILE, self.upstream.post)

    def test_document_windows_pages_scoping_and_late_failure(self):
        for kind in paths.DOCUMENTS:
            with self.subTest(kind=kind):
                reads = []
                def fetch(path, body):
                    self.assertEqual((path, body["storeIds"]), (paths.DOCUMENTS[kind][0], [STORE]))
                    reads.append((body["from"], body["to"]))
                    return [{"date": body["to"], "documentId": str(UUID(int=date.fromisoformat(body["to"]).toordinal())),
                             "assignedStores": [STORE], "storageFrom": STORE}]
                with patch.object(self.api, "read", side_effect=fetch):
                    result = self.api.documents(kind, "2024-01-01", "31.03.2024", STORE, 1, 1)
                    self.assertEqual((result["total_rows"], result["next_offset"], result["rows"][0]["date"]), (3, 2, "2024-03-02"))
                    self.assertEqual(result["next_page_arguments"], {"date_from": "2024-01-01", "date_to": "2024-03-31",
                                                                    "limit": 1, "offset": 2, "store_id": STORE})
                    self.assertEqual(reads, [("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-03-02"),
                                             ("2024-03-03", "2024-03-31")])
                with patch.object(self.api, "read", side_effect=[[], sales.CloudHTTPError(paths.DOCUMENTS[kind][0], 503)]):
                    with self.assertRaises(sales.CloudHTTPError):
                        self.api.documents(kind, "2024-01-01", "2024-02-10", None, 100, 0)

    def test_stock_pagination_pins_now(self):
        instants = []
        def fetch(path, body):
            instants.append(body["balanceAt"])
            return {"balanceAt": body["balanceAt"], "organizationId": ORG, "offset": body["offset"], "totalCount": 2,
                    "items": [{"organizationId": ORG, "storeId": STORE, "productId": str(UUID(int=1 + body["offset"])),
                               "quantity": 1}]}
        with patch.object(self.api, "read", side_effect=fetch):
            first = self.api.stock("now", STORE, None, 1, 0)
            args = first["next_page_arguments"]
            final = self.api.stock(args["as_of"], args["store_id"], args["product_id"], args["limit"], args["offset"])
            self.assertEqual(instants, [first["balanceAt"], first["balanceAt"]])
            self.assertNotIn("next_page_arguments", final)

    def test_duplicates_and_ambiguous_price(self):
        doc = {"documentId": ORG, "date": DAY}
        self.upstream.overrides[paths.DOCUMENTS["invoice"][0]] = [doc, doc]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.api.documents("invoice", DAY, DAY, None, 1, 0)
        quote = {"storeId": STORE, "productId": SALT, "costPrice": 10, "quantityFactor": 1}
        for rows, problems in [([quote, quote], []), ([dict(quote, productSizeId=ORG)], []), ([quote], [quote])]:
            self.upstream.overrides[paths.COST] = {"items": rows, "problemItems": problems}
            result = self.api.cost(SALT, STORE, AT, 1)
            self.assertEqual((result["status"], result["rows"]), ("unpriced", []))

    def test_diagnostic_does_not_use_utc_date_for_local_documents(self):
        clock = datetime.fromisoformat("2026-11-22T21:04:00+00:00")
        with patch("iiko_intelligence.cloud_checks.datetime", wraps=datetime) as frozen, patch.object(
                self.api, "documents", return_value={"rows": []}) as documents:
            frozen.now.return_value = clock
            result = check_access(self.api, "documents", None, None, None, "Europe/Moscow")
            self.assertEqual(result["checks"]["get_incoming_invoices"]["status"], "no_data")
            self.assertTrue(all(c.args[1:3] == ("2026-11-23", "2026-11-23") for c in documents.call_args_list))


class ProtocolTests(IsolatedAsyncioTestCase):
    async def test_questions_through_tools(self):
        upstream = SalesRestaurant()
        now = datetime.fromisoformat("2026-10-01T21:01:00+00:00")
        with patch("iiko_intelligence.live_sales.datetime", wraps=datetime) as clock, patch(
                "iiko_intelligence.cloud_mcp.read_profile", return_value=PROFILE), patch(
                "iiko_intelligence.cloud_mcp.CloudReports", side_effect=lambda profile: upstream.api()):
            clock.now.return_value = now
            async with Client(server) as client:
                async def call(tool, **args):
                    reply = await client.call_tool(tool, args)
                    self.assertFalse(reply.is_error, reply)
                    return reply.structured_content
                report = await call("get_sales_summary", date_from="за прошлый месяц", date_to="last_month")
                self.assertEqual((report["period"]["from"], report["period"]["to"], report["totals"]["sales_after_discounts"]),
                                 ("2026-09-01", "2026-09-30", "130.00"))
                top = await call("get_top_dishes", date_from="этот месяц", date_to="this_month")
                self.assertEqual((top["rows"][0]["name"], top["scope"]), ("Суп", "organization"))
                compare = await call("compare_sales_periods", baseline_from="last_month", baseline_to="last_month",
                                     current_from="this_month", current_to="this_month")
                self.assertEqual(set(compare["comparison_notes"]), {"unequal_period_lengths", "includes_open_day", "days_without_rows"})
                overlap = await call("compare_sales_periods", baseline_from="yesterday", baseline_to="yesterday",
                                     current_from="last_7_days", current_to="last_7_days")
                self.assertIn("periods_overlap", overlap["comparison_notes"])
                ambiguous = await call("get_sales_summary", source="live", date_from="за неделю", date_to="за неделю")
                self.assertIn("Clarify", ambiguous["reason"])
                self.assertNotIn("totals", ambiguous)

    async def test_document_recipe_and_stock_shortcuts_through_tools(self):
        warehouse = Warehouse()
        api = CloudReports(PROFILE, warehouse.post)
        warehouse.overrides[paths.DOCUMENTS["invoice"][0]] = []
        with patch("iiko_intelligence.cloud_reports.datetime", wraps=datetime) as clock, patch(
                "iiko_intelligence.cloud_mcp.CloudReports", return_value=api), patch(
                "iiko_intelligence.cloud_mcp.read_profile", return_value=PROFILE):
            clock.now.return_value = datetime.fromisoformat("2026-10-10T21:01:00+00:00")
            async with Client(server) as client:
                common = {"restaurant_timezone": "Europe/Moscow"}
                docs = (await client.call_tool("get_incoming_invoices", {**common, "date_from": "last_quarter",
                                                                         "date_to": "прошлый квартал"})).structured_content
                self.assertEqual((docs["date_from"], docs["date_to"], docs["total_rows"]), ("2026-07-01", "2026-09-30", 0))
                card = (await client.call_tool("get_recipe_card", {**common, "product_id": DISH, "as_of": "вчера"})).structured_content
                self.assertEqual(card["as_of"], DAY)
                at = DAY + "T23:59:59.999+03:00"
                warehouse.overrides[paths.STOCK] = {"organizationId": ORG, "balanceAt": at, "items": []}
                stock = (await client.call_tool("get_live_stock", {**common, "as_of": "end_of_yesterday"})).structured_content
                self.assertEqual(stock["balanceAt"], at)

    async def test_http_diagnostic_and_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, patch("iiko_intelligence.mcp_server.database_path", return_value=Path(tmp)/"absent"), patch(
                "iiko_intelligence.cloud_mcp.configured", return_value=True):
            async with Client(server) as client:
                for tool in ("get_data_status", "get_capabilities"):
                    result = (await client.call_tool(tool, {})).structured_content
                    self.assertEqual((result["snapshot_status"], result["live_cloud"]["status"]), ("unavailable", "configured"))
                for status, body in [(400, {"correlationId": ORG, "error": "INVALID_BODY_JSON_FORMAT",
                                            "message": "private sample", "apiKey": "fixture-secret"}),
                                     (429, {}), (503, None)]:
                    raw = json.dumps(body).encode() if body is not None else b"private sample: unavailable"
                    exc = HTTPError("https://example.invalid", status, "", {"Retry-After": "12"}, io.BytesIO(raw))
                    with patch.object(sales, "build_opener") as opener, patch("iiko_intelligence.cloud_mcp.read_profile", return_value=PROFILE):
                        opener.return_value.open.side_effect = exc
                        result = (await client.call_tool("get_sales_summary", {"source": "live", "date_from": DAY, "date_to": DAY})).structured_content
                    self.assertEqual((result["http_status"], result["operation"], result["retry_after_seconds"]), (status, sales.TOKEN, 12))
                    self.assertNotIn("private sample", str(result))
                    self.assertNotIn("fixture-secret", str(result))
                    self.assertNotIn("totals", result)
                    if status == 400:
                        self.assertEqual((result["correlation_id"], result["iiko_code"]), (ORG, "INVALID_BODY_JSON_FORMAT"))
