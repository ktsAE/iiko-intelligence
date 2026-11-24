"""Exercise the shipped Windows programs under Wine with disposable synthetic reports."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

from openpyxl import Workbook
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from iiko_intelligence import release_trust as trust

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
        assert len(listing.tools) == 28
        assert {t.name for t in listing.tools if not t.annotations.read_only_hint} == {"create_iiko_record", "update_iiko_record"}
        async def call(name, **args):
            reply = await client.call_tool(name, args)
            assert not reply.is_error, reply
            return reply.structured_content
        recipe = await call('get_recipe_card', product_id='00000000-0000-4000-8000-000000000001', as_of='2026-09-01')
        assert recipe['status'] == 'not_configured'
        for tool, args in [('check_live_access', {}), ('get_sales_summary', {'date_from':'yesterday', 'date_to':'yesterday'})]:
            assert (await call(tool, **args))['status'] == 'not_configured'
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


def newer_release_fixture(work: Path, original: str) -> str:
    # Generate a newer package locally so the update can be exercised with networking disabled.
    from zipfile import ZIP_DEFLATED
    major, minor, patch = original.split(".")
    new = f"{major}.{minor}.{int(patch) + 1}"
    source = work / "next.zip"
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    test_pin = base64.b64encode(key.public_key().public_numbers().n.to_bytes(384, "big"))
    anchor_name = "iiko-intelligence/src/iiko_intelligence/release_trust.py"
    anchor = work / anchor_name
    anchor.write_bytes(anchor.read_bytes().replace(trust.MODULUS.encode(), test_pin))
    for cache in (anchor.parent / "__pycache__").glob("release_trust.*.pyc"):
        cache.unlink()
    with ZipFile(ARCHIVE) as old, ZipFile(source, "w", ZIP_DEFLATED) as upcoming:
        for info in old.infolist():
            data = old.read(info)
            if info.filename in ("iiko-intelligence/pyproject.toml", "iiko-intelligence/src/iiko_intelligence/__init__.py"):
                data = data.replace(f'"{original}"'.encode(), f'"{new}"'.encode())
            if info.filename == anchor_name:
                data = data.replace(trust.MODULUS.encode(), test_pin)
            upcoming.writestr(info, data)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset = "iiko-intelligence-windows-x64.zip"
    url = f"https://github.com/ktsAE/iiko-intelligence/releases/download/v{new}/{asset}"
    message = trust.Release("v" + new, source.stat().st_size, digest, trust.archive_tree(source)).message()
    raw = message + base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())) + b"\n"
    (work / "next.sig").write_bytes(raw)
    (work / "latest.json").write_text(json.dumps({"draft": False, "prerelease": False, "immutable": True, "tag_name": "v" + new,
        "assets": [{"name": asset, "size": source.stat().st_size, "digest": "sha256:" + digest, "browser_download_url": url},
                   {"name": trust.SIGNATURE, "size": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "browser_download_url": url + ".sig"}]}), encoding="utf-8")
    return new


UPDATE = """
from pathlib import Path
from iiko_intelligence import __version__, updates
root = Path.cwd()
def local_response(request, **kwargs):
    if getattr(request, 'full_url', None) == updates.LATEST:
        return (root.parent / 'latest.json').open('rb')
    if request.endswith('.sig'):
        return (root.parent / 'next.sig').open('rb')
    return (root.parent / 'next.zip').open('rb')
out = updates.refresh(root, 'v' + __version__, force=True,
                      fetch=lambda: updates.latest_release(local_response),
                      retrieve=lambda release, archive: updates.download(release, archive, local_response))
assert out['status'] == 'ready', out
assert updates.update_status()['status'] == 'restart_required'
"""

VERIFY_UPDATED = """
import asyncio, sys, tomllib
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters
async def verify():
    settings = tomllib.loads(Path('.codex/config.toml').read_text(encoding='utf8'))['mcp_servers']['iiko_intelligence']
    settings['env']['IIKO_NO_UPDATE_CHECK'] = '1'
    async with Client(StdioServerParameters(**settings), read_timeout_seconds=25) as client:
        reply = await client.call_tool('get_data_status', {})
        assert not reply.is_error
        status = reply.structured_content
        assert status['software_update']['running'] == 'v' + sys.argv[1], status['software_update']
        if len(sys.argv) > 2:
            assert status['software_update']['status'] == 'installed_files_rejected', status['software_update']
        assert status['live_cloud']['status'] == 'configured'
        assert status['reconciliation']['iiko_report_total'] == '110.50'
asyncio.run(verify())
"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="iiko win #") as tmp:
        work = Path(tmp)
        app = work / "iiko-intelligence"
        from iiko_intelligence import __version__
        signed = trust.verify_receipt(ARCHIVE.with_name(trust.SIGNATURE).read_bytes(), "v" + __version__)
        with ARCHIVE.open("rb") as stream:
            assert trust.file_hash(stream) == signed.sha256 and ARCHIVE.stat().st_size == signed.size
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
        # Use only fixture credentials for the current Windows-user encryption round trip.
        profile_check = (
            "from iiko_intelligence.cloud_profile import PROFILE, validated, save_profile, read_profile; "
            "p=validated('00000000-0000-4000-8000-000000000001', "
            "{'appId':'example','clientSecret':'private-fixture','apiKey':'test-key'}); "
            "save_profile(p); assert b'private-fixture' not in PROFILE.read_bytes(); assert read_profile()==p"
        )
        run(r"runtime\python.exe", "-c", profile_check)
        original = run(r"runtime\python.exe", "-c", "from iiko_intelligence import __version__; print(__version__)").strip()
        new = newer_release_fixture(work, original)
        protected = [app / name for name in ("data/manager.sqlite", "data/cloud-profile.dpapi", ".codex/config.toml")] + [report]
        before = [path.read_bytes() for path in protected]
        (work / "stage_update.py").write_text(UPDATE, encoding="utf-8")
        run(r"runtime\python.exe", r"..\stage_update.py")
        (work / "verify_update.py").write_text(VERIFY_UPDATED, encoding="utf-8")
        run(r"runtime\python.exe", r"..\verify_update.py", new)
        assert cli("check-mcp")["dataset_mode"] != "synthetic"
        assert "Setup verified" in cmd("Setup-iiko.cmd", "/quiet")
        assert before == [path.read_bytes() for path in protected]
        changed_file = app / ".iiko-updates" / ("v" + new) / "README.md"
        changed_file.write_text("modified fixture", encoding="utf-8")
        run(r"runtime\python.exe", r"..\verify_update.py", original, "rejected")
        assert before == [path.read_bytes() for path in protected]
        run(r"runtime\python.exe", "-m", "iiko_intelligence.cloud_profile", "--disconnect")
        assert not (app / "data/cloud-profile.dpapi").exists()
        fixture_tests = work / "test_cloud_reports.py"
        shutil.copy(ROOT / "tests/test_cloud_reports.py", fixture_tests)
        run(r"runtime\python.exe", "-m", "unittest", "discover", "-s", "..", "-p", fixture_tests.name)
        subprocess.run(["wineserver", "-w"], cwd=app, env=env, timeout=45, check=True)
    print("Windows ZIP passed setup, three file formats, stock, documents, failed import, repeat setup, text report, staged self-update, preserved profile and MCP checks.")


if __name__ == "__main__":
    main()
