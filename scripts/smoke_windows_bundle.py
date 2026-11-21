"""Exercise the shipped Windows programs under Wine with disposable synthetic reports."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "dist/iiko-intelligence-windows-x64.zip"
VERIFY = """
import asyncio, tomllib
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def verify():
    params = tomllib.loads(Path('.codex/config.toml').read_text(encoding='utf-8'))['mcp_servers']['iiko_intelligence']
    async with Client(StdioServerParameters(**params), read_timeout_seconds=15) as client:
        listing = await client.list_tools()
        assert len(listing.tools) == 8 and all(t.annotations.read_only_hint for t in listing.tools)
        async def call(name, **args):
            reply = await client.call_tool(name, args)
            assert not reply.is_error, reply
            return reply.structured_content
        status = await call('get_data_status')
        assert status['restaurant']['name'] == 'Кафе' and status['stock_snapshot']['passed']
        assert status['reconciliation']['iiko_report_total'] == '110.50'
        result = await call('compare_sales_periods', baseline_from='2026-09-01', baseline_to='2026-09-01',
                            current_from='2026-09-02', current_to='2026-09-02', limit=1)
        assert (result['change'], result['other_dishes_change']) == ('-89.50', '-9.50')
        stock = await call('get_stock_balances', negative_only=True)
        assert stock['as_of'] == '2026-08-31' and stock['total_value'] == '590.00'
        assert [r['quantity'] for r in stock['rows']] == ['-0.250']
        for tool, amount in [('get_purchases', '90.20'), ('get_writeoffs', '2.20')]:
            report = await call(tool, date_from='2026-08-01', date_to='2026-08-31')
            assert report['totals'] == {'documents': 1, 'document_amount': amount}
            assert report['granularity'] == 'document' and report['rows'][0]['number'] == '0003'
asyncio.run(verify())
"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="iiko win #") as tmp:
        work = Path(tmp)
        app = work / "iiko-intelligence"
        with ZipFile(ARCHIVE) as archive:
            assert archive.testzip() is None
            assert not any(name.startswith(("iiko-intelligence/data/", "iiko-intelligence/.codex/")) for name in archive.namelist())
            archive.extractall(work)
        env = {**os.environ, "WINEPREFIX": str(work / "prefix"), "WINEDEBUG": "-all",
               "WINEDLLOVERRIDES": "mscoree,mshtml,winemenubuilder.exe="}

        def run(*args: str, inputs: str = "", expected: int = 0) -> str:
            result = subprocess.run(["wine", *args], cwd=app, input=inputs.encode("utf-8"), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=150)
            output = result.stdout.decode("utf-8", errors="replace")
            if result.returncode != expected:
                raise RuntimeError(f"{args[0]} returned {result.returncode} (expected {expected})\n{output}")
            return output

        def cmd(*args, **kwargs):
            return run("cmd", "/d", "/c", *args, **kwargs)

        def cli(*args):
            return json.loads(run(r"runtime\python.exe", "-m", "iiko_intelligence.cli", *args))

        assert "Setup verified" in cmd("Setup-iiko.cmd", "/quiet")
        assert cli("status")["dataset_mode"] == "synthetic"
        shutil.copy(ROOT / "tests/fixtures/sales-legacy.xls", work / "old.xls")
        assert "Report ready" in cmd("Import-iiko-report.cmd", r"..\old.xls",
                                     inputs="01.09.2026\n01.09.2026\n125,50\nКафе\nRUB\n\n")
        assert cli("dishes", "--from", "2026-09-01", "--to", "2026-09-01")["rows"][0]["product_id"] == "0007"

        wb = Workbook()
        ws = wb.active
        ws.append(("Учетный день", "Код блюда", "Блюдо", "Количество блюд", "Сумма со скидкой"))
        ws.append(("01.09.2026", "01", "Чай", 8, 20))
        ws.append((None, "02", "Обед", 1, 80))
        ws.merge_cells("A2:A3")
        ws.append(("01.09.2026 Всего", None, None, None, 100))
        ws.append(("02.09.2026", "01", "Чай", 3, 10.50))
        ws.append(("Итого", None, None, None, 110.50))
        wb.save(work / "sales.xlsx")
        ws["E6"] = 110.51
        wb.save(work / "bad.xlsx")
        wb.close()
        sales_inputs = "01.09.2026\n02.09.2026\n110,50\nКафе\nRUB\n\n"
        assert "Report ready" in cmd("Import-iiko-report.cmd", r"..\sales.xlsx", inputs=sales_inputs)
        (work / "stock.csv").write_text("Артикул;Наименование;Ед. изм.;Количество;Себестоимость\n"
                                         "A;Чай;кг;2;600\nB;Сахар;кг;-0,25;-10\n;Итого;;;590\n", encoding="cp1251")
        assert "Warehouse snapshot ready" in cmd("Import-iiko-stock.cmd", r"..\stock.csv",
                                                inputs="Кафе\nRUB\nКухня\n31.08.2026\n590\n\n")
        for choice, typ, party, amount in [(1, "incoming_invoice", "supplier", "90.20"),
                                           (2, "writeoff_act", "warehouse", "2.20")]:
            (work / "docs.csv").write_text(f"document_date,document_number,document_type,status,{party},document_amount\n"
                                           f"2026-08-31,0003,{typ},posted,Тест,{amount}\n", encoding="utf-8")
            assert "Document list ready" in cmd("Import-iiko-documents.cmd", r"..\docs.csv",
                         inputs=f"{choice}\n1\n01.08.2026\n31.08.2026\n{amount}\nКафе\nRUB\n\n")
        assert "grand total" in cmd("Import-iiko-report.cmd", r"..\bad.xlsx", inputs=sales_inputs, expected=2)
        assert "Setup verified" in cmd("Setup-iiko.cmd", "/quiet")
        assert "reports" in cmd("Report-for-ChatGPT.cmd", "/quiet")
        report, = (app / "reports").glob("*.txt")
        body = report.read_text(encoding="utf-8-sig")
        assert all(s in body for s in ("Кафе", "110.50", "Обед", "Кухня", "590.00", "2026-08-31", "90.20", "Акты списания"))
        (work / "verify.py").write_text(VERIFY, encoding="utf-8")
        run(r"runtime\python.exe", r"..\verify.py")
        subprocess.run(["wineserver", "-w"], cwd=app, env=env, timeout=45, check=True)
    print("Windows ZIP passed setup, three file formats, stock, documents, failed import, repeat setup, text report and MCP checks.")


if __name__ == "__main__":
    main()
