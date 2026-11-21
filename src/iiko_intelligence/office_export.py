from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import xlrd
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .sales_snapshot import (
    MAX_SALES_ROWS,
    SalesRow,
    number_units,
    report_date,
    strip_ruble_suffix,
)
from .service import MAX_RANGE_DAYS

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_WORKBOOK_BYTES = 100 * 1024 * 1024
MAX_COLUMNS = 50
MAX_TITLE_ROWS = 25

FIELDS = (
    ("business_date", "учетный день", "дата учета", "accounting day"),
    ("product_code", "код блюда", "dish code"),
    ("dish", "блюдо", "наименование блюда", "dish name"),
    ("quantity", "количество блюд", "количество блюд всего", "кол-во блюд", "dish quantity"),
    ("sales_after_discounts", "сумма со скидкой", "сумма со скидкой, р.", "сумма со скидкой, руб.",
     "сумма со скидкой, р. всего", "sales after discounts"),
)
Headers = tuple[tuple[str, ...], ...]
TOTAL_LABELS = {"итого", "итог", "общий итог", "всего", "total", "grand total"}
DAY_SUBTOTAL_LABELS = {"всего", "итого", "subtotal", "total"}


class AmbiguousSalesSheet(ValueError):
    pass


def _select_sheet(matches: list[str]) -> str:
    if not matches:
        raise ValueError("no worksheet contains the required report headers")
    if len(matches) > 1:
        raise AmbiguousSalesSheet("multiple sheets contain report headers: " + ", ".join(matches))
    return matches[0]


