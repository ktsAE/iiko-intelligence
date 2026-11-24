import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from mcp import Client

from iiko_intelligence import records
from iiko_intelligence.report_calendar import RestaurantCalendar
from iiko_intelligence.cloud_profile import Profile
from iiko_intelligence.cloud_sales import CloudHTTPError, _post, write_record
from iiko_intelligence.mcp_server import server
from iiko_intelligence.record_catalog import RESOURCES, json_text


ORG, DOC, SUPPLIER, ITEM, STORE, REQ, OTHER = [str(UUID(int=i)) for i in range(1, 8)]
DATE = "2026-10-10"
LINE = {"num": 1, "product": ITEM, "store": STORE, "amount": 10, "price": Decimal("4.331"), "sum": Decimal("43.310")}
HEADER = {"number": "00017", "date": DATE + "T12:00:00", "counteragent": SUPPLIER, "documentId": DOC,
          "comment": "original", "revision": 22, "status": "NEW", "isEditable": True,
          "items": [LINE, dict(LINE, num=2, sum=Decimal("43.310"))]}


class Restaurant:
    org = ORG

    def __init__(self):
        self.record = deepcopy(HEADER)
        self.writes = []
        self.error = None

    def calendar(self, zone):
        return RestaurantCalendar(lambda: ["Europe/Moscow"], zone, datetime(2026, 10, 10, 22, tzinfo=timezone.utc))

    def read(self, path, body):
        if path.endswith("/get"):
            assert body == {"documentId": DOC, "organizationId": ORG}
            return deepcopy(self.record)
        assert path.endswith("/list") and body["organizationId"] == ORG
        return [deepcopy(self.record)]

    def write(self, path, body):
        self.writes.append((path, body))
        if self.error:
            raise self.error
        self.record.update(deepcopy(body))
        return {"documentId": body["documentId"], "documentNumber": self.record["number"]}


