"""Only direct read operations offered by the manager connector."""
PRODUCTS = "/api/nomenclature/v2/product/list"
RECIPE = "/api/nomenclature/v3/assembly-chart/resolve"
STORES = "/api/inventory/v2/storages/list"
COST = "/api/inventory/v2/costings/calculate"
STOCK = "/api/inventory/v1/stock_balance/list"
UNITS = "/api/inventory/v1/measure_units/list"
SUPPLIER = "/api/inventory/v1/counteragents/get"
DOCUMENTS = {
    "stocktake": ("/api/inventory/v2/incoming_inventory/list", "/api/inventory/v1/incoming_inventory/get"),
    "invoice": ("/api/inventory/v1/incoming_invoice/list", "/api/inventory/v1/incoming_invoice/get"),
    "writeoff": ("/api/inventory/v2/writeoff_document/list", "/api/inventory/v2/writeoff_document/get"),
}
LIVE_READ_PATHS = {PRODUCTS, RECIPE, STORES, COST, STOCK, UNITS, SUPPLIER}
for listing, detail in DOCUMENTS.values():
    LIVE_READ_PATHS.update((listing, detail))
