from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from mcp import Client

from iiko_intelligence import cloud_paths as paths
from iiko_intelligence.cloud_profile import Profile
from iiko_intelligence.cloud_reports import CloudReports
from iiko_intelligence.cloud_sales import ORGANIZATIONS, TOKEN, _post
from iiko_intelligence.mcp_server import server

ORG, STORE, DISH, SALT, DOC, UNIT, OTHER = [str(UUID(int=i)) for i in range(1, 8)]
PROFILE = Profile(ORG, {"appId": "example", "clientSecret": "dummy-secret", "apiKey": "dummy-key"})
DAY = "2026-10-10"
AT = DAY + "T23:59:00+03:00"


class Restaurant:
    def __init__(self):
        self.overrides = {}
        self.calls = []

    def post(self, path, body, token):
        self.calls.append((path, body))
        if path in self.overrides:
            return self.overrides[path]
        if path == TOKEN:
            assert body == PROFILE.credentials and token is None
            return {"token": "fixture"}
        assert token == "fixture"
        if path == ORGANIZATIONS:
            return {"organizations": [{"id": ORG}]}
        if path == paths.PRODUCTS:
            condition = body["filters"][0]
            if condition["op"] == "in":
                ids = condition["values"]
            else:
                assert condition == {"field": "name", "op": "like", "value": "Цезарь"}
                ids = [DISH, OTHER]
            rows = [{"productId": i, "name": {DISH: "Цезарь", SALT: "Соль", OTHER: "Цезарь с креветкой"}[i],
                     "amountUnitId": UNIT} for i in ids]
            return {"items": rows, "totalCount": len(rows), "offset": 0}
        if path == paths.UNITS:
            return {"entities": [{"id": UNIT, "name": "кг"}]}
        if path == paths.STORES:
            return {"stores": [{"id": STORE, "name": "Кухня", "organizationId": ORG, "address": "private field"}]}
        assert body["organizationId"] == ORG
        if path == paths.SUPPLIER:
            assert body["counteragentId"] == OTHER
            return {"id": OTHER, "name": "Поставщик", "email": "private field"}
        if path == paths.RECIPE:
            assert body["date"] == DAY and body["productId"] == DISH
            view = body["view"]
            chart = {"productId": DISH, "dateFrom": DAY}
            if view == "assembled":
                chart.update(assembledQuantity=2, technologyDescription="Смешать", items=[
                    {"productId": SALT, "quantityIn": Decimal("0.03"), "quantityMiddle": Decimal("0.03")}])
            else:
                chart.update(assembledAmount=2, items=[{"productId": SALT, "amount": Decimal("0.05")},
                    {"productId": SALT, "amount": Decimal("0.01")},
                    {"productId": OTHER, "amount": 1, "storeSpecification": {"organizationIds": [ORG], "inverse": True}}])
            return {view: chart, "view": view}
        if path == paths.COST:
            assert body["date"] == AT and all(r["storeId"] == STORE for r in body["items"])
            return {"items": [{**r, "isEvaluative": False, "costPrice": Decimal("10.25") * Decimal(str(r["quantityFactor"]))}
                              for r in body["items"]], "problemItems": []}
        if path == paths.STOCK:
            assert body["storeIds"] == [STORE] and body["productIds"] == [SALT] and body["shouldIncludeDetails"]
            return {"organizationId": ORG, "currencyCode": "RUB", "balanceAt": AT, "totalCount": 1, "offset": 0,
                    "items": [{"organizationId": ORG, "productId": SALT, "storeId": STORE, "quantity": -2,
                               "costSum": -20, "details": {"productName": "Соль", "storeName": "Кухня", "amountUnitName": "кг"}}]}
        for kind, (listing, detail) in paths.DOCUMENTS.items():
            if path == listing:
                assert body == {"organizationId": ORG, "from": DAY, "to": DAY, "returnFullDetails": False, "storeIds": [STORE]}
                return [{"documentId": DOC, "date": DAY, "number": "001", "processed": True, "deleted": False,
                         "assignedStores": [STORE], "storageFrom": STORE, "sum": 15, "userCreated": "private field"}]
            if path == detail:
                assert body == {"organizationId": ORG, "documentId": DOC}
                lines = [{"product": SALT, "amount": 2, "amountUnit": UNIT}]
                if kind == "invoice":
                    lines[0].update(price=3, sum=6, vatPercent=0)
                if kind == "stocktake":
                    lines[0]["currentActualAmount"] = 3
                return {"documentId": DOC, "status": "PROCESSED", "date": DAY, "items": lines, "itemsFirstStep": lines,
                        "counteragent": OTHER if kind == "invoice" else None}
        raise AssertionError(path)


class LiveMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_queries_through_mcp(self):
        api = Restaurant()
        with patch("iiko_intelligence.cloud_mcp.read_profile", return_value=PROFILE), patch(
                "iiko_intelligence.cloud_mcp.CloudReports", side_effect=lambda profile: CloudReports(profile, api.post)):
            async with Client(server) as client:
                async def call(tool, **args):
                    reply = await client.call_tool(tool, args)
                    self.assertFalse(reply.is_error, reply)
                    data = reply.structured_content
                    self.assertEqual((data["status"], data["organizationId"]), ("ok", ORG), data)
                    self.assertNotIn("dummy-", str(data))
                    self.assertNotIn("private field", str(data))
                    return data

                found = await call("find_iiko_products", query="Цезарь")
                self.assertEqual(len(found["rows"]), 2)
                self.assertEqual((await call("list_iiko_stores"))["rows"][0]["name"], "Кухня")
                card = await call("get_recipe_card", product_id=DISH, as_of=DAY)
                self.assertEqual((card["chart"]["technologyDescription"], card["rows"][0]["productName"],
                                  card["rows"][0]["unitName"]), ("Смешать", "Соль", "кг"))
                args = {"product_id": DISH, "store_id": STORE, "as_of": AT}
                cost = await call("get_item_cost", **args, quantity=2)
                self.assertEqual(Decimal(str(cost["rows"][0]["costPrice"])), Decimal("20.50"))
                recipe_cost = await call("get_recipe_cost", **args)
                self.assertEqual(Decimal(recipe_cost["cost_for_recipe_yield"]), Decimal("0.615"))
                self.assertEqual(Decimal(recipe_cost["cost_per_output_unit"]), Decimal("0.3075"))
                stock = await call("get_live_stock", product_id=SALT, store_id=STORE, as_of=AT)
                self.assertEqual((stock["currencyCode"], stock["rows"][0]["quantity"]), ("RUB", -2))
                for kind, tool in (("invoice", "get_incoming_invoices"), ("writeoff", "get_writeoff_acts"), ("stocktake", "get_inventory_counts")):
                    headers = await call(tool, date_from=DAY, date_to=DAY, store_id=STORE)
                    self.assertEqual(headers["rows"][0]["number"], "001")
                    detail = await call("get_iiko_document", kind=kind, document_id=DOC)
                    self.assertEqual(detail["rows"][0]["productName"], "Соль")
                    if kind == "invoice":
                        self.assertEqual(detail["rows"][0]["price"], 3)
                        self.assertEqual(detail["header"]["supplierName"], "Поставщик")
                    if kind == "stocktake":
                        self.assertEqual(detail["rows"][0]["currentActualAmount"], 3)
                        first = await call("get_iiko_document", kind=kind, document_id=DOC, section="itemsFirstStep")
                        self.assertEqual(first["rows"][0]["amount"], 2)

    async def test_no_profile_and_api_denial(self):
        async with Client(server) as client:
            args = {"product_id": DISH, "as_of": DAY}
            with patch("iiko_intelligence.cloud_mcp.read_profile", return_value=None), patch(
                    "iiko_intelligence.cloud_mcp.configured", return_value=False):
                reply = await client.call_tool("get_recipe_card", args)
                self.assertEqual(reply.structured_content["status"], "not_configured")
            with patch("iiko_intelligence.cloud_mcp.read_profile", return_value=PROFILE), patch(
                    "iiko_intelligence.cloud_mcp.CloudReports", side_effect=ValueError("HTTP 403")):
                reply = await client.call_tool("get_recipe_card", args)
                self.assertEqual(reply.structured_content["status"], "unavailable")
                self.assertNotIn("rows", reply.structured_content)


