from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from .cloud_paths import LIVE_READ_PATHS
from .sales_snapshot import MAX_SALES_ROWS, SalesPeriod, SalesRow, number_units, report_date, report_text


CLOUD_ORIGIN = "https://api-ru.iiko.services"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
TOKEN = "/api/v2/access_token"
ORGANIZATIONS = "/api/1/organizations"
COLUMNS = "/api/reporting/v1/olap/columns/get"
REPORT = "/api/reporting/v1/olap/get"
DAY, DEPARTMENT = "OpenDate.Typed", "Department"
DISH_ID, DISH_NAME, QUANTITY = "DishId", "DishName", "DishAmountInt"
SALES, ORDERS = "DishDiscountSumInt", "UniqOrderId"
Post = Callable[[str, dict[str, Any], str | None], Any]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(path: str, payload: dict[str, Any], token: str | None) -> Any:
    if path not in {TOKEN, ORGANIZATIONS, COLUMNS, REPORT} and path not in LIVE_READ_PATHS:
        raise ValueError("unsupported iiko Cloud operation")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(CLOUD_ORIGIN + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise ValueError(f"iiko Cloud rejected {path} (HTTP {exc.code}); check API access and report settings") from None
    except (OSError, URLError, HTTPException):
        raise ValueError(f"could not reach iiko Cloud for {path}; check your network and retry") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("iiko Cloud response exceeded the limit; use a shorter period")
    try:
        return json.loads(raw, parse_float=Decimal)
    except (UnicodeError, ValueError):
        raise ValueError(f"iiko Cloud returned invalid JSON for {path}") from None


def _objects(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(record, dict) for record in value):
        raise ValueError(f"iiko Cloud did not return a valid {label}")
    return value


def _object_array(response: object, field: str, label: str) -> list[dict[str, Any]]:
    return _objects(response.get(field) if isinstance(response, dict) else None, label)


def column_permissions(response: object) -> dict:
    return {row["name"]: row for row in _objects(response, "OLAP column list") if isinstance(row.get("name"), str)}


def require_columns(columns: dict, flags: dict) -> None:
    unsupported = [name for name, required in flags.items()
                   if any(columns.get(name, {}).get(flag) is not True for flag in required)]
    if unsupported:
        raise ValueError("the iiko Cloud key lacks required OLAP fields: " + ", ".join(unsupported))


def _check_columns(response: object) -> bool:
    flags = {
        DAY: ("canGroup", "canFilter"), DEPARTMENT: ("canGroup", "canFilter"),
        DISH_ID: ("canGroup",), DISH_NAME: ("canGroup",), QUANTITY: ("canAggregate",),
        SALES: ("canAggregate",),
    }
    columns = column_permissions(response)
    require_columns(columns, flags)
    return columns.get(ORDERS, {}).get("canAggregate") is True


def _windows(start: date, end: date) -> Iterator[tuple[date, date]]:
    while start <= end:
        last = start + timedelta(days=min(30, (end - start).days))
        yield start, last
        if last == end:
            return
        start = last + timedelta(days=1)


def _request(start: date, end: date, department: str, *, by_dish: bool, with_orders: bool = False) -> dict[str, Any]:
    daily_fields = [ORDERS, SALES] if with_orders else [SALES]
    return olap_request({"periodType": "CUSTOM", "from": start.isoformat(), "to": end.isoformat(),
                         "includeLow": True, "includeHigh": True}, department,
                        [DISH_ID, DISH_NAME] if by_dish else [], [QUANTITY, SALES] if by_dish else daily_fields)


def olap_request(period: dict, department: str | None, groups: list[str], measures: list[str]) -> dict:
    filters = [{"name": DAY, "filterType": "DateRange", **period}]
    dimensions = [DAY]
    if department is not None:
        filters.append({"name": DEPARTMENT, "filterType": "IncludeValues", "values": [department]})
        dimensions.append(DEPARTMENT)
    return {"reportType": "SALES", "buildSummary": False, "groupByRowFields": dimensions + groups,
            "aggregateFields": measures, "filters": filters}


def _scoped_day(row: dict[str, Any], start: date, end: date, department: str) -> date:
    day = report_date(row.get(DAY))
    if not start <= day <= end or row.get(DEPARTMENT) != department:
        raise ValueError("iiko Cloud returned rows outside the requested dates or outlet")
    return day


def organization_session(organization: str, credentials: dict[str, str], *, post: Post = _post,
                         extended: bool = False) -> tuple[str, dict]:
    try:
        organization = str(UUID(organization))
    except ValueError:
        raise ValueError("organization must be the UUID supplied by iiko") from None
    response = post(TOKEN, credentials, None)
    token = response.get("token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token or not token.isascii() or not token.isprintable():
        raise ValueError("iiko Cloud did not return a usable access token")
    scope = {"includeDisabled": True}
    if extended:
        scope["returnAdditionalInfo"] = True
    organizations = _object_array(post(ORGANIZATIONS, scope, token),
                                  "organizations", "organization list")
    if len(organizations) != 1 or organizations[0].get("id") != organization:
        raise ValueError("use a key whose organization list contains only the configured iiko organization")
    return token, organizations[0]


def organization_token(organization: str, credentials: dict[str, str], *, post: Post = _post) -> str:
    token, _ = organization_session(organization, credentials, post=post)
    return token


def fetch_cloud_sales(
    period: SalesPeriod, organization: str, department: str, credentials: dict[str, str], *, post: Post = _post,
) -> tuple[list[SalesRow], dict[date, int] | None]:
    department = report_text(department, "iiko outlet name")
    token = organization_token(organization, credentials, post=post)
    with_orders = _check_columns(post(COLUMNS, {"reportType": "SALES"}, token))

    sales: list[SalesRow] = []
    orders: dict[date, int] = {}
    for start, end in _windows(period.start, period.end):
        grand_rows = _object_array(post(REPORT, _request(start, end, department, by_dish=False, with_orders=with_orders), token),
                                   "data", "daily sales report")
        if len(grand_rows) > (end - start).days + 1:
            raise ValueError("iiko Cloud returned too many daily totals")
        day_totals: dict[date, int] = {}
        for row in grand_rows:
            day = _scoped_day(row, start, end, department)
            if day in day_totals:
                raise ValueError("iiko Cloud returned duplicate daily totals")
            day_totals[day] = number_units(row.get(SALES), 2, "daily sales")
            if with_orders:
                count = number_units(row.get(ORDERS), 0, "order count")
                if count < 0:
                    raise ValueError("iiko Cloud returned a negative order count")
                orders[day] = count
        dish_rows = _object_array(post(REPORT, _request(start, end, department, by_dish=True), token),
                                  "data", "dish sales report")
        if len(sales) + len(dish_rows) > MAX_SALES_ROWS:
            raise ValueError("iiko Cloud returned too many dish rows; use a shorter period")
        dish_totals: dict[date, int] = defaultdict(int)
        for row in dish_rows:
            day = _scoped_day(row, start, end, department)
            sale = SalesRow.from_values(day, row.get(DISH_ID), row.get(DISH_NAME), row.get(QUANTITY), row.get(SALES))
            dish_totals[day] += sale.sales_minor
            sales.append(sale)
        if dict(dish_totals) != day_totals:
            raise ValueError("dish sales do not reconcile with the daily iiko Cloud report")
    return sales, orders if with_orders else None
