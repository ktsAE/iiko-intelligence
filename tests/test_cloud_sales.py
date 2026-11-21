from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from iiko_intelligence import cloud_sales as cloud
from iiko_intelligence.sales_snapshot import SalesPeriod


ORGANIZATION = "85166a83-9173-478b-9f95-f2818851fd84"
OUTLET = "Sample outlet"
PERIOD = SalesPeriod(date(2026, 8, 1), date(2026, 9, 2), 24690, OUTLET, "RUB")
FAKE_CREDENTIALS = {"appId": "fixture", "clientSecret": "fixture", "apiKey": "fixture"}


class CloudSalesTest(unittest.TestCase):
    def _fixture(self, failure: str | None = None):
        calls = []

        def post(path, payload, token):
            calls.append((path, payload))
            self.assertEqual(token, None if path == cloud.TOKEN else "fixture-token")
            if path == cloud.TOKEN:
                self.assertEqual(payload, FAKE_CREDENTIALS)
                return {"token": "fixture-token"}
            if path == cloud.ORGANIZATIONS:
                organizations = [{"id": ORGANIZATION}]
                if failure == "organization":
                    organizations.append({"id": "other"})
                return {"organizations": organizations}
            if path == cloud.COLUMNS:
                fields = [cloud.DAY, cloud.DEPARTMENT, cloud.DISH_ID, cloud.DISH_NAME, cloud.QUANTITY, cloud.ORDERS]
                if failure == "no_orders":
                    fields.remove(cloud.ORDERS)
                if failure != "columns":
                    fields.append(cloud.SALES)
                return [{"name": field, "canGroup": True, "canFilter": True, "canAggregate": True}
                        for field in fields]
            self.assertEqual(path, cloud.REPORT)
            self.assertEqual(payload["filters"][1], {"name": cloud.DEPARTMENT, "filterType": "IncludeValues",
                                                       "values": [OUTLET]})
            day = payload["filters"][0]["from"]
            row = {cloud.DAY: f"{day}T00:00:00.000", cloud.DEPARTMENT: OUTLET, cloud.SALES: Decimal("123.45")}
            if cloud.DISH_ID in payload["groupByRowFields"]:
                row.update({cloud.DISH_ID: "sample-dish", cloud.DISH_NAME: "Tea", cloud.QUANTITY: 5})
                if failure == "outlet":
                    row[cloud.DEPARTMENT] = "other"
                if failure == "sales":
                    row[cloud.SALES] = 3
            elif cloud.ORDERS in payload["aggregateFields"]:
                row[cloud.ORDERS] = 3
            return {"data": [row]}

        return calls, post

    def test_expected_requests_and_date_windows(self) -> None:
        calls, post = self._fixture()
        rows, orders = cloud.fetch_cloud_sales(PERIOD, ORGANIZATION, OUTLET, FAKE_CREDENTIALS, post=post)
        self.assertEqual(sum(row.sales_minor for row in rows), 24690)
        self.assertEqual(sum(orders.values()), 6)
        self.assertEqual([path for path, _ in calls],
                         [cloud.TOKEN, cloud.ORGANIZATIONS, cloud.COLUMNS] + [cloud.REPORT] * 4)
        windows = [body["filters"][0] for path, body in calls if path == cloud.REPORT]
        self.assertEqual([(f["from"], f["to"]) for f in windows],
                         [("2026-08-01", "2026-08-31")] * 2 + [("2026-09-01", "2026-09-02")] * 2)

    def test_bad_scope_mappings_and_totals_fail_closed(self) -> None:
        for problem, message in (("organization", "only the configured"), ("columns", "lacks required"),
                                 ("outlet", "outside the requested"), ("sales", "do not reconcile")):
            with self.subTest(problem=problem), self.assertRaisesRegex(ValueError, message):
                _, post = self._fixture(problem)
                cloud.fetch_cloud_sales(PERIOD, ORGANIZATION, OUTLET, FAKE_CREDENTIALS, post=post)

    def test_key_without_order_count_still_reconciles_sales(self) -> None:
        calls, post = self._fixture("no_orders")
        rows, orders = cloud.fetch_cloud_sales(PERIOD, ORGANIZATION, OUTLET, FAKE_CREDENTIALS, post=post)
        self.assertIsNone(orders)
        self.assertEqual(sum(row.sales_minor for row in rows), PERIOD.expected_minor)
        self.assertTrue(all(cloud.ORDERS not in body["aggregateFields"] for path, body in calls if path == cloud.REPORT))