class TestRecords(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(records.config, "PROJECT_ROOT", Path(tmp)))
        self.api = Restaurant()
        self.enterContext(patch("iiko_intelligence.cloud_mcp.read_profile", return_value=Profile(ORG, {})))
        self.enterContext(patch("iiko_intelligence.cloud_mcp.CloudReports", return_value=self.api))

    async def call(self, tool, **args):
        async with Client(server) as client:
            reply = await client.call_tool(tool, args)
            self.assertFalse(reply.is_error, reply)
            return reply.structured_content

    async def test_discover_list_edit_and_retry(self):
        result = await self.call("describe_iiko_records")
        self.assertEqual(len(result["record_types"]), 24)
        result = await self.call("describe_iiko_records", record_type="incoming_invoice", field="items")
        self.assertIn("store", result["required"])
        listing = await self.call("list_iiko_records", record_type="incoming_invoice", criteria={"from": DATE, "to": DATE})
        self.assertEqual(listing["total_rows"], 1)
        args = {"record_type": "incoming_invoice", "record_id": DOC}
        data = await self.call("get_iiko_record", **args)
        edit = {**args, "request_id": REQ, "expected_version": data["version"], "changes": {"comment": "reviewed"},
                "row_edits": [{"field": "items", "action": "edit", "match": {"num": 1},
                               "values": {"amount": 2, "sum": 8.662}},
                              {"field": "items", "action": "append", "values": {"num": 3, "store": STORE, "product": ITEM, "amount": 1}}]}
        result = await self.call("update_iiko_record", **edit)
        self.assertEqual((result["status"], result["record_id"]), ("updated", DOC))
        self.assertEqual((await self.call("update_iiko_record", **edit)), result)
        path, sent = self.api.writes[0]
        self.assertEqual(len(self.api.writes), 1)
        self.assertTrue(path.endswith("/incoming_invoice/update"))
        self.assertEqual(sent["items"][1], LINE | {"num": 2})
        self.assertEqual((sent["items"][0]["amount"], len(sent["items"]), sent["number"]), (2, 3, "00017"))
        self.assertNotIn("revision", sent)
        self.assertNotIn("status", sent)
        self.assertEqual(json.loads(json_text(sent), parse_float=Decimal)["items"][1]["price"], Decimal("4.331"))
        readback = await self.call("get_iiko_record", **args)
        self.assertEqual(readback["record"]["comment"], "reviewed")
        conflict = await self.call("update_iiko_record", **dict(edit, changes={"comment": "different"}))
        self.assertEqual(conflict["status"], "unavailable")
        stale = await self.call("update_iiko_record", **dict(edit, request_id=OTHER))
        self.assertIn("changed", stale["reason"])
        self.assertEqual(len(self.api.writes), 1)

    async def test_yesterday_document_list_pins_dates_for_next_page(self):
        with patch.object(self.api, "read", return_value=[HEADER, dict(HEADER, documentId=OTHER)]) as read:
            out = await self.call("list_iiko_records", record_type="incoming_invoice",
                                  criteria={"from": "за фчера", "to": "yesterday", "storeIds": [STORE]}, limit=1)
            self.assertEqual((out["date_from"], out["date_to"]), (DATE, DATE))
            self.assertEqual(read.call_args.args[1], {"from": DATE, "to": DATE, "storeIds": [STORE], "organizationId": ORG})
            following = out["next_page_arguments"]
            self.assertEqual(following["criteria"], {"from": DATE, "to": DATE, "storeIds": [STORE]})
            self.assertEqual((await self.call("list_iiko_records", **following))["next_offset"], None)

    async def test_create_idempotency_scope_and_unknown_delivery(self):
        values = {"counteragent": SUPPLIER, "date": DATE, "items": [{"num": 1, "amount": 1, "product": ITEM, "store": STORE}]}
        args = {"record_type": "incoming_invoice", "values": values, "request_id": REQ}
        for invalid in [dict(values, organizationId=OTHER), dict(values, posted=True), dict(values, items=[{"price": 8}])]:
            self.assertEqual((await self.call("create_iiko_record", **dict(args, values=invalid)))["status"], "unavailable")
        self.assertEqual(self.api.writes, [])
        result = await self.call("create_iiko_record", **args)
        again = await self.call("create_iiko_record", **args)
        self.assertEqual((again, len(self.api.writes), result["status"]), (result, 1, "created"))
        self.api.error = OSError("timeout")
        args["request_id"] = OTHER
        unknown = await self.call("create_iiko_record", **args)
        self.assertEqual(unknown["status"], "outcome_unknown")
        self.api.error = None
        self.assertEqual((await self.call("create_iiko_record", **args))["status"], "outcome_unknown")
        self.assertEqual(len(self.api.writes), 2)

    async def test_denial_and_ambiguous_table_changes(self):
        base = {"record_type": "incoming_invoice", "record_id": DOC, "request_id": REQ}
        for match, values in [({"product": ITEM}, {"amount": 2}), ({"num": 99}, {"amount": 2}), ({"num": 1}, {"price": 5})]:
            out = await self.call("update_iiko_record", **base, row_edits=[{"field": "items", "action": "edit",
                                                                            "match": match, "values": values}])
            self.assertEqual(out["status"], "unavailable")
        self.assertEqual(self.api.writes, [])
        self.api.error = CloudHTTPError("invoice/update", 403)
        result = await self.call("update_iiko_record", **base, changes={"comment": "updated"})
        self.assertEqual(result["http_status"], 403)
        self.api.error = None
        result = await self.call("update_iiko_record", **base, changes={"comment": "updated"})
        self.assertEqual(result["status"], "updated")

    async def test_read_only_paths_and_diagnostics_never_write(self):
        with self.assertRaises(ValueError):
            _post(RESOURCES["incoming_invoice"]["base"] + "/create", {}, "fixture")
        for suffix in ("delete", "post", "unpost"):
            with self.assertRaises(ValueError):
                write_record(RESOURCES["writeoff"]["base"] + "/" + suffix, {}, "fixture")
        self.api.record["documentId"] = OTHER
        result = await self.call("update_iiko_record", record_type="incoming_invoice", record_id=DOC, request_id=REQ,
                                 changes={"comment": "new"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.api.writes, [])


class CatalogAndPrices(TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(records.config, "PROJECT_ROOT", Path(tmp)))
        self.api = Mock(org=ORG)

    def test_product_patch_preserves_other_fields_and_uses_exact_selected_id(self):
        row = {"productId": ITEM, "name": "Old name", "code": "002", "amountUnitId": STORE, "type": "DISH",
               "estimatedPurchasePrice": Decimal("14.22")}
        self.api.read.return_value = {"items": [row], "totalCount": 1, "offset": 0}
        self.api.write.return_value = {"productId": ITEM}
        out = records.write_operation(self.api, "product", "update", ITEM, {"name": "New name"}, [], {}, None, REQ)
        self.assertEqual(out["status"], "updated")
        body = self.api.write.call_args.args[1]
        self.assertEqual(body, {"name": "New name", "type": "DISH", "code": "002", "amountUnitId": STORE, "productId": ITEM})
        self.assertEqual(self.api.read.call_args.args[1]["filters"][0]["value"], ITEM)

    def test_every_supported_create_uses_the_pinned_endpoint_and_org_scope(self):
        def sample(model):
            model = records.resolve(model)
            kind = model.get("type")
            if isinstance(kind, list):
                return None if "null" in kind else sample(dict(model, type=kind[0]))
            if kind == "object":
                return {k: sample(model["properties"][k]) for k in model.get("required", [])}
            if kind == "array":
                return [sample(model["items"])]
            if kind in ("integer", "number"):
                return 1
            if kind == "boolean":
                return False
            return {"uuid": ITEM, "date": DATE, "date-time": DATE + "T12:00:00Z"}.get(model.get("format"), "fixture")

        for i, (kind, entry) in enumerate(RESOURCES.items(), 200):
            with self.subTest(kind=kind):
                schema = records.DEFINITIONS[entry["create"]]
                payload = sample(schema)
                payload.pop("organizationId", None)
                self.api.write.side_effect = lambda path, body: {entry["id"]: body.get(entry["id"], ITEM)}
                result = records.write_operation(self.api, kind, "create", None, payload, [], {}, None, str(UUID(int=i)))
                path, body = self.api.write.call_args.args
                self.assertEqual((result["status"], path), ("created", entry["base"] + "/create"))
                if "organizationId" in schema["properties"]:
                    self.assertEqual(body["organizationId"], ORG)

    def test_price_list_single_row_edit_preserves_remaining_prices(self):
        self.api.read.return_value = {"items": [{"priceListId": DOC, "dateFrom": DATE, "dateTo": None,
                                                 "nativeProductId": item, "supplierProductId": item, "price": 8}
                                                for item in (ITEM, OTHER)]}
        self.api.write.return_value = {"priceListId": DOC}
        edits = [{"action": "edit", "field": "items", "match": {"nativeProductId": ITEM}, "values": {"price": 9}}]
        result = records.write_operation(self.api, "supplier_price_list", "update", DOC, {}, edits,
                                         {"counteragentId": SUPPLIER}, None, REQ)
        self.assertEqual(result["status"], "updated")
        path, body = self.api.write.call_args.args
        self.assertTrue(path.endswith("/pricelist/update"))
        self.assertEqual([row["price"] for row in body["items"]], [9, 8])
        self.assertEqual(body["dateFrom"], DATE)
        self.assertNotIn("priceListId", body["items"][0])

    def test_concurrent_retry_sends_one_request(self):
        self.api.write.return_value = {"id": ITEM}
        def submit():
            return records.write_operation(self.api, "product_category", "create", None, {"name": "Drinks"}, [], {}, None, REQ)
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(self.api.write.call_count, 1)
