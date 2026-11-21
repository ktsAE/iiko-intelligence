from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from getpass import getpass
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Callable

from .cloud_sales import fetch_cloud_sales
from .config import MANAGER_DATABASE, PROJECT_ROOT, database_path
from .connect import setup_local, verify_connection
from .document_import import import_documents
from .manager_reports import compare_periods, export_brief
from .office_export import AmbiguousSalesSheet, read_sales_export
from .sales_snapshot import (
    SalesPeriod,
    number_units,
    open_reports,
    report_date,
    save_snapshot,
    strip_ruble_suffix,
)
from .seed import DEFAULT_AS_OF, DEFAULT_DAYS, seed_demo
from .stock_import import import_stock


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iiko-intelligence",
        description="Read-only iiko reporting for one restaurant.",
    )
    parser.add_argument("--db", type=Path, help="Override the local SQLite dataset path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init-demo", help="Create a deterministic synthetic dataset")
    initialize.add_argument("--as-of", default=DEFAULT_AS_OF.isoformat(), help="Last date, YYYY-MM-DD")
    initialize.add_argument("--days", type=int, default=DEFAULT_DAYS)

    subparsers.add_parser("capabilities", help="Show available data and definitions")
    subparsers.add_parser("status", help="Show coverage and reconciliation status")
    subparsers.add_parser("setup-local", help="Install this folder's local Codex MCP configuration and verify it")
    subparsers.add_parser("check-mcp", help="Run a tool in a local MCP subprocess to verify the connection")

    importer = subparsers.add_parser("import-sales", help="Import and reconcile an iikoOffice sales export")
    importer.add_argument("--interactive", action="store_true", help="Prompt for the report and dates")
    importer.add_argument("--file", type=Path, help="Path to the approved XLSX, XLS or CSV export")
    importer.add_argument("--sheet", help="Name of the sales sheet when multiple sheets match")
    _period_arguments(importer)

    docs = subparsers.add_parser("import-documents", help="Reconcile posted invoices or write-offs from a prepared list")
    docs.add_argument("--interactive", action="store_true")
    docs.add_argument("--kind", choices=("purchases", "writeoffs"))
    docs.add_argument("--file", type=Path)
    docs.add_argument("--sheet")
    docs.add_argument("--iiko-count", type=int)
    _period_arguments(docs)

    inventory = subparsers.add_parser("import-stock", help="Reconcile one warehouse balance export with iikoOffice")
    inventory.add_argument("--interactive", action="store_true")
    inventory.add_argument("--file", type=Path)
    inventory.add_argument("--sheet")
    for flag in ("restaurant", "currency", "warehouse", "as-of", "iiko-total"):
        inventory.add_argument("--" + flag)

    cloud = subparsers.add_parser("sync-cloud-sales", help="Fetch an approved iiko Cloud outlet sales snapshot")
    cloud.add_argument("--organization", help="iiko organization UUID")
    cloud.add_argument("--outlet", help="Exact name of the outlet in iiko OLAP reports")
    cloud.add_argument("--interactive", action="store_true")
    _period_arguments(cloud)

    brief = subparsers.add_parser("export-brief", help="Write a local sales summary to review or attach to ChatGPT")
    brief.add_argument("--directory", type=Path, default=PROJECT_ROOT / "reports")
    brief.add_argument("--from", dest="date_from")
    brief.add_argument("--to", dest="date_to")
    brief.add_argument("--limit", type=int, default=10)

    compare = subparsers.add_parser("compare", help="Compare two periods from the same snapshot")
    for flag in ("baseline-from", "baseline-to", "current-from", "current-to"):
        compare.add_argument("--" + flag, required=True)
    compare.add_argument("--limit", type=int, default=10)

    for name in ("sales", "dishes", "purchases", "writeoffs"):
        report = subparsers.add_parser(name)
        report.add_argument("--from", dest="date_from", required=True)
        report.add_argument("--to", dest="date_to", required=True)
        if name != "sales":
            report.add_argument("--limit", type=int, default=10 if name == "dishes" else 20)
        if name == "dishes":
            report.add_argument("--rank-by", choices=("sales", "quantity"), default="sales")

    stock = subparsers.add_parser("stock")
    stock.add_argument("--limit", type=int, default=50)
    stock.add_argument("--negative-only", action="store_true")
    return parser


