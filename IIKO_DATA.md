# What the manager can ask

The public Windows release v0.3.1 offers eight MCP tools. The local Windows build v0.4.2 also offers ten tools that read from the Russian iiko Cloud API. Use `get_data_status` to check the local snapshot and Cloud configuration separately. A configured profile identifies an organization. Each API method needs permission at that restaurant.

| Question | Public v0.3.1 | Additional MCP methods in local v0.4.2 |
| --- | --- | --- |
| What data do we have? | `get_capabilities`, `get_data_status` | Both also report Cloud configuration. |
| What sold and which dishes explain a change? | `get_sales_summary`, `get_top_dishes`, `compare_sales_periods` on a local snapshot | The existing sales collector can refresh the snapshot when run separately. |
| What goes into a salad? | No restaurant recipe | `find_iiko_products` finds the dish. `get_recipe_card` reads the card effective on a date, including ingredients, gross and net quantities, yield and preparation text where available. |
| How much does it cost? | Demo costs only | `get_item_cost` reads iiko's accounting cost for a product, quantity, warehouse and time. `get_recipe_cost` calculates ingredient cost for the recipe yield and per output unit from the flattened recipe and the selected warehouse's unit prices. Missing or ambiguous prices prevent a total. This calculation excludes labour and overhead. |
| What is on hand? | `get_stock_balances` reads an imported, dated warehouse balance. | `list_iiko_stores` finds warehouses. `get_live_stock` queries quantity and value by warehouse, product and timestamp. iiko returns rows with nonzero quantity or value. |
| What did the team count? | No stocktake documents | `get_inventory_counts` lists inventory documents. `get_iiko_document` reads their count lines and first-stage lines. Surpluses and shortages are reported when supplied by iiko. |
| What deliveries arrived? | `get_purchases` summarizes imported posted invoice headers. | `get_incoming_invoices` lists documents. `get_iiko_document` reads quantities, prices and VAT fields. |
| What did we write off? | `get_writeoffs` summarizes imported posted act headers. | `get_writeoff_acts` lists documents. `get_iiko_document` reads product quantities and units. |

The manager can ask in Russian. The local MCP client picks the methods. For example, «Найди салат “Цезарь” и покажи его техкарту на 20 ноября», «Рассчитай стоимость закладки и единицы выхода по ценам склада “Кухня”», «Открой накладную № 0128 за неделю и покажи позиции». If a name matches several dishes or warehouses, choose the right one. For a date/time report, use the restaurant's UTC offset. Follow `next_offset` for subsequent rows. Check document status before counting posted activity. Directory names and units are current labels and can differ from historical labels.

Cloud methods use an organization-specific credential. Verify a known recipe, warehouse and document against iikoOffice on first use. The restaurant's deployment and licence determine access. The ordinary browser text report includes snapshot reports only. Classic iikoServer REST, writing documents, a hosted browser connector and automatic recurring refresh have no implementation in this build.

[Official iiko Cloud API specification](https://api-ru.iiko.services/api-docs/docs) · [Assembly charts](https://api.iiko.help/docs/en/guides/assembly-charts/) · [Inventory](https://api.iiko.help/docs/en/guides/inventory/)
