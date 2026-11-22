from datetime import date
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from mcp import Client

from iiko_intelligence import cloud_paths as paths, cloud_sales as s
from iiko_intelligence.cloud_checks import check_access
from iiko_intelligence.cloud_profile import Profile
from iiko_intelligence.cloud_reports import CloudReports
from iiko_intelligence.live_sales import LiveSales, windows
from iiko_intelligence.mcp_server import server

ORG = "00000000-0000-0000-0000-000000000123"
PROFILE = Profile(ORG, {"appId": "app", "clientSecret": "fixture", "apiKey": "key"})
FIRST, SECOND = "2026-09-30", "2026-10-01"


class Restaurant:
    def __init__(self):
        self.columns = [s.SALES, s.ORDERS, s.DAY, s.DEPARTMENT, s.DISH_ID, s.DISH_NAME, s.QUANTITY]
        self.responses = {}
        self.calls = []
        self.data = {FIRST: [("1", "Чай", 3, 60), ("2", "Суп", 1, 70)],
                     SECOND: [("1", "Чай", 4, 80), ("2", "Суп", 2, 140)]}

    def post(self, path, body, token):
        self.calls.append((path, body))
        if path in self.responses:
            result = self.responses[path]
            if isinstance(result, Exception):
                raise result
            return result
        if path == s.TOKEN:
            assert body == PROFILE.credentials and token is None
            return {"token": "restaurant"}
        assert token == "restaurant"
        if path == s.ORGANIZATIONS:
            return {"organizations": [{"id": ORG, "name": "Кафе", "currencyIsoName": "KZT", "private": "address"}]}
        if path == s.COLUMNS:
            return [{"name": key, "canFilter": True, "canGroup": True, "canAggregate": True} for key in self.columns]
        assert path == s.REPORT
        period, *outlet = body["filters"]
        assert body["reportType"] == "SALES" and body["buildSummary"] is False
        kind = period["periodType"]
        if kind == "CUSTOM":
            assert period["includeLow"] and period["includeHigh"]
            days = [day for day in sorted(self.data) if period["from"] <= day <= period["to"]]
        else:
            days = [FIRST if kind == "YESTERDAY" else SECOND]
            assert "from" not in period and "to" not in period
        rows = []
        for day in days:
            detail = self.data.get(day, [])
            if not detail:
                continue
            base = {s.DAY: day}
            if outlet:
                base[s.DEPARTMENT] = outlet[0]["values"][0]
            if s.DISH_ID in body["groupByRowFields"]:
                for key, name, qty, amount in detail:
                    rows.append({**base, s.DISH_ID: key, s.DISH_NAME: name, s.QUANTITY: qty, s.SALES: amount})
            else:
                row = {**base, s.SALES: sum(values[-1] for values in detail)}
                if s.ORDERS in body["aggregateFields"]:
                    row[s.ORDERS] = 2
                rows.append(row)
        return {"data": rows}

    def api(self):
        return CloudReports(PROFILE, self.post)