def _period_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="date_from", help="First accounting day, inclusive")
    parser.add_argument("--to", dest="date_to", help="Last accounting day, inclusive")
    parser.add_argument("--iiko-total", help="Matching control total displayed in iikoOffice")
    parser.add_argument("--restaurant", help="Short label for the restaurant")
    parser.add_argument("--currency", help="Currency code, for example RUB")


def _period(args: argparse.Namespace) -> SalesPeriod:
    if not all((args.date_from, args.date_to, args.iiko_total, args.restaurant, args.currency)):
        raise ValueError("supply --from, --to, --iiko-total, --restaurant and --currency")
    currency = args.currency.strip().upper()
    total = number_units(strip_ruble_suffix(args.iiko_total, currency), 2, "iiko total")
    return SalesPeriod(report_date(args.date_from), report_date(args.date_to),
                       total, args.restaurant.strip(), currency)


def _prompt_period(args: argparse.Namespace, total_label: str = "Итог «Сумма со скидкой» из iikoOffice") -> None:
    if args.interactive:
        args.date_from = args.date_from or input("Первый учетный день (ДД.ММ.ГГГГ): ").strip()
        args.date_to = args.date_to or input("Последний учетный день (ДД.ММ.ГГГГ): ").strip()
        args.iiko_total = args.iiko_total or input(total_label + ": ").strip()
        args.restaurant = args.restaurant or input("Название ресторана: ").strip()
        args.currency = args.currency or input("Валюта (Enter для RUB): ").strip() or "RUB"


def _source_file(args: argparse.Namespace) -> Path:
    if not args.file and args.interactive:
        args.file = Path(input("Путь к выгрузке iikoOffice (XLSX, XLS или CSV): ").strip().strip('"'))
    if not args.file:
        raise ValueError("supply --file or use --interactive")
    return args.file.expanduser()


def _choose_sheet(action: Callable, args: argparse.Namespace):
    try:
        return action(args.sheet)
    except AmbiguousSalesSheet as exc:
        if not args.interactive:
            raise ValueError(f"{exc}; select one with --sheet") from None
        print(exc)
        sheet = input("Название листа: ").strip()
        if not sheet:
            raise ValueError("enter one of the listed sheet names") from None
        return action(sheet)


def _import_sales(args: argparse.Namespace, path: Path) -> dict[str, Any]:
    source = _source_file(args)
    _prompt_period(args)
    period = _period(args)
    rows = _choose_sheet(lambda sheet: read_sales_export(source, currency=period.currency, sheet=sheet), args)
    return save_snapshot(path, period, rows, provider="iikoOffice_export")


def _import_stock(args: argparse.Namespace, path: Path) -> dict[str, Any]:
    source = _source_file(args)
    if args.interactive:
        args.restaurant = args.restaurant or input("Название ресторана, как в отчете по продажам: ").strip()
        args.currency = args.currency or input("Валюта (Enter для RUB): ").strip() or "RUB"
        args.warehouse = args.warehouse or input("Склад в отчете: ").strip()
        args.as_of = args.as_of or input("Дата остатков (ДД.ММ.ГГГГ): ").strip()
        args.iiko_total = args.iiko_total or input("Общая стоимость остатков по iikoOffice: ").strip()
    if not all((args.restaurant, args.currency, args.warehouse, args.as_of, args.iiko_total)):
        raise ValueError("supply --restaurant, --currency, --warehouse, --as-of and --iiko-total")
    currency = args.currency.strip().upper()
    total = number_units(strip_ruble_suffix(args.iiko_total, currency), 2, "stock total")
    return _choose_sheet(lambda sheet: import_stock(path, source, restaurant=args.restaurant.strip(), currency=currency,
                         warehouse=args.warehouse, as_of=report_date(args.as_of), expected_minor=total, sheet=sheet), args)


