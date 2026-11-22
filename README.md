# iiko Intelligence

iiko Intelligence gives one manager read-only reports on sales, dishes, stock, supplier invoices and write-offs. Use them in local Codex in the ChatGPT desktop app, or create a text report for an ordinary chat. Connect a supported iiko Cloud reporting profile for live answers or load an iikoOffice export for offline reports.

## Windows

1. Give the manager the first-install prompt and the installer hash through your usual trusted chat. Local Codex downloads the signed Windows package from GitHub and sets it up under Documents. [Installation prompt](INSTALL.md) · [Windows release](https://github.com/ktsAE/iiko-intelligence/releases/tag/v0.5.1)
2. Open **Documents → iiko-intelligence** in local Codex and trust the project. Use this same folder for reports and updates. To check the connection, enter `/mcp` and look for `iiko_intelligence`.
3. Run `Connect-iiko-Cloud.cmd` once to save the restaurant’s single-organization Cloud profile. Then ask **«Какая была выручка вчера?»** or **«Какие отчёты доступны онлайн?»**. The MCP reads sales directly from iiko and can check each reporting area.
4. To work from an offline snapshot, run `Import-iiko-report.cmd` with the export described below. For ordinary ChatGPT, double-click `Report-for-ChatGPT.cmd`, review the text file in `reports`, and attach it to a chat. The report contains the selected restaurant, dates, sales by day, top dishes and, when imported, the warehouse valuation. [Windows guide in Russian](WINDOWS.md)

### Updating the manager's connector

While the Windows project is open, the connector checks stable GitHub releases daily and accepts only immutable releases for updates. **«Обнови iiko»** asks local Codex to check immediately. Both installation and update require an RSA-3072/SHA-256 signature from the pinned publisher key. The signed record binds the repository, version, Windows asset name, size, archive digest and installed-file digest. The candidate MCP runs a synthetic health check with a disposable profile and a limited environment. The launcher verifies the selected files on each new launch. The ordinary manager workflow keeps its reports and credentials in the original project.

The signing key belongs on the publisher's trusted machine or offline storage. It has no role on the manager's laptop or in GitHub Actions. Build and test reviewed source. On the signing machine run `uv run --locked python scripts/sign_windows_release.py --key /path/to/private/publisher-v1.pem`. Attach the ZIP, its `.zip.sig` and `Install-iiko.ps1` to a draft GitHub release. Publish after verification. Immutable releases lock their attached assets. Share the bootstrap digest separately for first installation and keep a secure backup of the private key. A signing-key loss or compromise calls for a fresh trusted bootstrap. Windows file permissions on the original trusted installation and the security of the user's account still matter.

## Import an iikoOffice export

In iikoOffice, create a **Sales OLAP report** for one outlet and a closed period. Group rows by accounting day, product code and dish. Include quantity and sales after discounts. Use the native report filters for the approved outlet, include the same items in both the export and the total, and make sure the file has one row per product per day. Export it as `.xlsx`, Excel 97–2003 `.xls`, or `.csv`. The importer accepts grouped dates if each day with blank date cells has a numeric subtotal such as `01.09.2026 Всего`. It reconciles those rows by day. For other layouts, repeat the date on each detail row and use Excel’s value-only copy for data columns containing formulas. [iikoOffice SALES fields](https://ru.iiko.help/smart/project-iikooffice/olap-sales) · [iiko’s Excel export instructions](https://ru.iiko.help/article/iikoweb/prep)

| Required field | iikoOffice label |
| --- | --- |
| `business_date` | Учетный день |
| `product_code` | Код блюда |
| `dish` | Блюдо |
| `quantity` | Количество блюд |
| `sales_after_discounts` | Сумма со скидкой |

Keep the raw file outside the folder you open in Codex. When prompted, enter the source period and the matching iikoOffice total. The import replaces the prior snapshot only if the detail rows add up to that total exactly. To compare two weeks, export a single period covering both. [Example column layout](examples/sales-example.csv)

For offline comparisons use `compare_sales_periods` with `source="snapshot"` and two ranges covered by the same export. It returns each period’s length, days with records, amount and percentage change, and the dishes contributing most to the difference. Dish rankings support revenue and quantity. Refund and tax treatment follows the source report. The Cloud collector can include order count. The snapshot only has recipe costs in demo mode. The separate Cloud recipe-cost tool uses the effective recipe and warehouse prices.

## Warehouse balances

After importing sales, export **Остатки на складах** for one warehouse on one selected date. Use a flat report with one row per item and include **Артикул**, **Наименование**, **Ед. изм.**, **Количество**, and the **Себестоимость** column containing the full value of the row. Enter the selected warehouse, date, native report total and the same restaurant name and currency as the sales report in `Import-iiko-stock.cmd`. The import accepts XLSX, XLS or CSV and checks the sum of all item values. A successful import replaces the previous warehouse snapshot. Sales refreshes for the same restaurant label and currency retain it with its own date. Use a consistent, unique label for each outlet. Ask the MCP for the highest-value items or for negative balances. [iikoOffice manual, warehouse balances](https://ru.iiko.help/resources/Storage/archive/PDF/RU_iikoOffice_8.0.pdf)

## Purchases and write-offs

Use a closed-period list of posted, active incoming supplier invoices or warehouse write-off acts for one restaurant. Prepare one row per document using the [purchase template](examples/purchases-template.csv) or [write-off template](examples/writeoffs-template.csv). Fill in the document date, number, type, posting status, supplier or warehouse, and document amount in the report currency. Use `incoming_invoice` or `writeoff_act` for the document type and `posted` for confirmed posted, active records. Exclude returns, internal transfers, deleted records and drafts. In versions of iikoOffice that show a state through color or an icon, check it in iikoOffice before filling the template. [iikoOffice invoice register](https://ru.iiko.help/resources/Storage/archive/PDF/RU_iikoOffice_6.3.pdf)

Run `Import-iiko-documents.cmd` and enter the native list count and total. The import requires both controls to match. Each document type has its own dates and refreshes independently. The MCP returns full-period document totals, supplier or warehouse totals, and a bounded list of documents ranked by amount. The amount is the document face value. Cash payments, dish-level costs and profit require other sources.

## Set up and run on Linux

```sh
uv sync --locked --python 3.11
uv run --locked iiko-intelligence setup-local
uv run --locked iiko-intelligence check-mcp
```

The setup command generates this folder’s `.codex/config.toml` and verifies a real stdio MCP subprocess. It leaves any already imported data in place. Open the project in a local Codex client. For hosts with manual stdio setup, use the Python executable and argument list shown in `.codex/config.toml`.

```sh
uv run --locked iiko-intelligence import-sales --file /path/to/report.xlsx \
  --from 2026-09-01 --to 2026-09-07 --iiko-total 82130.50 \
  --restaurant 'My restaurant' --currency RUB
uv run --locked iiko-intelligence status
uv run --locked iiko-intelligence sales --from 2026-09-01 --to 2026-09-07
```

The import also works before local setup. A workbook with one matching sales sheet is selected automatically. Use `--sheet` when more than one worksheet has the required headers. Dates in Excel can be typed dates, `YYYY-MM-DD`, or `DD.MM.YYYY`. Monetary amounts can use a comma or point as the decimal mark. Values with more precision than the currency supports are rejected.

## iiko Cloud reporting collector

Ask the iiko administrator to confirm the restaurant’s product, reporting entitlement, and organization scope. For the documented Russian Cloud API host, the collector supports a dedicated key limited to one organization and one unique configured outlet. It uses iiko’s v2 token endpoint, verifies required OLAP fields from metadata, caps runs to 31-day query windows, checks the outlet and date on every returned row, and reconciles daily and by-dish reports before comparing their total to the iikoOffice figure. Output is the same SQLite snapshot, with order counts when iiko returns them. [iiko Cloud reports](https://api.iiko.help/docs/en/guides/analytics-and-reports/) · [Cloud API schema](https://api-ru.iiko.services/api-docs/docs) · [Access and data coverage](IIKO_DATA.md)

On Linux, run the command below from an ordinary terminal outside your Codex chat. On Windows double-click `Sync-iiko-Cloud.cmd` and enter the organization and report details. Enter the application ID, client secret, and organization API key into the terminal prompts. For managed runs set `IIKO_CLOUD_APP_ID`, `IIKO_CLOUD_CLIENT_SECRET`, and `IIKO_CLOUD_API_KEY` in the collector process only.

```sh
uv run --locked iiko-intelligence sync-cloud-sales \
  --organization '00000000-0000-0000-0000-000000000000' --outlet 'Exact OLAP outlet name' \
  --from 2026-09-01 --to 2026-09-07 --iiko-total 82130.50 \
  --restaurant 'My restaurant' --currency RUB
```

For the first live test, compare a closed day and a closed month against the native sales report with matching filters. Keep API secrets out of chat, files in the project, and the MCP configuration. The collector implementation has fixture tests. The target restaurant’s permissions, report columns, and numbers still require verification. For a classic iikoServer installation, use the export path until the installation’s integrator can supply the version-specific Server API contract and access.

## Using it in ChatGPT in a browser

The Windows setup connects local Codex. For a regular chat with file uploads, attach a reviewed text report produced by `Report-for-ChatGPT.cmd`. A direct browser MCP connection requires an account that can add a custom MCP server. [ChatGPT file uploads](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt) If that option is available, an approved OpenAI Secure MCP Tunnel can expose this same **stdio** command to ChatGPT without publishing the iiko server. A permitted Platform project and the proper ChatGPT workspace association are required. Use the Python command and the database path from `.codex/config.toml` for the tunnel’s local server, and keep the collector’s credentials outside the tunnel. In ChatGPT use **Plugins → + → Add custom MCP server → Tunnel**, and verify with demo data first. [ChatGPT custom server availability](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) · [Secure MCP Tunnel setup](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

The local command for a tunnel profile on Linux looks like this. Use absolute paths from your own setup. On Windows use `runtime\python.exe` in that command. Follow the tunnel client’s onboarding guide to provision the ID and runtime key, generate the `sample_mcp_stdio_local` profile, and run its `doctor` check. [Tunnel client onboarding](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md)

```sh
/absolute/path/iiko-intelligence/.venv/bin/python -m iiko_intelligence.mcp_server \
  --db /absolute/path/iiko-intelligence/data/manager.sqlite
```

## Development

The release smoke test requires Wine on Linux. It creates its own temporary Windows prefix and exercises the generated ZIP. To run it with external networking disabled on a Linux host with user namespaces, use `unshare -Urn sh -c 'ip link set lo up && exec .venv/bin/python scripts/smoke_windows_bundle.py'` after signing the reviewed build.

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked python scripts/build_windows_bundle.py
uv run --locked python scripts/sign_windows_release.py --key /path/to/private/publisher-v1.pem
uv run --locked python scripts/smoke_windows_bundle.py
```

The build writes `dist/iiko-intelligence-windows-x64.zip`. The builder verifies the Windows Python archive against the official SHA-256 and uses hashes from `uv.lock` for Windows wheels. Build on a machine with `uv` and internet access. [Python release](https://www.python.org/downloads/release/python-31410/) The ZIP contains the runtime, application and guides. Restaurant snapshots, generated reports and project-local MCP configuration stay on the manager’s computer.