class ContractFailures(unittest.TestCase):
    def setUp(self):
        self.transport = Restaurant()
        self.api = CloudReports(PROFILE, self.transport.post)

    def test_single_org_and_read_only_allowlist(self):
        self.transport.overrides[ORGANIZATIONS] = {"organizations": [{"id": ORG}, {"id": OTHER}]}
        with self.assertRaisesRegex(ValueError, "only"):
            CloudReports(PROFILE, self.transport.post)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            _post("/api/inventory/v1/incoming_invoice/create", {}, "unused")

    def test_scoped_stock_and_date_boundaries(self):
        self.transport.overrides[paths.STOCK] = {"organizationId": OTHER, "items": []}
        with self.assertRaisesRegex(ValueError, "organization"):
            self.api.stock(AT, STORE, SALT, 100, 0)
        with self.assertRaisesRegex(ValueError, "offset"):
            self.api.cost(SALT, STORE, DAY, 1)
        with self.assertRaisesRegex(ValueError, "366 days"):
            self.api.documents("invoice", DAY, "2027-12-31", None, 10, 0)
        listing = paths.DOCUMENTS["stocktake"][0]
        self.transport.overrides[listing] = [{"documentId": DOC, "date": DAY, "storageFrom": OTHER}]
        with self.assertRaisesRegex(ValueError, "warehouse"):
            self.api.documents("stocktake", DAY, DAY, STORE, 30, 0)

    def test_reports_missing_recipe_units_or_prices_honestly(self):
        self.transport.overrides[paths.RECIPE] = {"view": "assembled", "assembled": None}
        self.assertEqual(self.api.recipe(DISH, DAY, 10, 0)["status"], "no_chart")
        del self.transport.overrides[paths.RECIPE]
        self.transport.overrides[paths.COST] = {"items": [], "problemItems": [{"productId": SALT, "storeId": STORE, "quantityFactor": 1}]}
        report = self.api.recipe_cost(DISH, STORE, AT, None, 50, 0)
        self.assertEqual(report["status"], "unpriced")
        self.assertIsNone(report["cost_for_recipe_yield"])
        self.transport.overrides[paths.UNITS] = {"entities": []}
        self.assertTrue(self.api.recipe(DISH, DAY, 50, 0)["warnings"])

    def test_no_partial_totals_and_ambiguous_sizes(self):
        chart = {"productId": DISH, "assembledAmount": 2, "items": [
            {"productId": SALT, "amount": 2, "productSizeSpecification": OTHER}, {"productId": OTHER, "amount": 1}]}
        self.transport.overrides[paths.RECIPE] = {"view": "prepared", "prepared": chart}
        self.assertEqual(self.api.recipe_cost(DISH, STORE, AT, None, 1, 0)["status"], "select_size")
        report = self.api.recipe_cost(DISH, STORE, AT, OTHER, 1, 0)
        self.assertEqual(report["next_offset"], 1)
        self.assertEqual(Decimal(report["cost_for_recipe_yield"]), Decimal("30.75"))
        self.assertEqual(len(report["rows"]), 1)
        self.transport.overrides[paths.COST] = {"items": [{"productId": SALT, "storeId": STORE,
                                                           "quantityFactor": 1, "costPrice": 0}]}
        report = self.api.recipe_cost(DISH, STORE, AT, OTHER, 1, 0)
        self.assertIsNone(report["cost_per_output_unit"])
        self.assertEqual(report["missing_or_ambiguous_costs"], [OTHER])
