"""Manager-facing live tools for a single configured organization."""
from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Callable, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from . import transaction_reports
from .cloud_sales import CloudHTTPError
from .cloud_checks import CheckGroup, check_access
from .cloud_profile import configured, read_profile
from .cloud_reports import CloudReports
from .updates import with_update_notice

LIVE_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0)]
Query = Annotated[str, Field(min_length=1, max_length=200)]
Quantity = Annotated[float, Field(gt=0, allow_inf_nan=False)]
FieldName = Annotated[str, Field(min_length=1, max_length=160)]
GroupFields = Annotated[list[FieldName], Field(max_length=16)]
AmountFields = Annotated[list[FieldName], Field(min_length=1, max_length=8)]
FilterValues = Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(min_length=1, max_length=100)]
SnapshotId = Annotated[str, Field(pattern="^[a-f0-9]{64}$")]
ReportBound = Annotated[str, Field(
    description="Включительная граница периода. Для «за вчера» или «за фчера» передай yesterday "
                "в обеих границах. Для прошлой недели — last_week в обеих. Можно указать точную дату.",
    examples=["yesterday", "last_week", "2026-11-23"])]
AccountingDay = Annotated[str, Field(description="Один учётный день ресторана, например yesterday для «вчера».",
                                     examples=["yesterday", "2026-11-23"])]
BalanceTime = Annotated[str, Field(description="Момент расчёта. Для «на конец вчерашнего дня» передай end_of_yesterday.",
                                   examples=["now", "end_of_yesterday", "2026-11-23T18:00:00+03:00"])]
Kind = Literal["stocktake", "invoice", "writeoff"]


def cloud_status() -> dict:
    return {"source": "iiko Cloud RU", "status": "configured" if configured() else "not_configured",
            "setup": "Connect-iiko-Cloud.cmd",
            "permissions": "Use check_live_access for an online check of reporting permissions."}


def live(report: Callable[[CloudReports], dict]) -> dict[str, Any]:
    return with_update_notice(_read_live(report))


def _read_live(report: Callable[[CloudReports], dict]) -> dict[str, Any]:
    try:
        profile = read_profile()
        if profile is None:
            return cloud_status()
        result = report(CloudReports(profile))
        return {"source": "iiko Cloud RU", "organizationId": profile.organization, "status": "ok", **result}
    except CloudHTTPError as exc:
        return {"source": "iiko Cloud RU", "status": "unavailable", "reason": str(exc), **exc.details}
    except ValueError as exc:
        return {"source": "iiko Cloud RU", "status": "unavailable", "reason": str(exc)}
    except sqlite3.Error:
        return {"source": "iiko Cloud RU", "status": "unavailable",
                "reason": "Unable to verify the local write receipt. Check iiko and reuse the same request_id."}
    except (KeyError, TypeError, ArithmeticError, AttributeError):
        return {"source": "iiko Cloud RU", "status": "unavailable", "reason": "Incomplete or unexpected iiko response."}


def optional_id(value: UUID | None) -> str | None:
    return str(value) if value else None


