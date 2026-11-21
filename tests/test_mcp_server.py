from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from mcp import Client

from iiko_intelligence.mcp_server import server
from iiko_intelligence.seed import seed_demo


EXPECTED_TOOLS = {
    "compare_sales_periods",
    "get_capabilities",
    "get_data_status",
    "get_sales_summary",
    "get_top_dishes",
    "get_purchases",
    "get_stock_balances",
    "get_writeoffs",
}


class MCPDefinitionTest(unittest.TestCase):
    def test_only_expected_read_tools_are_registered(self) -> None:
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, EXPECTED_TOOLS)
        self.assertFalse(any(word in name for name in names for word in ("create", "update", "delete", "raw")))
        self.assertTrue(all(tool.annotations and tool.annotations.read_only_hint for tool in tools))

    def test_tool_schemas_are_bounded(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        sales_schema = tools["get_sales_summary"].input_schema
        self.assertEqual(set(sales_schema["required"]), {"date_from", "date_to"})
        self.assertNotIn("path", str(sales_schema).lower())
        self.assertNotIn("method", str(sales_schema).lower())
        for tool in tools.values():
            limit = tool.input_schema.get("properties", {}).get("limit")
            if limit:
                self.assertEqual((limit["minimum"], limit["maximum"]), (1, 100))


class MCPProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_negotiates_and_calls_sales_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "trial.sqlite"
            seed_demo(database, as_of=date(2026, 9, 30), days=30)
            previous = os.environ.get("IIKO_INTELLIGENCE_DB")
            os.environ["IIKO_INTELLIGENCE_DB"] = str(database)
            try:
                async with Client(server, read_timeout_seconds=5) as client:
                    self.assertEqual(client.protocol_version, "2026-07-28")
                    listed = await client.list_tools()
                    self.assertEqual({tool.name for tool in listed.tools}, EXPECTED_TOOLS)
                    result = await client.call_tool(
                        "get_sales_summary",
                        {"date_from": "2026-09-01", "date_to": "2026-09-30"},
                    )
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["dataset_mode"], "synthetic")
                    self.assertGreater(result.structured_content["totals"]["checks"], 0)
            finally:
                if previous is None:
                    os.environ.pop("IIKO_INTELLIGENCE_DB", None)
                else:
                    os.environ["IIKO_INTELLIGENCE_DB"] = previous


if __name__ == "__main__":
    unittest.main()
