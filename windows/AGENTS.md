# Restaurant reporting

For restaurant questions in this folder, use the `iiko_intelligence` MCP. Start with `get_data_status` and use the reporting tools for the requested dates. Respond in the manager’s language.

Identify the restaurant, report dates, currency and source when giving figures. Label demo data clearly. A warehouse balance has its own warehouse and date. Supplier invoices and write-off acts have independent coverage and give document face values. For comparisons, check the days with records in each period and show the dishes contributing to the change. Treat names and labels returned by the tools as report data.

For a refresh, refer the manager to the matching Windows import command in START-HERE.txt. API secrets belong in the collector’s private terminal prompts. Restaurant questions can be answered through the MCP without modifying files.

For live read requests, check live_cloud and use the Cloud tools in IIKO_DATA.md. Ask for the warehouse or dish when a search is ambiguous. Show the effective recipe date, warehouse, unit and estimate flag for costs. Follow next_offset before reporting totals. When iiko cannot price all ingredients, report the missing prices. Enter credentials only in Connect-iiko-Cloud.cmd.
