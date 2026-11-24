"""Generic read/create/edit operations on documented iiko entities in one organization."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import date
from uuid import UUID, uuid5

from jsonschema import Draft7Validator, FormatChecker

from . import config
from .cloud_reports import CloudReports, object_result, page, remote_page
from .cloud_sales import CloudHTTPError, _objects
from .database import write_lock
from .record_catalog import DEFINITIONS, RESOURCES, json_text

FORMATS = FormatChecker(formats=["date", "uuid"])
PRICE_LISTS = {"supplier_price_list", "internal_price_list"}


def resource(kind):
    if kind not in RESOURCES:
        raise ValueError("Unknown iiko record type. Use describe_iiko_records to list the supported names.")
    return RESOURCES[kind]


def resolve(schema):
    if "$ref" in schema:
        return DEFINITIONS[schema["$ref"].split("/")[-1]]
    return schema


def describe(kind, action, field):
    if kind is None:
        return {"record_types": sorted(RESOURCES), "actions": ["list", "get", "create", "update"]}
    entry = resource(kind)
    if action not in entry:
        return {"record_type": kind, "id_field": entry["id"], "lookup": "Use get_iiko_record with the record ID."}
    schema = DEFINITIONS[entry[action]]
    for key in field.split(".") if field else []:
        if schema.get("type") == "array":
            schema = resolve(schema["items"])
        if key not in schema.get("properties", {}):
            raise ValueError("Unknown field in the selected iiko schema.")
        schema = resolve(schema["properties"][key])
    if field and schema.get("type") == "array":
        schema = resolve(schema["items"])
    return {"record_type": kind, "action": action, "id_field": entry["id"], "field": field,
            "required": [k for k in schema.get("required", []) if k != "organizationId"],
            "fields": {k: {name: value for name, value in resolve(v).items() if name in ("type", "format", "enum")}
                       for k, v in schema.get("properties", {}).items() if k != "organizationId"},
            "value_type": schema.get("type")}


def scoped(api, entry, action, values):
    schema = DEFINITIONS[entry[action]]
    result = dict(values)
    if "organizationId" in result and result["organizationId"] != api.org:
        raise ValueError("This operation is for the configured organization only.")
    if "organizationId" in schema["properties"]:
        result["organizationId"] = api.org
    validator = Draft7Validator({"definitions": DEFINITIONS, **schema}, format_checker=FORMATS)
    error = next(validator.iter_errors(result), None)
    if error:
        location = ".".join(str(k) for k in error.absolute_path) or "record"
        if error.validator == "required":
            missing = sorted(set(error.validator_value) - set(error.instance))
            raise ValueError(f"Missing required fields at {location}: {', '.join(missing)}.")
        raise ValueError(f"Invalid iiko field at {location} ({error.validator}). Consult describe_iiko_records.")
    return result


def list_records(api, kind, criteria, limit, offset, restaurant_timezone=None):
    entry = resource(kind)
    args = dict(criteria)
    props = DEFINITIONS[entry["list"]]["properties"]
    paginated = "limit" in props and "offset" in props
    if {"limit", "offset"} & args.keys():
        raise ValueError("Pass limit and offset as separate tool arguments.")
    if paginated:
        args.update(limit=limit, offset=offset)
    if entry["id"] == "documentId":
        if not all(isinstance(args.get(key), str) for key in ("from", "to")):
            raise ValueError("Supply criteria.from and criteria.to, e.g. yesterday for both.")
        period = api.calendar(restaurant_timezone).resolve(args["from"], args["to"])
        if len(period.windows) != 1:
            raise ValueError("List documents for an ordered date range of up to 31 days.")
        start, end = period.windows[0].start, period.windows[0].end
        args.update({"from": start.isoformat(), "to": end.isoformat()})
    result = api.read(entry["base"] + "/list", scoped(api, entry, "list", args))
    key = entry["collection"]
    rows = _objects(object_result(result).get(key) if key else result, "record list")
    if any("organizationId" in row and row["organizationId"] != api.org for row in rows):
        raise ValueError("iiko returned a record from another organization.")
    if entry["id"] == "documentId" and any(not isinstance(row.get("date"), str) or
            not start <= date.fromisoformat(row["date"][:10]) <= end for row in rows):
        raise ValueError("iiko returned a document outside the requested dates.")
    if paginated:
        result_page = remote_page(rows, result, offset, limit)
    else:
        result_page = page(rows, offset, limit)
    if kind == "internal_price_list":
        result_page["header"] = {k: result[k] for k in ("priceListId", "dateFrom", "dateTo", "revision") if k in result}
    if entry["id"] == "documentId":
        result_page.update(date_from=args["from"], date_to=args["to"])
        if result_page["next_offset"] is not None:
            result_page["next_page_arguments"] = {"record_type": kind, "criteria": {**criteria, "from": args["from"], "to": args["to"]},
                                                  "limit": limit, "offset": result_page["next_offset"]}
    return {"record_type": kind, **result_page}


def price_list(api, kind, identifier, selector):
    if kind == "supplier_price_list":
        supplier = str(UUID(selector.get("counteragentId", "")))
        entry = RESOURCES[kind]
        result = api.read(entry["base"] + "/list", scoped(api, entry, "list", {"counteragentId": supplier}))
        rows = [r for r in _objects(object_result(result).get("items"), "price list") if r.get("priceListId") == identifier]
        if not rows or len({(r.get("dateFrom"), r.get("dateTo")) for r in rows}) != 1:
            raise ValueError("No unique price list with this ID and supplier.")
        return {"priceListId": identifier, "counteragentId": supplier, "dateFrom": rows[0].get("dateFrom"),
                "dateTo": rows[0].get("dateTo"), "items": rows}
    criteria = {"date": date.fromisoformat(selector.get("date", "")).isoformat()}
    result = list_records(api, kind, criteria, 100, 0)
    header = result["header"]
    if header.get("priceListId") != identifier:
        raise ValueError("The active price list on this date has a different ID.")
    rows = list(result["rows"])
    while result["next_offset"] is not None:
        result = list_records(api, kind, criteria, 100, result["next_offset"])
        if result["header"] != header or len(rows) >= 100000:
            raise ValueError("The price list changed or is too large to edit in one request.")
        rows.extend(result["rows"])
    return {**header, "items": rows}


def read_record(api, kind, identifier, selector):
    entry = resource(kind)
    if kind in PRICE_LISTS:
        key = "counteragentId" if kind == "supplier_price_list" else "date"
        if set(selector) != {key}:
            raise ValueError("Reading or editing this price list also requires selector." + key)
        return price_list(api, kind, identifier, selector)
    if selector:
        raise ValueError("This record type only needs its ID.")
    if "get" in entry:
        data = object_result(api.read(entry["base"] + "/get", scoped(api, entry, "get", {entry["id"]: identifier})))
    else:
        criteria = ({"ids": [identifier], "includeDeleted": True} if kind == "product_category" else
                    {"filters": [{"field": "id", "op": "eq", "value": identifier}]})
        matches = list_records(api, kind, criteria, 2, 0)
        if len(matches["rows"]) != 1 or matches["next_offset"] is not None:
            raise ValueError("The lookup did not return exactly one matching iiko record.")
        data = matches["rows"][0]
    actual = data.get(entry["id"]) or data.get("id")
    if str(actual).lower() != identifier.lower() or ("organizationId" in data and data["organizationId"] != api.org):
        raise ValueError("The returned record has a different ID or organization.")
    return data


def fingerprint(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def project(value, schema):
    """Project GET records into their write model, including line items; omit computed fields."""
    schema = resolve(schema)
    if isinstance(value, list) and "items" in schema:
        return [project(v, schema["items"]) for v in value]
    if isinstance(value, dict) and "properties" in schema:
        return {key: project(value[key], child) for key, child in schema["properties"].items() if key in value and
                (value[key] is not None or "null" in (resolve(child).get("type") or []))}
    return value


def merge_fields(original, changes):
    merged = dict(original)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_fields(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def modify_rows(record, edits, schema, replaced):
    for edit in edits:
        field = edit["field"]
        prop = resolve(schema["properties"].get(field, {}))
        if field in replaced or prop.get("type") != "array" or not isinstance(record.get(field), list):
            raise ValueError("Row edits need an existing array that is not also being replaced.")
        rows = record[field]
        match = edit.get("match")
        changes = edit["values"]
        if not changes:
            raise ValueError("Specify the row values to add or change.")
        if edit["action"] == "append":
            if match is not None:
                raise ValueError("An appended row does not use match.")
            rows.append(changes)
        else:
            if not match:
                raise ValueError("Editing a row requires a unique match, such as its line number or item ID.")
            match_rows = [row for row in rows if isinstance(row, dict) and all(k in row and row[k] == v for k, v in match.items())]
            if len(match_rows) != 1:
                raise ValueError("The row selector must match exactly one existing line.")
            row = match_rows[0]
            if (row.get("sum") is not None and "sum" in resolve(prop.get("items", {})).get("properties", {})
                    and "sum" not in changes and any(k in changes and changes[k] != row.get(k)
                                                      for k in ("amount", "price", "vatPercent", "actualAmount"))):
                raise ValueError("Include the reviewed line sum when changing the price, quantity or VAT of a priced line.")
            row.update(merge_fields(row, changes))


def journal(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database, timeout=8)
    db.execute("CREATE TABLE IF NOT EXISTS operations (org TEXT, request TEXT, intent TEXT, result TEXT, PRIMARY KEY(org, request))")
    return db


def write_operation(api: CloudReports, kind, action, identifier, values, edits, selector, expected, request):
    entry = resource(kind)
    schema = DEFINITIONS[entry[action]]
    id_field = entry["id"]
    if not values and not edits:
        raise ValueError("Specify record fields or table row changes.")
    if any(values.get(k) for k in ("deleteWithSameDate", "saveWithErrors")):
        raise ValueError("This editor creates and updates records without deleting other price lists or bypassing validation.")
    if action == "create" and id_field in schema["properties"]:
        identifier = str(UUID(values[id_field])) if id_field in values else str(uuid5(UUID(request), api.org + "/" + kind))
    if action == "update" and id_field in values and values[id_field] != identifier:
        raise ValueError("The update ID differs from the requested record.")
    intent = fingerprint([kind, action, identifier, values, edits, selector, expected])
    filename = config.PROJECT_ROOT / "data" / "record-operations.sqlite"
    with closing(journal(filename)) as db, write_lock(filename):
        old = db.execute("SELECT intent,result FROM operations WHERE org=? AND request=?", (api.org, request)).fetchone()
        if old:
            if old[0] != intent:
                raise ValueError("This request_id was used for a different change.")
            return json.loads(old[1])
        if action == "update":
            current = read_record(api, kind, identifier, selector)
            if expected and expected != fingerprint(current):
                raise ValueError("The record changed since it was read. Refresh it and review the changes.")
            if current.get("isDeleted") or current.get("deleted") or current.get("isEditable") is False:
                raise ValueError("iiko marks this record as deleted or locked for editing.")
            base = project(current, schema)
            if kind in {"counterparty", "product"}:
                # These endpoints preserve omitted fields. Send the required baseline and arrays being edited.
                keep = set(schema["required"]) | {edit["field"] for edit in edits}
                keep.update(k for k, v in values.items() if isinstance(v, dict))
                base = {k: v for k, v in base.items() if k in keep}
            body = merge_fields(base, values)
            body[id_field] = identifier
            modify_rows(body, edits, schema, values)
        else:
            body = dict(values)
            if identifier is not None:
                body[id_field] = identifier
        body = scoped(api, entry, action, body)
        pending = {"status": "outcome_unknown", "record_type": kind, "record_id": identifier, "request_id": request,
                   "reason": "Check the record in iiko before repeating this change. The request may have reached the server."}
        db.execute("INSERT INTO operations VALUES (?,?,?,?)", (api.org, request, intent, json.dumps(pending)))
        db.commit()
        try:
            receipt = object_result(api.write(entry["base"] + "/" + action, body))
        except CloudHTTPError as exc:
            if exc.status in {400, 401, 403, 404, 409, 422, 429}:
                db.execute("DELETE FROM operations WHERE org=? AND request=?", (api.org, request))
                db.commit()
                raise
            return {**pending, **exc.details}
        except (ValueError, OSError, TypeError):
            return pending
        result_id = receipt.get(id_field)
        try:
            result_id = str(UUID(str(result_id)))
        except ValueError:
            return pending
        if identifier is not None and identifier.lower() != result_id:
            return pending
        outcome = {"status": "created" if action == "create" else "updated", "record_type": kind, "record_id": result_id,
                   "request_id": request, **{k:receipt[k] for k in ("documentNumber", "messages") if k in receipt}}
        db.execute("UPDATE operations SET result=? WHERE org=? AND request=?", (json.dumps(outcome), api.org, request))
        db.commit()
        return outcome
