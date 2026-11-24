from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from mcp import Client

from iiko_intelligence import cloud_paths as paths, cloud_sales as sales
from iiko_intelligence.cloud_profile import Profile
from iiko_intelligence.mcp_server import server
from iiko_intelligence.report_calendar import RestaurantCalendar


DAY = "DateTime.DateTyped"
START, END = "2026-10-20", "2026-10-22"
ORG = "00000000-0000-0000-0000-000000000123"


class Transactions(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.date_field = DAY
        self.denied = set()
        self.calls = []
        self.rows = [{DAY: START, "Reference": "A", "Movement": "received", "Amount": Decimal("410.11")},
                     {DAY: END, "Reference": "B", "Movement": "applied", "Amount": -15}]
        self.presets = [{"presetId": "report-1", "name": "Предоплаты банкетов"}]
        self.saved_rows = [{"Reference": "A", "Amount": Decimal("410.11")}, {"Reference": "B", "Amount": -15}]
        self.org = ORG
        self.restaurant = {"id": ORG}
        self.currency = "RUB"
        self.enterContext(patch("iiko_intelligence.cloud_mcp.read_profile", return_value=Profile(ORG, {})))
        self.enterContext(patch("iiko_intelligence.cloud_mcp.CloudReports", return_value=self))

    def calendar(self, zone):
        return RestaurantCalendar(lambda: ["Europe/Moscow"], zone, datetime(2026, 10, 22, 15, tzinfo=timezone.utc))

    def terminal_timezones(self):
        return {"Europe/Moscow"}

    def read(self, path, payload):
        self.calls.append((path, payload))
        if path in self.denied:
            raise sales.CloudHTTPError(path, 403)
        if path == sales.COLUMNS:
            self.assertEqual(payload, {"reportType": "TRANSACTIONS"})
            return [{"name": name, "displayName": label, "canFilter": True, "canGroup": name != "Amount",
                     "canAggregate": name == "Amount", "type": "MONEY" if name == "Amount" else "STRING"}
                    for name, label in [(self.date_field, "Дата"), ("Account", "Счёт"), ("Reference", "Основание"),
                                        ("Movement", "Операция"), ("Amount", "Сумма")]]
        if path == sales.REPORT:
            self.assertEqual(payload["reportType"], "TRANSACTIONS")
            return {"data": self.rows, "summary": [{"Amount": 9999}]}
        if path == paths.PRESETS:
            self.assertEqual(payload, {})
            return self.presets
        self.assertEqual(path, paths.PRESET_REPORT)
        return {"data": self.saved_rows, "summary": [{"Amount": 9999}]}

    async def call(self, tool, **args):
        async with Client(server) as client:
            reply = await client.call_tool(tool, args)
        self.assertFalse(reply.is_error, reply)
        return reply.structured_content

    def args(self):
        return {"date_from": START, "date_to": END, "group_fields": ["Reference", "Movement"], "amount_fields": ["Amount"],
                "filters": {"Account": ["Confirmed account"]}, "limit": 1}

    async def test_field_discovery_and_transaction_pages(self):
        fields = await self.call("list_transaction_fields", query="счёт")
        self.assertEqual([r["name"] for r in fields["rows"]], ["Account"])
        result = await self.call("get_transaction_report", **self.args())
        body = self.calls[-1][1]
        self.assertEqual(body["filters"], [{"filterType": "DateRange", "name": DAY, "from": START, "to": END,
                                            "periodType": "CUSTOM", "includeLow": True, "includeHigh": True},
                                           {"filterType": "IncludeValues", "name": "Account", "values": ["Confirmed account"]}])
        self.assertEqual(body["groupByRowFields"], [DAY, "Reference", "Movement"])
        self.assertFalse(body["buildSummary"])
        self.assertEqual((result["status"], result["total_rows"], result["organizationId"]), ("ok", 2, ORG))
        self.rows.reverse()
        second = await self.call("get_transaction_report", **result["next_page_arguments"])
        self.assertEqual(second["next_offset"], None)
        self.assertEqual({r["Amount"] for r in result["rows"] + second["rows"]}, {"410.11", "-15"})
        self.rows[0]["Amount"] = 17
        changed = await self.call("get_transaction_report", **result["next_page_arguments"])
        self.assertEqual(changed["status"], "unavailable")
        self.assertNotIn("rows", changed)

    async def test_saved_report_retains_configuration_and_includes_last_day(self):
        listing = await self.call("list_saved_iiko_reports", query="Предоплат")
        self.assertEqual(listing["rows"], self.presets)
        self.denied.add(sales.COLUMNS)
        result = await self.call("get_saved_iiko_report", preset_id="report-1", date_from=START, date_to="сегодня", limit=1)
        self.assertEqual(self.calls[-1], (paths.PRESET_REPORT, {"presetId": "report-1", "dateFrom": START, "dateTo": "2026-10-23"}))
        self.assertEqual((result["date_to"], result["next_page_arguments"]["date_to"]), (END, END))
        second = await self.call("get_saved_iiko_report", **result["next_page_arguments"])
        self.assertEqual((second["total_rows"], second["next_offset"]), (2, None))
        self.assertNotIn("9999", str(result))
        self.assertNotIn(sales.REPORT, [p for p, _ in self.calls])

    async def test_no_data_denial_and_invalid_fields_do_not_yield_a_balance(self):
        self.rows = []
        empty = await self.call("get_transaction_report", **self.args())
        self.assertEqual((empty["status"], empty["rows"]), ("no_data", []))
        for changes in [{"amount_fields": ["Unknown"]}, {"group_fields": ["Amount"]}, {"filters": {DAY: [START]}},
                        {"date_from": "2025-01-01"}]:
            count = sum(p == sales.REPORT for p, _ in self.calls)
            failed = await self.call("get_transaction_report", **(self.args() | changes))
            self.assertEqual(failed["status"], "unavailable")
            self.assertEqual(sum(p == sales.REPORT for p, _ in self.calls), count)
        self.denied.add(sales.REPORT)
        check = await self.call("check_live_access", group="transactions", as_of=END + "T12:15:00+03:00", preset_id="report-1")
        self.assertEqual(check["checks"]["get_transaction_report"]["http_status"], 403)
        self.assertEqual(check["checks"]["get_saved_iiko_report"]["status"], "ok")
        self.assertNotIn("410.11", str(check))

    async def test_timestamp_fallback_covers_last_evening_and_31_day_boundary(self):
        self.date_field = "DateTime.Typed"
        self.rows = [{self.date_field: END + "T23:59:59", "Reference": "A", "Movement": "refund", "Amount": "1.25"}]
        report = await self.call("get_transaction_report", **self.args())
        self.assertEqual(report["rows"][0]["Amount"], "1.25")
        f = self.calls[-1][1]["filters"][0]
        self.assertEqual((f["to"], f["includeHigh"]), ("2026-10-23", False))
        self.rows = []
        result = await self.call("get_transaction_report", **(self.args() | {"date_from": "2026-10-01", "date_to": "2026-10-31"}))
        self.assertEqual(result["status"], "no_data")
        ranges = [body["filters"][0] for path, body in self.calls if path == sales.REPORT][-2:]
        self.assertEqual([(f["from"], f["to"]) for f in ranges], [("2026-10-01","2026-10-31"), ("2026-10-31","2026-11-01")])

    async def test_incomplete_duplicate_and_out_of_range_groups_fail_closed(self):
        row = self.rows[0]
        for rows in [[{**row, DAY: "2026-10-01"}], [{DAY: START}], [row, row], [{**row, "Amount": "NaN"}]]:
            self.rows = rows
            failed = await self.call("get_transaction_report", **self.args())
            self.assertEqual(failed["status"], "unavailable")
            self.assertNotIn("rows", failed)
