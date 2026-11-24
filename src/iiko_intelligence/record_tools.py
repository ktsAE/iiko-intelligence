"""Manager-facing tools for editing existing iiko tables and records."""
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from . import records
from .cloud_mcp import LIVE_READ, Limit, Offset, live

CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True, idempotent_hint=True)
UPDATE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True, idempotent_hint=True)


class RowEdit(BaseModel, extra="forbid"):
    field: str
    action: Literal["append", "edit"]
    match: dict[str, Any] | None = None
    values: dict[str, Any]


def register_record_tools(server: MCPServer):
    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def describe_iiko_records(record_type: str | None = None, action: Literal["list", "get", "create", "update"] = "create",
                              field: str | None = None) -> dict[str, Any]:
        """List supported iiko entity names and inspect an action's fields, types and requirements.
        Use field='items' or a dotted field path to see line-item fields. The connector supplies organizationId."""
        return records.describe(record_type, action, field)

    @server.tool(annotations=LIVE_READ)
    def list_iiko_records(record_type: str, criteria: dict[str, Any] | None = None,
                          limit: Limit = 50, offset: Offset = 0, restaurant_timezone: str | None = None) -> dict[str, Any]:
        """Read an existing iiko table or register. Discover criteria using describe_iiko_records(action='list').
        For documents pass up to 31 days. «Накладные за вчера»: record_type='incoming_invoice',
        criteria={'from': 'yesterday', 'to': 'yesterday'}. Follow next_page_arguments when supplied."""
        return live(lambda api: records.list_records(api, record_type, criteria or {}, limit, offset, restaurant_timezone))

    @server.tool(annotations=LIVE_READ)
    def get_iiko_record(record_type: str, record_id: UUID, selector: dict[str, str] | None = None) -> dict[str, Any]:
        """Read the full source record and a version fingerprint before editing. Supplier price lists also require
        selector.counteragentId; internal price lists require selector.date. Use items to choose existing lines."""
        def retrieve(api):
            data = records.read_record(api, record_type, str(record_id), selector or {})
            return {"record_type": record_type, "record_id": str(record_id), "record": data,
                    "version": records.fingerprint(data)}
        return live(retrieve)

    @server.tool(annotations=CREATE)
    def create_iiko_record(record_type: str, values: dict[str, Any], request_id: UUID) -> dict[str, Any]:
        """Create one real record in iiko using its create schema. For documents provide header and complete line items.
        Generate one UUID request_id per intended change and reuse it on retries. Only call for a requested write.
        On outcome_unknown inspect iiko before another attempt. This operation does not call a posting endpoint."""
        return live(lambda api: records.write_operation(api, record_type, "create", None, values, [], {}, None, str(request_id)))

    @server.tool(annotations=UPDATE)
    def update_iiko_record(record_type: str, record_id: UUID, request_id: UUID, changes: dict[str, Any] | None = None,
                           row_edits: Annotated[list[RowEdit], Field(max_length=500)] | None = None,
                           expected_version: str | None = None, selector: dict[str, str] | None = None) -> dict[str, Any]:
        """Edit the selected live iiko record. Pass changed fields only and the version from get_iiko_record.
        For individual table lines use row_edits: append values, or edit values by a unique match within the array.
        An array in changes explicitly replaces that entire array. Generate one request_id per change and reuse on retry.
        Price lists take the same selector as get_iiko_record. The tool sends no post, delete or unpost operation."""
        edits = [v.model_dump() for v in row_edits or []]
        return live(lambda api: records.write_operation(api, record_type, "update", str(record_id), changes or {}, edits,
                                                        selector or {}, expected_version, str(request_id)))
