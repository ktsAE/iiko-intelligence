"""Read transaction OLAP data and the restaurant's saved report configurations."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import cloud_paths as paths
from .cloud_reports import CloudReports, continuation, page, selected
from .cloud_sales import COLUMNS, REPORT, _object_array, _objects, column_permissions, require_columns
from .sales_snapshot import report_text


def fields(api: CloudReports) -> dict:
    return column_permissions(api.read(COLUMNS, {"reportType": "TRANSACTIONS"}))


def list_fields(api: CloudReports, query: str, limit: int, offset: int) -> dict:
    term = query.strip().casefold()
    rows = [selected(field, "name displayName type canFilter canGroup canAggregate") for _, field in sorted(fields(api).items())
            if term in str(field.get("displayName", "")).casefold() or term in field["name"].casefold()]
    return {"report_type": "TRANSACTIONS", **page(rows, offset, limit)}


def list_presets(api: CloudReports, query: str, limit: int, offset: int) -> dict:
    term = query.strip().casefold()
    rows = []
    for row in _objects(api.read(paths.PRESETS, {}), "saved reports"):
        name = report_text(row.get("name"), "saved report name", limit=500)
        identifier = report_text(row.get("presetId"), "saved report ID")
        if term in name.casefold():
            rows.append({"name": name, "presetId": identifier})
    rows.sort(key=lambda item: (item["name"], item["presetId"]))
    return page(rows, offset, limit)


def report_span(api: CloudReports, first: str, last: str, zone: str | None):
    period = api.calendar(zone).resolve(first, last)
    if len(period.windows) != 1:
        raise ValueError("Request up to 31 days at a time. Reconcile each period before combining reports.")
    return period.windows[0]


def canonical(value) -> str:
    # Keep decimal precision from the JSON decoder when sorting and fingerprinting pages.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def report_page(rows: list[dict], query: dict, arguments: dict, limit: int, offset: int, expected: str | None) -> dict:
    rows.sort(key=canonical)
    digest = hashlib.sha256(canonical(query).encode())
    for row in rows:
        digest.update(b"\n")
        digest.update(canonical(row).encode())
    snapshot = digest.hexdigest()
    if (offset or expected is not None) and expected != snapshot:
        raise ValueError("Report page could not be verified against the first page. Restart from offset 0 before reconciliation.")
    result = {**page(rows, offset, limit), "snapshot_id": snapshot,
              "status": "ok" if rows else "no_data", "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "date_from": arguments["date_from"], "date_to": arguments["date_to"]}
    return continuation(result, **arguments, limit=limit, snapshot_id=snapshot)


def saved_report(api: CloudReports, preset_id: str, first: str, last: str, zone: str | None,
                 limit: int, offset: int, snapshot: str | None) -> dict:
    span = report_span(api, first, last, zone)
    # The preset endpoint uses an *exclusive* upper date, unlike our public tools.
    body = {"presetId": preset_id, "dateFrom": span.start.isoformat(), "dateTo": (span.end + timedelta(days=1)).isoformat()}
    rows = _object_array(api.read(paths.PRESET_REPORT, body), "data", "saved report rows")
    args = {"preset_id": preset_id, "date_from": span.start.isoformat(), "date_to": span.end.isoformat()}
    return {"preset_id": preset_id, "row_basis": "data rows at the saved preset's grouping; summary rows are excluded",
            **report_page(rows, {"organization": api.org, **body}, args, limit, offset, snapshot)}


def posting_rows(api: CloudReports, payload: dict, start: date, end: date) -> list[dict]:
    dimensions = payload["groupByRowFields"]
    amounts = payload["aggregateFields"]
    date_field = dimensions[0]
    rows = []
    seen = set()
    for row in _object_array(api.read(REPORT, payload), "data", "transaction rows"):
        if not all(name in row for name in dimensions + amounts):
            raise ValueError("iiko returned incomplete transaction fields. Check the saved report before reconciliation.")
        value = row[date_field]
        try:
            day = datetime.fromisoformat(str(value)).date()
        except ValueError:
            raise ValueError("iiko returned an invalid transaction date.") from None
        if not start <= day <= end:
            raise ValueError("iiko returned a transaction outside the requested dates.")
        key = canonical([row[name] for name in dimensions])
        if key in seen:
            raise ValueError("Duplicate OLAP grouping. Recheck the source before reconciling the register.")
        seen.add(key)
        output = {name: row[name] for name in dimensions}
        for name in amounts:
            value = row[name]
            if value is not None:
                try:
                    amount = Decimal(str(value))
                except InvalidOperation:
                    raise ValueError("iiko returned a nonnumeric transaction measure.") from None
                if not amount.is_finite():
                    raise ValueError("iiko returned a nonfinite transaction measure.")
                value = str(amount)
            output[name] = value
        rows.append(output)
    return rows


def transaction_report(api: CloudReports, first: str, last: str, groups: list[str], amounts: list[str],
                       filters: dict[str, list[str]], zone: str | None, limit: int, offset: int, snapshot: str | None) -> dict:
    span = report_span(api, first, last, zone)
    columns = fields(api)
    date_field = next((name for name in ("DateTime.DateTyped", "DateTime.Typed")
                       if columns.get(name, {}).get("canFilter") is True and columns[name].get("canGroup") is True), None)
    if date_field is None:
        raise ValueError("No supported filterable and groupable TRANSACTIONS date field. Try a matching saved iiko report.")
    if len(set(groups + amounts)) != len(groups) + len(amounts) or date_field in amounts or date_field in filters:
        raise ValueError("Use distinct group and measure fields. The connector supplies the date filter.")
    if len(filters) > 8:
        raise ValueError("Use up to eight exact-value filters.")
    dimensions = [date_field, *(name for name in groups if name != date_field)]
    require_columns(columns, {name: ("canGroup",) for name in dimensions})
    require_columns(columns, {name: ("canAggregate",) for name in amounts})
    require_columns(columns, {name: ("canFilter",) for name in filters})
    date_filter = {"name": date_field, "filterType": "DateRange", **span.filter()}
    if date_field == "DateTime.Typed":
        # Use the following midnight to include transactions late on the final date.
        date_filter.update(to=(span.end + timedelta(days=1)).isoformat(), includeHigh=False)
    value_filters = [{"name": name, "filterType": "IncludeValues", "values": values} for name, values in filters.items()]
    body = {"reportType": "TRANSACTIONS", "buildSummary": False, "groupByRowFields": dimensions,
            "aggregateFields": amounts, "filters": [date_filter, *value_filters]}

    if date_field == "DateTime.Typed" and (span.end - span.start).days == 30:
        # Both wire bounds must fit iiko's 31-calendar-date CUSTOM limit.
        rows = []
        for start, end in ((span.start, span.end - timedelta(days=1)), (span.end, span.end)):
            part_filter = {**date_filter, "from": start.isoformat(), "to": (end + timedelta(days=1)).isoformat()}
            rows.extend(posting_rows(api, {**body, "filters": [part_filter, *value_filters]}, start, end))
    else:
        rows = posting_rows(api, body, span.start, span.end)
    args = {"date_from": span.start.isoformat(), "date_to": span.end.isoformat(), "group_fields": groups,
            "amount_fields": amounts, "filters": filters}
    return {"report_type": "TRANSACTIONS", "restaurant": api.restaurant, "organization_currency": api.currency,
            "date_field": date_field, "group_fields": dimensions, "amount_fields": amounts,
            "row_basis": "groups of postings, not necessarily individual transactions or outstanding balances",
            **report_page(rows, {"organization": api.org, **body}, args, limit, offset, snapshot)}
