"""Exercise the same read methods the manager uses, without returning restaurant records."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import cache
from typing import Literal
from uuid import UUID

from .report_calendar import RestaurantCalendar
from .cloud_sales import CloudHTTPError
from .cloud_reports import CloudReports, moment
from .live_sales import LiveSales

CheckGroup = Literal["all", "sales", "catalog", "stock", "documents", "recipes"]


def check_access(api: CloudReports, group: CheckGroup, as_of: str | None, product: str | None, store: str | None,
                 restaurant_timezone: str | None = None) -> dict:
    instant = datetime.now(timezone.utc)
    at = moment(as_of) if as_of else instant.isoformat()
    calendar = RestaurantCalendar(api.terminal_timezones, restaurant_timezone, instant)

    @cache
    def restaurant_time():
        return datetime.fromisoformat(at) if as_of else instant.astimezone(calendar.zone)

    def accounting_day():
        return restaurant_time().date().isoformat()

    checks = {}

    def check(name, operation):
        try:
            result = operation()
            state = result.get("status", "ok")
            if state == "ok" and result.get("rows") == []:
                state = "no_data"
            checks[name] = {"status": state}
            return result
        except CloudHTTPError as exc:
            checks[name] = {"status": "unavailable", "reason": str(exc), **exc.details}
        except ValueError as exc:
            checks[name] = {"status": "unavailable", "reason": str(exc)}
        except (KeyError, TypeError, ArithmeticError, AttributeError):
            checks[name] = {"status": "unavailable", "reason": "Incomplete or unexpected iiko response."}
        return {}

    if group in ("all", "sales"):
        @cache
        def sales():
            return LiveSales(api, restaurant_timezone=restaurant_timezone)
        check("get_sales_summary", lambda: sales().summary("yesterday", "yesterday"))
        check("get_top_dishes", lambda: sales().top("yesterday", "yesterday", "sales", 1))
        check("compare_sales_periods", lambda: sales().compare("yesterday", "yesterday", "today", "today", 1))

    test_recipes = group in ("all", "recipes")
    sample_product, sample_store = product, store
    if group in ("all", "catalog") or (test_recipes and not product):
        filters = [{"field": "isDeleted", "op": "eq", "value": False}]
        if test_recipes and not product:
            filters.append({"field": "type", "op": "eq", "value": "DISH"})
        listing = check("find_iiko_products", lambda: api.products(filters, 1, 0))
        if not product and listing.get("rows"):
            sample_product = listing["rows"][0]["productId"]
    if group in ("all", "catalog") or (test_recipes and not store):
        listing = check("list_iiko_stores", lambda: api.stores(20, 0))
        if not store:
            sample_store = next((row["id"] for row in listing.get("rows", [])
                                 if row.get("id") and not row.get("deleted")), None)
    if group in ("all", "stock"):
        check("get_live_stock", lambda: api.stock(at, store, product, 1, 0))
    if group in ("all", "documents"):
        for kind, tool in (("stocktake", "get_inventory_counts"), ("invoice", "get_incoming_invoices"),
                           ("writeoff", "get_writeoff_acts")):
            listing = check(tool, lambda: api.documents(kind, accounting_day(), accounting_day(), store, 1, 0))
            detail = "get_iiko_document." + kind
            if listing.get("rows"):
                check(detail, lambda: api.document(kind, str(UUID(listing["rows"][0]["documentId"])), "items", 1, 0))
            else:
                checks[detail] = {"status": "not_checked", "reason": "No sample document ID from this list and date."}
    if test_recipes:
        if sample_product:
            check("get_recipe_card", lambda: api.recipe(sample_product, accounting_day(), 1, 0))
        else:
            checks["get_recipe_card"] = {"status": "needs_input", "required": ["product_id"]}
        if sample_product and sample_store:
            check("get_item_cost", lambda: api.cost(sample_product, sample_store, at, 1))
            check("get_recipe_cost", lambda: api.recipe_cost(sample_product, sample_store, restaurant_time().isoformat(), None, 1, 0))
        else:
            required = [key for key, value in (("product_id", sample_product), ("store_id", sample_store)) if not value]
            for name in ("get_item_cost", "get_recipe_cost"):
                checks[name] = {"status": "needs_input", "required": required}

    incomplete = {"unavailable", "partial", "needs_input", "not_checked"}
    return {"status": "partial" if any(r["status"] in incomplete for r in checks.values()) else "ok",
            "restaurant": api.restaurant, "checked_at": datetime.now(timezone.utc).isoformat(), "as_of": at,
            "recipe_sample": {"product_id": sample_product, "store_id": sample_store} if test_recipes else None,
            "checks": checks}
