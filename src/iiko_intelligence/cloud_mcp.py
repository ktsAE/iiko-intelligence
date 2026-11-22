"""Manager-facing live tools for a single configured organization."""
from __future__ import annotations

from typing import Annotated, Any, Callable, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from .cloud_profile import configured, read_profile
from .cloud_reports import CloudReports

LIVE_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0)]
Query = Annotated[str, Field(min_length=1, max_length=200)]
Quantity = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Kind = Literal["stocktake", "invoice", "writeoff"]


def cloud_status() -> dict:
    return {"source": "iiko Cloud RU", "status": "configured" if configured() else "not_configured",
            "setup": "Connect-iiko-Cloud.cmd",
            "permissions": "The restaurant's access to each method is checked when requested."}


def live(report: Callable[[CloudReports], dict]) -> dict[str, Any]:
    try:
        profile = read_profile()
        if profile is None:
            return cloud_status()
        result = report(CloudReports(profile))
        return {"source": "iiko Cloud RU", "organizationId": profile.organization, "status": "ok", **result}
    except ValueError as exc:
        return {"source": "iiko Cloud RU", "status": "unavailable", "reason": str(exc)}
    except (KeyError, TypeError, ArithmeticError, AttributeError):
        return {"source": "iiko Cloud RU", "status": "unavailable", "reason": "Incomplete or unexpected iiko response."}


def optional_id(value: UUID | None) -> str | None:
    return str(value) if value else None


def register_cloud_tools(server: MCPServer) -> None:
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
    def get_recipe_card(product_id: UUID, as_of: str, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Read the effective технологическая карта on YYYY-MM-DD. Return yield, preparation and top-level
        ingredient quantities, including size and department applicability. Use next_offset for additional rows."""
        return live(lambda api: api.recipe(str(product_id), as_of, limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_item_cost(product_id: UUID, store_id: UUID, as_of: str, quantity: Quantity = 1) -> dict[str, Any]:
        """Read warehouse accounting себестоимость for this quantity at an ISO timestamp with timezone.
        Identify the store, quantity and estimated flag. A missing price is unavailable."""
        return live(lambda api: api.cost(str(product_id), str(store_id), as_of, quantity))

    @server.tool(annotations=LIVE_READ)
    def get_recipe_cost(product_id: UUID, store_id: UUID, as_of: str, product_size_id: UUID | None = None,
                        limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Calculate recipe ingredient cost at a single warehouse using iiko's flattened ingredients and unit costs.
        Give an ISO timestamp in the restaurant's timezone. Returns batch yield and cost per output unit only
        when every ingredient is priced. Excludes labour, overhead and actual consumption variance."""
        return live(lambda api: api.recipe_cost(str(product_id), str(store_id), as_of, optional_id(product_size_id), limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_live_stock(as_of: str, store_id: UUID | None = None, product_id: UUID | None = None,
                       limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Get товарные остатки at an ISO timestamp with timezone. Returns nonzero quantity or value rows,
        product and warehouse names, units and costs. Use next_offset before drawing full-report conclusions."""
        return live(lambda api: api.stock(as_of, optional_id(store_id), optional_id(product_id), limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_inventory_counts(date_from: str, date_to: str, store_id: UUID | None = None,
                             limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """List инвентаризации in up to 31 days. Show document status, surplus/shortage values when supplied.
        Open the desired document with get_iiko_document for counted quantities."""
        return live(lambda api: api.documents("stocktake", date_from, date_to, optional_id(store_id), limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_incoming_invoices(date_from: str, date_to: str, store_id: UUID | None = None,
                              limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """List приходные накладные in up to 31 days. Status and document totals come from iiko.
        Open an invoice with get_iiko_document for individual quantities and prices."""
        return live(lambda api: api.documents("invoice", date_from, date_to, optional_id(store_id), limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_writeoff_acts(date_from: str, date_to: str, store_id: UUID | None = None,
                          limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """List акты списания in up to 31 days with warehouse, status and document totals.
        Open an act with get_iiko_document for individual quantities."""
        return live(lambda api: api.documents("writeoff", date_from, date_to, optional_id(store_id), limit, offset))

    @server.tool(annotations=LIVE_READ)
    def get_iiko_document(kind: Kind, document_id: UUID, section: Literal["items", "itemsFirstStep"] = "items",
                          limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """Read the item lines of an invoice, write-off or inventory count by ID from its list. For a stocktake
        use itemsFirstStep for the first count stage. Keep actual quantities distinct from stock balances."""
        return live(lambda api: api.document(kind, str(document_id), section, limit, offset))
