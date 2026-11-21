# What can the connector read?

The installed MCP reads checked local snapshots. Ask it for `get_capabilities` and `get_data_status` to see what is available for the current restaurant and dates.

| Question | Available in this release | Additional iiko methods |
| --- | --- | --- |
| What sold, and which dishes explain a change? | Sales, quantities, rankings and period comparisons from an iikoOffice sales export or the supported Russian iiko Cloud OLAP collector. Cloud can also provide order counts when enabled. | Reporting metadata and OLAP queries can expose further dimensions permitted for the restaurant. |
| What is on hand? What did we buy or write off? | A dated warehouse balance and closed-period lists of posted invoices and write-offs from exports. Document results include counts and totals. | Cloud documents stock balances, incoming invoice details, write-off details, transfers and stocktakes. These require additional collectors. |
| What are the ingredients and recipe costs? | Recipe costs in the demo are synthetic. | Cloud has product, recipe history, prepared assembly-chart and costing methods. Actual cost also depends on recipe date, units, valuation and source prices. |
| What do finance and staffing show? | These sources have no collector in this release. | Cloud documents accounts, postings, balances, employee schedules and attendance. Full payroll or profit may require another accounting source. |

On 21 November 2026, the official Russian Cloud OpenAPI specification listed **417 distinct paths**, including both reads and writes. The count describes the API catalogue. The methods available to one restaurant depend on its deployment, licence and credentials. The application’s Cloud collector currently calls organization discovery, sales report field metadata and sales OLAP, plus authentication. [Official Cloud specification](https://api-ru.iiko.services/api-docs/docs) · [iiko developer guide](https://api.iiko.help/quickstart)

For a specific restaurant, ask its iiko administrator or integrator for the backend product and version, Cloud region or iikoServer deployment, enabled read permissions, organization and outlet, and available history. Request credentials through the restaurant’s normal private channel. Use one closed day and the matching iikoOffice total to validate an initial sales connection. A classic iikoServer deployment can use the export route while the integrator confirms version-specific REST access. [iiko documentation](https://ru.iiko.help/home/ru-ru/)