def register_cloud_tools(server: MCPServer) -> None:
    @server.tool(annotations=LIVE_READ)
    def list_transaction_fields(query: str = "", limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Discover transaction OLAP columns, Russian labels and supported grouping, filtering and aggregation.
        Find the account, amount, operation and reference fields before requesting postings."""
        return live(lambda api: transaction_reports.list_fields(api, query, limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_transaction_report(date_from: ReportBound, date_to: ReportBound, group_fields: GroupFields, amount_fields: AmountFields,
                               filters: dict[FieldName, FilterValues] | None = None, restaurant_timezone: str | None = None,
                               limit: Limit = 50, offset: Offset = 0, snapshot_id: SnapshotId | None = None) -> dict[str, Any]:
        """Read TRANSACTIONS OLAP for up to 31 inclusive dates. Use actual metadata field names and exact filter values.
        Select the account, operation, reference and amount fields required by the report. Rows may be aggregated.
        Follow next_page_arguments until the final page. An account movement alone does not establish a balance."""
        return live(lambda api: transaction_reports.transaction_report(api, date_from, date_to, group_fields, amount_fields,
                                                                       filters or {}, restaurant_timezone, limit, offset, snapshot_id))

    @server.tool(annotations=LIVE_READ)
    def list_saved_iiko_reports(query: str = "", limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Find owner-configured iiko OLAP presets by part of their name. Choose an exact match or ask."""
        return live(lambda api: transaction_reports.list_presets(api, query, limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_saved_iiko_report(preset_id: FieldName, date_from: ReportBound, date_to: ReportBound, restaurant_timezone: str | None = None,
                              limit: Limit = 50, offset: Offset = 0, snapshot_id: SnapshotId | None = None) -> dict[str, Any]:
        """Read data rows of a known saved report for up to 31 inclusive dates. Use the ID returned by the preset list.
        Presets may group multiple postings. Follow next_page_arguments. Never add intermediate or grand totals to detail."""
        return live(lambda api: transaction_reports.saved_report(api, preset_id, date_from, date_to, restaurant_timezone,
                                                                 limit, offset, snapshot_id))

    @server.tool(annotations=LIVE_READ)
    def check_live_access(group: CheckGroup = "all", as_of: str | None = None,
                          product_id: UUID | None = None, store_id: UUID | None = None,
                          restaurant_timezone: str | None = None, preset_id: FieldName | None = None) -> dict[str, Any]:
        """Test live iiko reads on this restaurant and report each outcome. Sales checks query yesterday and compare it with today.
        Use transactions to check OLAP posting access. Supply preset_id to test the selected saved report.
        For recipe/cost checks it samples a dish and warehouse unless you supply their IDs. Choose as_of with the restaurant UTC
        offset to check documents on a known day. Without as_of the connector resolves the local day of the restaurant.
        A successful empty list confirms the query ran. A sample document is needed for its detail check."""
        return live(lambda api: check_access(api, group, as_of, optional_id(product_id), optional_id(store_id), restaurant_timezone, preset_id))

    @server.tool(annotations=LIVE_READ)
    def find_iiko_products(query: Query, field: Literal["name", "code", "productArticle"] = "name",
                           limit: Limit = 20, offset: Offset = 0) -> dict[str, Any]:
        """Find dishes or ingredients by name, code or article. Use productId in the recipe, stock and cost tools.
        When multiple products match, ask the manager which one to use."""
        return live(lambda api: api.find_products(query, field, limit, offset))

    @server.tool(annotations=LIVE_READ)
    def list_iiko_stores(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """List warehouse names and IDs for this restaurant. Check deleted status and select the requested warehouse."""
        return live(lambda api: api.stores(limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_recipe_card(product_id: UUID, as_of: AccountingDay, limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """Read the effective технологическая карта on a date, today, yesterday or day_before_yesterday. Return yield, preparation and top-level
        ingredient quantities, including size and department applicability. Use next_offset for additional rows."""
        return live(lambda api: api.recipe(str(product_id), as_of, limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_item_cost(product_id: UUID, store_id: UUID, as_of: BalanceTime, quantity: Quantity = 1, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """Read warehouse accounting себестоимость for this quantity at now, end_of_yesterday or an ISO timestamp with UTC offset.
        Identify the store, quantity and estimated flag. A missing price is unavailable."""
        return live(lambda api: api.cost(str(product_id), str(store_id), as_of, quantity, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_recipe_cost(product_id: UUID, store_id: UUID, as_of: BalanceTime, product_size_id: UUID | None = None,
                        limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """Calculate recipe ingredient cost at a single warehouse using iiko's flattened ingredients and unit costs.
        Use now, end_of_yesterday or an ISO timestamp in the restaurant's timezone. Returns batch yield and cost per output unit only
        when every ingredient is priced. Excludes labour, overhead and actual consumption variance."""
        return live(lambda api: api.recipe_cost(str(product_id), str(store_id), as_of, optional_id(product_size_id), limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_live_stock(as_of: BalanceTime, store_id: UUID | None = None, product_id: UUID | None = None,
                       limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """Get товарные остатки at now, end_of_yesterday or an ISO timestamp with UTC offset. Returns nonzero quantity or value rows,
        product and warehouse names, units and costs. Use next_offset before drawing full-report conclusions."""
        return live(lambda api: api.stock(as_of, optional_id(store_id), optional_id(product_id), limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_inventory_counts(date_from: ReportBound, date_to: ReportBound, store_id: UUID | None = None,
                             limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """List инвентаризации in up to 366 days. Show document status, surplus/shortage values when supplied.
        Repeat a named period such as last_month in both dates or supply inclusive dates. Follow next_offset for more.
        Open the desired document with get_iiko_document for counted quantities."""
        return live(lambda api: api.documents("stocktake", date_from, date_to, optional_id(store_id), limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_incoming_invoices(date_from: ReportBound, date_to: ReportBound, store_id: UUID | None = None,
                              limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """List приходные накладные in up to 366 days. Status and document totals come from iiko.
        Repeat a named period such as last_month in both dates or supply inclusive dates. Follow next_offset for more.
        Open an invoice with get_iiko_document for individual quantities and prices."""
        return live(lambda api: api.documents("invoice", date_from, date_to, optional_id(store_id), limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_writeoff_acts(date_from: ReportBound, date_to: ReportBound, store_id: UUID | None = None,
                          limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """List акты списания in up to 366 days with warehouse, status and document totals.
        Repeat a named period such as last_month in both dates or supply inclusive dates. Follow next_offset for more.
        Open an act with get_iiko_document for individual quantities."""
        return live(lambda api: api.documents("writeoff", date_from, date_to, optional_id(store_id), limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_iiko_document(kind: Kind, document_id: UUID, section: Literal["items", "itemsFirstStep"] = "items",
                          limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Read the item lines of an invoice, write-off or inventory count by ID from its list. For a stocktake
        use itemsFirstStep for the first count stage. Keep actual quantities distinct from stock balances."""
        return live(lambda api: api.document(kind, str(document_id), section, limit, offset))
