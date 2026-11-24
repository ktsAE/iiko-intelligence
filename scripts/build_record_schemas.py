"""Extract the supported request contracts from the official iiko Cloud RU OpenAPI JSON."""
import json
from copy import deepcopy
import sys
from pathlib import Path

# A reviewed list, independent of new methods added upstream.
RESOURCES = {
    "incoming_invoice": "inventory/v1/incoming_invoice",
    "writeoff": "inventory/v2/writeoff_document",
    "inventory_count": "inventory/v1/incoming_inventory",
    "internal_transfer": "inventory/v2/internal_transfer",
    "production": "inventory/v2/production_document",
    "disassembly": "inventory/v2/disassemble_document",
    "outgoing_invoice": "inventory/v1/outgoing_invoice",
    "incoming_return": "inventory/v1/incoming_returned_invoice",
    "supplier_return": "inventory/v1/returned_invoice",
    "sales_document": "inventory/v1/sales_document",
    "transformation": "inventory/v1/transformation_document",
    "substitution": "inventory/v2/substitution_record",
    "incoming_service": "finance/v1/incoming_service",
    "outgoing_service": "finance/v1/outgoing_service",
    "counterparty": "inventory/v1/counteragents",
    "account": "finance/v1/account",
    "cash_flow_category": "finance/v1/cash-flow-category",
    "product": "nomenclature/v2/product",
    "product_group": "nomenclature/v2/group",
    "product_category": "nomenclature/v1/category",
    "product_scale": "nomenclature/v1/product-scale",
    "recipe": "nomenclature/v3/assembly-chart",
    "supplier_price_list": "inventory/v1/counteragents/pricelist",
    "internal_price_list": "inventory/v1/independent-price-list",
}
IDS = {"counterparty": "counteragentId", "product": "productId", "product_group": "groupId",
       "supplier_price_list": "priceListId", "internal_price_list": "priceListId"}
GENERIC_IDS = {"account", "cash_flow_category", "product_category", "recipe", "product_scale"}
ARRAY_KEYS = {"product_group": "groups", "counterparty": "counteragents", "account": "accounts",
              "cash_flow_category": "cashFlowCategories"}


def build(spec):
    types = {}

    def reduce(node):
        if "$ref" in node:
            key = node["$ref"].split("/")[-1]
            if key not in types:
                types[key] = {}
                types[key] = reduce(spec["components"]["schemas"][key])
            return {"$ref": "#/definitions/" + key}
        result = {k: node[k] for k in ("type", "format", "enum", "required", "minimum", "maximum",
                                        "minItems", "maxItems", "minLength", "maxLength", "pattern") if k in node}
        if node.get("type") == "string" and ("(GUID)" in node.get("description", "") or "UUID" in node.get("description", "")):
            result.setdefault("format", "uuid")
        if node.get("nullable") and "type" in result:
            result["type"] = [result["type"], "null"]
        if "properties" in node:
            result.update(properties={k: reduce(v) for k, v in node["properties"].items()}, additionalProperties=False)
        elif isinstance(node.get("additionalProperties"), dict):
            result["additionalProperties"] = reduce(node["additionalProperties"])
        if "items" in node:
            result["items"] = reduce(node["items"])
        for keyword in ("allOf", "oneOf", "anyOf"):
            if keyword in node:
                result[keyword] = [reduce(part) for part in node[keyword]]
        return result

    resources = {}
    for kind, path in RESOURCES.items():
        base = "/api/" + path
        entry = {"base": base, "id": IDS.get(kind, "id" if kind in GENERIC_IDS else "documentId")}
        for op in ("list", "get", "create", "update"):
            operation = spec["paths"].get(base + "/" + op, {}).get("post")
            if not operation:
                continue
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            entry[op] = reduce(schema)["$ref"].split("/")[-1]
            if op == "list":
                response = operation["responses"]["200"]["content"]["application/json"]["schema"]
                entry["collection"] = None if response.get("type") == "array" else ARRAY_KEYS.get(kind, "items")
                if entry["collection"]:
                    assert entry["collection"] in spec["components"]["schemas"][response["$ref"].split("/")[-1]]["properties"]
        resources[kind] = entry
        for op in ("create", "update"):
            model = deepcopy(types[entry[op]])
            entry[op] = kind + "." + op
            types[entry[op]] = model
            required = set(model.get("required", []))
            if op == "update":
                required.add(entry["id"])
            if kind == "recipe":
                required.update(("productId", "dateFrom"))
                model["properties"]["dateTo"] = {"type": ["string", "null"], "format": "date"}
            if kind in ("product_scale", "product_category"):
                required.add("name")
            if kind.endswith("price_list"):
                required.update(("dateFrom", "items", "organizationId"))
                if kind == "supplier_price_list":
                    required.add("counteragentId")
                model["properties"]["items"]["minItems"] = 1
            model["required"] = sorted(required)
    used = {value for entry in resources.values() for key, value in entry.items() if key in ("create", "update", "list", "get")}
    def dependencies(node):
        if isinstance(node, list):
            for child in node:
                dependencies(child)
        elif isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                if name not in used:
                    used.add(name)
                    dependencies(types[name])
            for child in node.values():
                dependencies(child)
    for name in list(used):
        dependencies(types[name])
    types = {name: types[name] for name in used}
    return {"source": "https://api-ru.iiko.services/api-docs/docs", "resources": resources, "definitions": types}


if __name__ == "__main__":
    result = build(json.loads(Path(sys.argv[1]).read_bytes()))
    target = Path(__file__).resolve().parents[1] / "src/iiko_intelligence/record_schemas.json"
    target.write_text(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