def _import_documents(args: argparse.Namespace, path: Path) -> dict[str, Any]:
    source = _source_file(args)
    if not args.kind and args.interactive:
        choice = input("1 — приходные накладные, 2 — акты списания: ").strip()
        args.kind = {"1": "purchases", "2": "writeoffs"}.get(choice)
    if args.interactive and args.iiko_count is None:
        args.iiko_count = int(input("Число документов в отфильтрованном списке iikoOffice: ").strip())
    if not args.kind or args.iiko_count is None:
        raise ValueError("supply --kind and --iiko-count or use --interactive")
    _prompt_period(args, "Общая сумма документов в списке iikoOffice")
    period = _period(args)
    return _choose_sheet(lambda sheet: import_documents(path, source, args.kind, period, args.iiko_count, sheet), args)


def _sync_cloud_sales(args: argparse.Namespace, path: Path) -> dict[str, Any]:
    if args.interactive:
        args.organization = args.organization or input("UUID организации iiko: ").strip()
        args.outlet = args.outlet or input("Название заведения в OLAP: ").strip()
    if not args.organization or not args.outlet:
        raise ValueError("supply --organization and --outlet or use --interactive")
    _prompt_period(args)
    period = _period(args)
    credentials = {}
    for key, name in (("appId", "IIKO_CLOUD_APP_ID"), ("clientSecret", "IIKO_CLOUD_CLIENT_SECRET"),
                      ("apiKey", "IIKO_CLOUD_API_KEY")):
        value = os.environ.get(name)
        if not value and sys.stdin.isatty():
            value = getpass(f"{name}: ").strip()
        if not value:
            raise ValueError(f"supply {name} to this collector process or run it in an interactive terminal")
        credentials[key] = value
    rows, orders = fetch_cloud_sales(period, args.organization, args.outlet, credentials)
    return save_snapshot(path, period, rows, provider="iikoCloud_olap", daily_orders=orders,
                         cloud_scope=(args.organization.strip().lower(), args.outlet.strip()))


def main() -> None:
    if isinstance(sys.stdin, TextIOWrapper) and not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if isinstance(sys.stdout, TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = _parser()
    args = parser.parse_args()
    path = (args.db or database_path()).expanduser().resolve()

    try:
        if args.command == "init-demo":
            _print(seed_demo(path, as_of=date.fromisoformat(args.as_of), days=args.days))
            return

        if args.command == "setup-local":
            if args.db:
                raise ValueError("setup-local configures the manager database for this folder")
            _print(setup_local())
            return

        if args.command == "check-mcp":
            _print(verify_connection(path))
            return

        collectors = {"import-sales": _import_sales, "sync-cloud-sales": _sync_cloud_sales,
                      "import-stock": _import_stock, "import-documents": _import_documents}
        if args.command in collectors:
            target = (args.db or MANAGER_DATABASE).expanduser().resolve()
            _print(collectors[args.command](args, target))
            return

        if args.command == "export-brief":
            print(export_brief(path, args.directory, args.date_from, args.date_to, args.limit))
            return
        if args.command == "compare":
            _print(compare_periods(path, args.baseline_from, args.baseline_to, args.current_from,
                                   args.current_to, limit=args.limit))
            return
        with open_reports(path) as service:
            actions: dict[str, Callable[[], dict[str, Any]]] = {
                "capabilities": service.capabilities,
                "status": service.data_status,
                "sales": lambda: service.sales_summary(args.date_from, args.date_to),
                "dishes": lambda: service.top_dishes(args.date_from, args.date_to, limit=args.limit, rank_by=args.rank_by),
                "purchases": lambda: service.purchases(args.date_from, args.date_to, limit=args.limit),
                "stock": lambda: service.stock_balances(limit=args.limit, negative_only=args.negative_only),
                "writeoffs": lambda: service.writeoffs(args.date_from, args.date_to, limit=args.limit),
            }
            _print(actions[args.command]())
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    except sqlite3.DatabaseError:
        parser.error("could not read or write the local SQLite dataset")
    except (EOFError, KeyboardInterrupt):
        parser.exit(130, "Import cancelled.\n")


if __name__ == "__main__":
    main()