def _heading(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(value.strip(": ").split())


@contextmanager
def _csv_rows(source: Path, fields: Headers) -> Iterator[Iterator[tuple[object, ...]]]:
    try:
        source.read_bytes().decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp1251"
    with source.open(encoding=encoding, newline="") as file:
        sample = file.read(8192)
        file.seek(0)
        # Excel typically uses a semicolon when the decimal mark is a comma.
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            for separator in (";", ",", "\t"):
                if any(_columns(row, fields) for row in csv.reader(sample.splitlines(), delimiter=separator)):
                    break
            else:
                raise ValueError("could not determine the CSV delimiter") from None
            yield (tuple(row) for row in csv.reader(file, delimiter=separator, strict=True))
        else:
            yield (tuple(row) for row in csv.reader(file, dialect=dialect, strict=True))


@contextmanager
def _xlsx_rows(source: Path, sheet: str | None, fields: Headers) -> Iterator[Iterator[tuple[object, ...]]]:
    with ZipFile(source) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > MAX_WORKBOOK_BYTES:
            raise ValueError("workbook exceeds the unpacked size limit")
    workbook = load_workbook(source, read_only=True, data_only=False, keep_links=False)
    try:
        worksheets = [worksheet for worksheet in workbook.worksheets if sheet is None or worksheet.title == sheet]
        for worksheet in worksheets:
            worksheet.reset_dimensions()
        if sheet is not None and not worksheets:
            raise ValueError("the selected sheet does not exist")
        if sheet is None and len(worksheets) > 1:
            worksheets = [worksheet for worksheet in worksheets if any(
                _columns(row, fields) for row in worksheet.iter_rows(max_row=MAX_TITLE_ROWS, max_col=MAX_COLUMNS, values_only=True)
            )]
        worksheet = workbook[_select_sheet([w.title for w in worksheets])]
        rows = worksheet.iter_rows(max_col=MAX_COLUMNS + 1)
        yield _excel_values(rows, fields)
    finally:
        workbook.close()


def _excel_values(rows: Iterator, fields: Headers) -> Iterator[tuple[object, ...]]:
    code_column: int | None = None
    for row in rows:
        values = [cell.value for cell in row]
        if code_column is None:
            columns = _columns(values, fields)
            if columns:
                code_column = columns[1]
        else:
            code = row[code_column]
            if type(code.value) is int and re.fullmatch(r"0{2,80}", code.number_format or ""):
                values[code_column] = str(code.value).zfill(len(code.number_format))
        yield tuple(values)


@contextmanager
def _xls_rows(source: Path, sheet: str | None, fields: Headers) -> Iterator[Iterator[tuple[object, ...]]]:
    book = xlrd.open_workbook(source, on_demand=True, formatting_info=True, ragged_rows=True)
    try:
        names = book.sheet_names()
        if sheet is None:
            matches = []
            for name in names:
                table = book.sheet_by_name(name)
                if any(_columns(table.row_values(i)[:MAX_COLUMNS], fields) for i in range(min(table.nrows, MAX_TITLE_ROWS))):
                    matches.append(name)
                book.unload_sheet(name)
            sheet = _select_sheet(matches)
        elif sheet not in names:
            raise ValueError("the selected sheet does not exist")
        table = book.sheet_by_name(sheet)

        def values() -> Iterator[tuple[object, ...]]:
            columns: tuple[int, ...] = ()
            for index in range(table.nrows):
                row = table.row(index)
                result = [cell.value for cell in row]
                if not columns:
                    columns = _columns(result, fields)
                else:
                    for col, cell in enumerate(row):
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            result[col] = xlrd.xldate_as_datetime(cell.value, book.datemode)
                        elif cell.ctype == xlrd.XL_CELL_NUMBER and col == columns[1] and cell.value.is_integer():
                            fmt = book.format_map[book.xf_list[cell.xf_index].format_key].format_str
                            code = str(int(cell.value))
                            result[col] = code.zfill(len(fmt)) if re.fullmatch(r"0{2,80}", fmt) else code
                        elif cell.ctype in {xlrd.XL_CELL_ERROR, xlrd.XL_CELL_BOOLEAN} and col in columns:
                            raise ValueError(f"report row {index + 1}: expected a value in a required column")
                yield tuple(result)

        yield values()
    finally:
        book.release_resources()


def _columns(row: Sequence[object], fields: Headers = FIELDS) -> tuple[int, ...]:
    headings = [_heading(cell) for cell in row]
    indices = [[index for index, heading in enumerate(headings) if heading in names] for names in fields]
    if any(not matches for matches in indices):
        return ()
    if any(len(matches) != 1 for matches in indices):
        raise ValueError("a required column appears more than once in the report")
    return tuple(matches[0] for matches in indices)


def _parse_rows(rows: Iterator[tuple[object, ...]], currency: str) -> list[SalesRow]:
    columns: tuple[int, ...] = ()
    result: list[SalesRow] = []
    group_day: date | None = None
    inferred_days: set[date] = set()
    subtotals: dict[date, int] = {}
    detail_sales: dict[date, int] = defaultdict(int)
    grand_total: int | None = None
    for number, cells in enumerate(rows, start=1):
        if number > MAX_SALES_ROWS + MAX_TITLE_ROWS + 2 * MAX_RANGE_DAYS + 5:
            raise ValueError("report exceeds the row limit")
        if any(cell is not None and cell != "" for cell in cells[MAX_COLUMNS:]):
            raise ValueError("report exceeds the column limit")
        if not columns:
            if number > MAX_TITLE_ROWS:
                break
            columns = _columns(cells)
            continue
        day, code, dish, quantity, sales = (cells[index] if index < len(cells) else None for index in columns)
        if all(value in (None, "") for value in (day, code, dish, quantity, sales)):
            continue
        try:
            if code in (None, "") and dish in (None, ""):
                label = _heading(day)
                if label in TOTAL_LABELS:
                    if sales not in (None, ""):
                        amount = number_units(strip_ruble_suffix(sales, currency), 2, "grand total")
                        if grand_total is not None and amount != grand_total:
                            raise ValueError("conflicting grand total rows")
                        grand_total = amount
                    group_day = None
                    continue
                day_label, _, suffix = label.rpartition(" ")
                if suffix in DAY_SUBTOTAL_LABELS:
                    subtotal_date = report_date(day_label)
                    if subtotal_date in subtotals:
                        raise ValueError("expected at most one subtotal per accounting day")
                    subtotals[subtotal_date] = number_units(strip_ruble_suffix(sales, currency), 2, "daily subtotal")
                    group_day = None
                    continue
                if day not in (None, "") and quantity in (None, "") and sales in (None, ""):
                    group_day = report_date(day)
                    continue
            if any(isinstance(value, str) and value.startswith("=") for value in (day, code, dish, quantity, sales)):
                raise ValueError("formulas are unsupported in data rows; export values")
            if day in (None, "") and group_day is not None:
                day = group_day
                inferred_days.add(group_day)
            row = SalesRow.from_values(day, code, dish, quantity, strip_ruble_suffix(sales, currency))
            group_day = row.day
            result.append(row)
            detail_sales[row.day] += row.sales_minor
        except ValueError as exc:
            raise ValueError(f"report row {number}: {exc}") from None
    if not columns:
        raise ValueError("required headers: " + ", ".join(names[0] for names in FIELDS))
    for day in sorted(inferred_days):
        if day not in subtotals:
            raise ValueError(f"{day}: grouped dates need a daily subtotal; otherwise repeat the date on each row")
    for day, subtotal in subtotals.items():
        if subtotal != detail_sales.get(day):
            raise ValueError(f"{day}: dish sales do not match the accounting-day subtotal")
    if grand_total is not None and sum(detail_sales.values()) != grand_total:
        raise ValueError("dish sales do not match the grand total in the export")
    return result


@contextmanager
def export_rows(source: Path, sheet: str | None, fields: Headers) -> Iterator[Iterator[tuple[object, ...]]]:
    if not source.is_file():
        raise ValueError("select an existing XLSX, XLS or CSV export")
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("report exceeds the 20 MiB input limit")
    extension = source.suffix.casefold()
    try:
        if extension == ".xlsx":
            reader = _xlsx_rows(source, sheet, fields)
        elif extension == ".xls":
            reader = _xls_rows(source, sheet, fields)
        elif extension == ".csv" and sheet is None:
            reader = _csv_rows(source, fields)
        else:
            raise ValueError("select an .xlsx, .xls or .csv file; use --sheet only with Excel")
        with reader as rows:
            yield rows
    except (BadZipFile, InvalidFileException, xlrd.XLRDError, KeyError, UnicodeError, ParseError, csv.Error) as exc:
        raise ValueError("could not read the export; save it again as a flat Excel or CSV report") from exc


def read_sales_export(source: Path, *, currency: str = "RUB", sheet: str | None = None) -> list[SalesRow]:
    with export_rows(source, sheet, FIELDS) as rows:
        return _parse_rows(rows, currency)
