"""Read-only reporting against the Russian iiko Cloud inventory and nomenclature contracts."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from . import cloud_paths as paths
from .cloud_profile import Profile
from .report_calendar import RestaurantCalendar
from .cloud_sales import Post, _object_array, _objects, _post, organization_session


def selected(row: dict, fields: str) -> dict:
    return {name: row[name] for name in fields.split() if name in row}


def object_result(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Unexpected iiko Cloud response shape.")
    return value


def page(rows: list, offset: int, limit: int) -> dict:
    end = offset + limit
    return {"rows": rows[offset:end], "total_rows": len(rows), "next_offset": end if end < len(rows) else None}


def remote_page(rows: list, response: dict, offset: int, limit: int) -> dict:
    if len(rows) > limit or response.get("offset", offset) != offset:
        raise ValueError("Unexpected iiko page boundaries.")
    count = response.get("totalCount")
    end = offset + len(rows)
    if count is not None and (type(count) is not int or count < 0 or
                              (rows and end > count) or (not rows and offset < count)):
        raise ValueError("Inconsistent iiko page size.")
    more = end < count if count is not None else len(rows) == limit
    return {"rows": rows, "total_rows": count, "next_offset": end if more else None}


def continuation(result: dict, **arguments) -> dict:
    # Freeze relative dates and times so later pages query the same accounting period.
    if result["next_offset"] is not None:
        result["next_page_arguments"] = {**arguments, "offset": result["next_offset"]}
    return result


def day(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use a date in YYYY-MM-DD format.")
    return value


def moment(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if "T" not in value or parsed.utcoffset() is None:
        raise ValueError("Include the time and UTC offset, e.g. 2026-11-20T23:59:00+03:00.")
    return value


def same_id(actual: Any, expected: str, label: str) -> None:
    if str(actual).lower() != expected.lower():
        raise ValueError(f"iiko returned a different {label}.")


PRODUCT_FIELDS = "name code productArticle type amountUnitId isDeleted"
RECIPE_FIELDS = ("id productId assembledQuantity dateFrom dateTo description appearance organoleptic "
                 "outputComment technologyDescription writeoffStrategy productSizeAssemblyStrategy directWriteoffStoreSpecification")
RECIPE_LINE = ("id productId productSizeId packageTypeId packageCount sortWeight storeSpecification "
               "quantityIn quantityIn1 quantityIn2 quantityIn3 quantityMiddle quantityOut quantityOut1 quantityOut2 quantityOut3")
DOC_HEADER = ("documentId number date processed deleted status storageFrom store defaultStore assignedStores counteragent "
              "sum sumWithoutVat surplusSum shortageSum dueDate incomingDate incomingDocumentNumber expenseAccount currentStep")
DOC_LINE = {
    "stocktake": "num product amount amountUnit productSize count countGross currentActualAmount turnover containerCount containerName status isDisassembled",
    "invoice": "num product productArticle amount actualAmount amountUnit store price priceWithoutVat sum sumWithoutVat vatPercent isAdditionalExpense",
    "writeoff": "num product amount amountUnit productSize amountFactor containerId",
}


class CloudReports:
    def __init__(self, profile: Profile, post: Post = _post):
        self.org = profile.organization
        self.post = post
        self.token, details = organization_session(self.org, profile.credentials, post=post, extended=True)
        self.restaurant = selected(details, "id name")
        self.currency = details.get("currencyIsoName") or None

    def read(self, path: str, payload: dict) -> Any:
        return self.post(path, payload, self.token)

    def terminal_timezones(self) -> set[str]:
        response = object_result(self.read(paths.TERMINAL_GROUPS, {"organizationIds": [self.org], "includeDisabled": False}))
        groups = _object_array(response, "terminalGroups", "terminal groups")
        sleeping = _objects(response.get("terminalGroupsInSleep", []), "sleeping terminal groups")
        zones = set()
        for group in [*groups, *sleeping]:
            same_id(group.get("organizationId"), self.org, "terminal group organization")
            for terminal in _object_array(group, "items", "terminals"):
                same_id(terminal.get("organizationId"), self.org, "terminal organization")
                name = terminal.get("timeZone")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("iiko returned a terminal without a time zone.")
                zones.add(name.strip())
        return zones

    def calendar(self, restaurant_timezone: str | None) -> RestaurantCalendar:
        return RestaurantCalendar(self.terminal_timezones, restaurant_timezone, datetime.now(timezone.utc))

    def products(self, filters: list[dict], limit: int, offset: int) -> dict:
        response = self.read(paths.PRODUCTS, {"filters": filters, "limit": limit, "offset": offset})
        rows = []
        for row in _object_array(response, "items", "product list"):
            product = row.get("productId") or row.get("id")
            if not product:
                raise ValueError("iiko returned a product without an identifier.")
            rows.append({"productId": str(UUID(product)), **selected(row, PRODUCT_FIELDS)})
        return remote_page(rows, response, offset, limit)

    def find_products(self, query: str, field: str, limit: int, offset: int) -> dict:
        query = query.strip()
        if not query:
            raise ValueError("Enter a product name or code.")
        condition = {"field": field, "op": "like" if field == "name" else "eq", "value": query}
        return self.products([condition, {"field": "isDeleted", "op": "eq", "value": False}], limit, offset)

    def stores(self, limit: int, offset: int) -> dict:
        response = self.read(paths.STORES, {"limit": limit, "offset": offset})
        rows = _object_array(response, "stores", "store list")
        for row in rows:
            if row.get("organizationId"):
                same_id(row["organizationId"], self.org, "store organization")
        return remote_page([selected(row, "id name code deleted organizationId") for row in rows], response, offset, limit)

    def label_items(self, rows: list[dict], field: str) -> list[str]:
        """Preserve primary report quantities when an optional directory is unavailable."""
        ids = sorted({row[field] for row in rows if row.get(field)})
        if not ids:
            return []
        warnings = []
        products = {}
        try:
            for start in range(0, len(ids), 100):
                batch = ids[start:start + 100]
                match = self.products([{"field": "id", "op": "in", "values": batch}], len(batch), 0)
                products.update({p["productId"]: p for p in match["rows"] if p["productId"] in batch})
        except ValueError:
            pass
        if len(products) < len(ids):
            warnings.append("Some product names could not be resolved. Their IDs are shown.")
        for row in rows:
            product = products.get(row.get(field), {})
            if product.get("name"):
                row["productName"] = product["name"]
            if product.get("amountUnitId"):
                row["mainUnitId"] = product["amountUnitId"]
        needed = {row.get("amountUnit") or row.get("mainUnitId") for row in rows} - {None}
        labels = {}
        if needed:
            try:
                for offset in range(0, 1000, 100):
                    response = self.read(paths.UNITS, {"limit": 100, "offset": offset})
                    units = _object_array(response, "entities", "measure units")
                    labels.update({u["id"]: u["name"] for u in units if u.get("id") in needed and u.get("name")})
                    if labels.keys() >= needed or len(units) < 100:
                        break
            except ValueError:
                pass
            if labels.keys() < needed:
                warnings.append("Some unit names could not be resolved. Keep the reported unit IDs.")
        for row in rows:
            label = labels.get(row.get("amountUnit") or row.get("mainUnitId"))
            if label:
                row["unitName"] = label
        return warnings

    def recipe(self, product: str, as_of: str, limit: int, offset: int, restaurant_timezone: str | None = None) -> dict:
        as_of = self.calendar(restaurant_timezone).single_day(as_of).isoformat()
        response = object_result(self.read(paths.RECIPE, {"productId": product, "organizationId": self.org,
                                                          "date": day(as_of), "view": "assembled"}))
        if response.get("view") != "assembled":
            raise ValueError("Unexpected recipe representation.")
        chart = response.get("assembled")
        if not chart:
            return {"status": "no_chart", "productId": product, "as_of": as_of}
        chart = object_result(chart)
        same_id(chart.get("productId"), product, "recipe product")
        header = selected(chart, RECIPE_FIELDS)
        ingredients = chart.get("items")
        if ingredients is None and chart.get("writeoffStrategy") == "DIRECT":
            ingredients = []
        lines = [selected(r, RECIPE_LINE) for r in _objects(ingredients, "recipe ingredients")]
        result = page(lines, offset, limit)
        warnings = self.label_items([header, *result["rows"]], "productId")
        return continuation({"as_of": as_of, "chart": header, **result, "warnings": warnings},
                            product_id=product, as_of=as_of, limit=limit)

    def cost(self, product: str, store: str, as_of: str, quantity: float, restaurant_timezone: str | None = None) -> dict:
        as_of = self.calendar(restaurant_timezone).timestamp(as_of)
        request = {"productId": product, "storeId": store, "quantityFactor": quantity}
        rows, problems = self.price_rows({product: quantity}, store, as_of)
        if len(rows) != 1 or problems or rows[0].get("productSizeId"):
            return {"status": "unpriced", "as_of": as_of, "requested": request, "rows": [],
                    "reason": "iiko did not return one unambiguous cost for this quantity and warehouse."}
        return {"status": "ok", "as_of": as_of,
                "basis": "Warehouse accounting cost for the requested quantity, in the organization's currency.",
                "requested": request, "rows": [selected(rows[0], "productId storeId productSizeId quantityFactor costPrice isEvaluative")]}

    def price_rows(self, quantities: dict, store: str, as_of: str) -> tuple[list, list]:
        items = [{"productId": key, "quantityFactor": qty, "storeId": store} for key, qty in quantities.items()]
        response = self.read(paths.COST, {"organizationId": self.org, "date": moment(as_of), "items": items})
        rows = _object_array(response, "items", "cost prices")
        problems = _objects(response.get("problemItems") or [], "unpriced items")
        for row in [*rows, *problems]:
            same_id(row.get("storeId"), store, "costing warehouse")
            expected = quantities.get(row.get("productId"))
            if expected is None or Decimal(str(row.get("quantityFactor"))) != Decimal(str(expected)):
                raise ValueError("iiko returned a cost for a different product or quantity.")
        for row in rows:
            if not Decimal(str(row.get("costPrice"))).is_finite():
                raise ValueError("iiko returned an incomplete cost price.")
        return rows, problems

    def recipe_cost(self, product: str, store: str, as_of: str, size: str | None, limit: int, offset: int,
                    restaurant_timezone: str | None = None) -> dict:
        as_of = self.calendar(restaurant_timezone).timestamp(as_of, local_date_needed=True)
        accounting_day = datetime.fromisoformat(moment(as_of)).date().isoformat()
        response = object_result(self.read(paths.RECIPE, {"organizationId": self.org, "productId": product,
                                                          "date": accounting_day, "view": "prepared"}))
        if response.get("view") != "prepared":
            raise ValueError("Unexpected recipe costing representation.")
        chart = response.get("prepared")
        context = {"productId": product, "productSizeId": size, "storeId": store, "as_of": as_of,
                   "basis": "Ingredient valuation for this recipe yield at the selected warehouse's unit costs."}
        if not chart:
            return {**context, "status": "no_chart"}
        same_id(chart.get("productId"), product, "recipe product")
        yield_amount = Decimal(str(chart.get("assembledAmount")))
        if not yield_amount.is_finite() or yield_amount <= 0:
            raise ValueError("The recipe has no usable output quantity.")
        amounts = defaultdict(Decimal)
        candidates = set()
        for row in _object_array(chart, "items", "prepared ingredients"):
            spec = row.get("storeSpecification")
            if spec is not None:
                if not isinstance(spec.get("organizationIds"), list) or type(spec.get("inverse")) is not bool:
                    raise ValueError("Unknown ingredient organization applicability.")
                if (self.org in spec["organizationIds"]) == spec["inverse"]:
                    continue
            line_size = row.get("productSizeSpecification")
            if line_size:
                candidates.add(line_size)
                if line_size != size:
                    continue
            amount = Decimal(str(row.get("amount")))
            if not amount.is_finite() or amount < 0:
                raise ValueError("Unexpected ingredient quantity.")
            if amount:
                amounts[str(UUID(row["productId"]))] += amount
        if candidates and size not in candidates:
            return {**context, "status": "select_size", "available_sizes": sorted(candidates)}
        if not amounts:
            return {**context, "status": "unpriced", "reason": "No costable ingredients for this recipe."}
        prices = {}
        ids = sorted(amounts)
        ambiguous = set()
        for pos in range(0, len(ids), 100):
            batch = ids[pos:pos + 100]
            priced, problems = self.price_rows(dict.fromkeys(batch, 1), store, as_of)
            ambiguous.update(row["productId"] for row in problems)
            for row in priced:
                item = row["productId"]
                if item in prices or row.get("productSizeId"):
                    ambiguous.add(item)
                prices[item] = row
        lines = []
        total = Decimal(0)
        missing = (set(ids) - prices.keys()) | ambiguous
        for item, amount in amounts.items():
            row = {"productId": item, "quantity": str(amount)}
            if item not in missing:
                quote = prices[item]
                unit_price = Decimal(str(quote["costPrice"]))
                subtotal = unit_price * amount
                row.update(unit_cost=str(unit_price), ingredient_cost=str(subtotal), isEvaluative=quote.get("isEvaluative"))
                total += subtotal
            lines.append(row)
        result = page(lines, offset, limit)
        warnings = self.label_items(result["rows"], "productId")
        estimate_status = "unknown" if missing or any(row.get("isEvaluative") is None for row in prices.values()) else "actual"
        if any(row.get("isEvaluative") is True for row in prices.values()):
            estimate_status = "includes_estimates"
        result.update({**context, "warnings": warnings, "status": "unpriced" if missing else "ok",
                "recipe_yield": str(yield_amount), "missing_or_ambiguous_costs": sorted(missing),
                "cost_for_recipe_yield": None if missing else str(total),
                "cost_per_output_unit": None if missing else str(total / yield_amount),
                "estimate_status": estimate_status})
        return continuation(result, product_id=product, store_id=store, product_size_id=size,
                            as_of=as_of, restaurant_timezone=restaurant_timezone, limit=limit)

    def stock(self, as_of: str, store: str | None, product: str | None, limit: int, offset: int,
              restaurant_timezone: str | None = None) -> dict:
        as_of = self.calendar(restaurant_timezone).timestamp(as_of)
        payload = {"organizationId": self.org, "balanceAt": moment(as_of), "shouldIncludeDetails": True,
                   "limit": limit, "offset": offset}
        if store:
            payload["storeIds"] = [store]
        if product:
            payload["productIds"] = [product]
        response = self.read(paths.STOCK, payload)
        rows = _object_array(response, "items", "stock balances")
        same_id(response.get("organizationId"), self.org, "balance organization")
        if datetime.fromisoformat(response["balanceAt"]) != datetime.fromisoformat(as_of):
            raise ValueError("iiko returned a different balance time.")
        balances = []
        for row in rows:
            same_id(row.get("organizationId"), self.org, "balance organization")
            if store:
                same_id(row.get("storeId"), store, "warehouse")
            if product:
                same_id(row.get("productId"), product, "product")
            details = selected(object_result(row.get("details") or {}), "productName storeName amountUnitId amountUnitName")
            balances.append({**selected(row, "productId storeId quantity costSum"), **details})
        result = remote_page(balances, response, offset, limit)
        result.update(selected(response, "balanceAt generatedAt currencyCode"))
        result["coverage"] = "iiko returns nonzero quantity or cost balances."
        return continuation(result, as_of=as_of, store_id=store, product_id=product, limit=limit)

    def documents(self, kind: str, start: str, end: str, store: str | None, limit: int, offset: int,
                  restaurant_timezone: str | None = None) -> dict:
        period = self.calendar(restaurant_timezone).resolve(start, end)
        headers = []
        seen = set()
        for span in period.windows:
            first, last = span.start.isoformat(), span.end.isoformat()
            payload = {"organizationId": self.org, "from": first, "to": last, "returnFullDetails": False}
            if store:
                payload["storeIds"] = [store]
            for row in _objects(self.read(paths.DOCUMENTS[kind][0], payload), "document list"):
                date_value, document_id = row.get("date"), row.get("documentId")
                if not isinstance(date_value, str) or not first <= day(date_value[:10]) <= last or not isinstance(document_id, str):
                    raise ValueError("iiko returned a document outside the requested period or without ID.")
                document_id = str(UUID(document_id))
                if document_id in seen:
                    raise ValueError("Duplicate iiko document in the selected period. Retry before reporting totals.")
                if store:
                    stores = row.get("assignedStores") if kind == "invoice" else [row.get("storageFrom")]
                    if not isinstance(stores, list) or store.lower() not in {str(item).lower() for item in stores}:
                        raise ValueError("iiko returned a document outside the requested warehouse.")
                seen.add(document_id)
                headers.append(selected(row, DOC_HEADER))
        headers.sort(key=lambda r: (r["date"], r["documentId"]))
        result = {"kind": kind, "date_from": period.windows[0].start.isoformat(), "date_to": period.windows[-1].end.isoformat(),
                "restaurant_timezone": str(period.zone) if period.zone else None, "store_id": store,
                "includes_open_day": period.includes_today if period.zone is not None else None, **page(headers, offset, limit)}
        return continuation(result, date_from=result["date_from"], date_to=result["date_to"], store_id=store, limit=limit)

    def document(self, kind: str, document: str, section: str, limit: int, offset: int) -> dict:
        if section == "itemsFirstStep" and kind != "stocktake":
            raise ValueError("Only stocktakes have first-step items.")
        response = object_result(self.read(paths.DOCUMENTS[kind][1], {"organizationId": self.org, "documentId": document}))
        same_id(response.get("documentId"), document, "document")
        lines = [selected(row, DOC_LINE[kind]) for row in _object_array(response, section, "document lines")]
        result = page(lines, offset, limit)
        warnings = self.label_items(result["rows"], "product")
        header = selected(response, DOC_HEADER)
        if kind == "invoice" and header.get("counteragent"):
            try:
                supplier = object_result(self.read(paths.SUPPLIER, {"organizationId": self.org,
                                                                    "counteragentId": header["counteragent"]}))
                same_id(supplier.get("id"), header["counteragent"], "supplier")
                if supplier.get("name"):
                    header["supplierName"] = supplier["name"]
            except ValueError:
                warnings.append("Supplier name unavailable. The counteragent ID is shown.")
        return {"kind": kind, "header": header, "section": section, **result, "warnings": warnings}