class Reporting(TestCase):
    def setUp(self):
        self.restaurant = Restaurant()

    def test_relative_dates_outlet_stable_drilldown_and_rank(self):
        sales = LiveSales(self.restaurant.api(), "Кафе")
        result = sales.top("yesterday", "yesterday", "quantity", 1)
        self.assertEqual(result["period"]["from"], FIRST)
        self.assertEqual((result["rows"][0]["name"], result["rows"][0]["quantity"], result["detail"]["total_rows"]),
                         ("Чай", "3.000", 2))
        reports = [body for path, body in self.restaurant.calls if path == s.REPORT]
        self.assertEqual(reports[0]["filters"], [{"name": s.DAY, "filterType": "DateRange", "periodType": "YESTERDAY"},
            {"name": s.DEPARTMENT, "filterType": "IncludeValues", "values": ["Кафе"]}])
        self.assertEqual(reports[1]["filters"][0]["from"], FIRST)
        self.assertEqual(sales.top(FIRST, SECOND, "sales", 1)["rows"][0]["sales_after_discounts"], "210.00")
        result = sales.compare(FIRST, FIRST, SECOND, SECOND, 1)
        self.assertEqual((result["change"], result["other_dishes_change"], result["change_percent"],
                          result["overlap_days"]), ("90.00", "20.00", "69.23", 0))

    def test_missing_fields_do_not_hide_revenue(self):
        self.restaurant.columns = [s.SALES, s.DAY]
        sales = LiveSales(self.restaurant.api())
        report = sales.summary("yesterday", "yesterday")
        self.assertEqual(report["totals"], {"sales_after_discounts": "130.00"})
        self.assertEqual(report["currency"], "KZT")
        self.assertEqual(report["restaurant"], {"name": "Кафе", "id": ORG})
        self.assertEqual(sales.compare(FIRST, FIRST, SECOND, SECOND, 1)["status"], "partial")
        with self.assertRaisesRegex(ValueError, "required OLAP fields"):
            sales.top(FIRST, FIRST, "sales", 1)

    def test_no_rows_and_genuine_zero(self):
        sales = LiveSales(self.restaurant.api())
        self.restaurant.data = {}
        report = sales.summary("yesterday", "yesterday")
        self.assertEqual((report["status"], report["totals"]["sales_after_discounts"], report["period"]["from"]),
                         ("no_data", None, None))
        self.assertEqual(sales.compare(FIRST, FIRST, SECOND, SECOND, 1)["change"], None)
        self.restaurant.data[FIRST] = [("1", "Возврат", 1, 0)]
        report = sales.summary("yesterday", "yesterday")
        self.assertEqual((report["status"], report["totals"]["sales_after_discounts"]), ("ok", "0.00"))

    def test_invalid_scope_rows_and_reconciliation(self):
        api = self.restaurant.api()
        sales = LiveSales(api)
        for rows in [[{s.DAY: FIRST, s.SALES: 1, s.ORDERS: 1}] * 2,
                     [{s.DAY: SECOND, s.SALES: 1, s.ORDERS: 1}]]:
            self.restaurant.responses[s.REPORT] = {"data": rows}
            with self.assertRaises(ValueError):
                sales.summary(FIRST, FIRST)
        self.restaurant.responses.pop(s.REPORT)
        period = sales.period("yesterday", "yesterday")
        self.restaurant.data[FIRST][0] = ("1", "Чай", 3, 55)
        with self.assertRaisesRegex(ValueError, "differ"):
            sales.dishes(period)
        self.restaurant.responses[s.ORGANIZATIONS] = {"organizations": [{"id": ORG}, {"id": "other"}]}
        with self.assertRaisesRegex(ValueError, "only"):
            self.restaurant.api()

    def test_diagnostic_samples_dish_warehouse_and_preserves_empty_recipe_outcome(self):
        api = Mock()
        dish, store = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
        api.products.return_value = {"rows": [{"productId": dish}]}
        api.stores.return_value = {"rows": [{"id": "old", "deleted": True}, {"id": store, "deleted": False}]}
        api.recipe.return_value = {"status": "no_chart"}
        api.cost.return_value = api.recipe_cost.return_value = {"status": "unpriced"}
        at = FIRST + "T12:00:00+03:00"
        result = check_access(api, "recipes", at, None, None)
        self.assertEqual(result["checks"]["get_recipe_card"]["status"], "no_chart")
        self.assertEqual(result["checks"]["get_recipe_cost"]["status"], "unpriced")
        api.recipe.assert_called_once_with(dish, FIRST, 1, 0)
        api.cost.assert_called_once_with(dish, store, at, 1)
        api.recipe_cost.assert_called_once_with(dish, store, at, None, 1, 0)

    def test_windows_and_denied_access_independent(self):
        self.assertEqual([w.start for w in windows("2026-09-01", "2026-10-03")],
                         [date(2026, 9, 1), date(2026, 10, 2)])
        for bounds in [(FIRST, "yesterday"), ("today", "yesterday"), (SECOND, FIRST), ("2020-01-01", FIRST)]:
            with self.assertRaises(ValueError):
                windows(*bounds)
        self.restaurant.responses.update({s.COLUMNS: ValueError("HTTP 403"), paths.PRODUCTS: {"items": []},
            paths.STORES: {"stores": []}, paths.STOCK: {"items": [], "balanceAt": FIRST + "T12:00:00+03:00",
            "organizationId": ORG}, **{listing: [] for listing, _ in paths.DOCUMENTS.values()}})
        result = check_access(self.restaurant.api(), "all", FIRST + "T12:00:00+03:00", None, None)
        self.assertEqual(result["status"], "partial")
        checks = result["checks"]
        self.assertEqual(checks["get_sales_summary"], {"status": "unavailable", "reason": "HTTP 403"})
        for method in ("find_iiko_products", "list_iiko_stores", "get_live_stock", "get_incoming_invoices"):
            self.assertEqual(checks[method]["status"], "no_data")
        self.assertEqual(checks["get_iiko_document.invoice"]["status"], "not_checked")
        self.assertEqual(checks["get_recipe_cost"]["status"], "needs_input")


class Protocol(IsolatedAsyncioTestCase):
    async def test_online_answer_denial_and_missing_connection(self):
        upstream = Restaurant()
        with patch("iiko_intelligence.cloud_mcp.configured", return_value=True), \
                patch("iiko_intelligence.cloud_mcp.read_profile", return_value=PROFILE), \
                patch("iiko_intelligence.cloud_mcp.CloudReports", side_effect=lambda profile: upstream.api()):
            async with Client(server) as client:
                async def call(tool, **args):
                    result = await client.call_tool(tool, args)
                    self.assertFalse(result.is_error)
                    return result.structured_content
                report = await call("get_sales_summary", date_from="yesterday", date_to="yesterday")
                self.assertEqual((report["organizationId"], report["dataset_mode"], report["totals"]["average_order"]),
                                 (ORG, "live", "65.00"))
                self.assertEqual(report["totals"]["orders"], 2)
                access = await call("check_live_access", group="sales")
                self.assertEqual(access["checks"]["get_top_dishes"]["status"], "ok")
                upstream.responses[s.REPORT] = ValueError("HTTP 403")
                report = await call("get_sales_summary", date_from=FIRST, date_to=SECOND)
                self.assertEqual((report["status"], report["reason"]), ("unavailable", "HTTP 403"))
                self.assertNotIn("totals", report)
                with patch("iiko_intelligence.cloud_mcp.read_profile", return_value=None), \
                        patch("iiko_intelligence.cloud_mcp.configured", return_value=False):
                    report = await call("get_sales_summary", date_from="yesterday", date_to="yesterday")
                    self.assertEqual(report["status"], "not_configured")
