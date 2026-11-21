from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .sales_snapshot import open_reports
from .service import _money, _validated_limit


def compare_periods(database: Path, baseline_from: str, baseline_to: str, current_from: str,
                    current_to: str, *, limit: int = 10) -> dict[str, Any]:
    limit = _validated_limit(limit)
    with open_reports(database) as service:
        baseline = service.sales_summary(baseline_from, baseline_to)
        current = service.sales_summary(current_from, current_to)
        periods = [summary["period"] for summary in (baseline, current)]
        first, last, start, end = [date.fromisoformat(p[key]) for p in periods for key in ("from", "to")]
        params = {"first": first.isoformat(), "last": last.isoformat(),
                  "start": start.isoformat(), "end": end.isoformat(), "limit": limit}
        with service._read() as connection:
            rows = connection.execute(f"""
                WITH items AS ({service.dish_sales_sql}), amounts AS (
                    SELECT product_id, name,
                           SUM(CASE WHEN day BETWEEN :first AND :last THEN amount ELSE 0 END) AS baseline,
                           SUM(CASE WHEN day BETWEEN :start AND :end THEN amount ELSE 0 END) AS current
                    FROM items WHERE day BETWEEN :first AND :last OR day BETWEEN :start AND :end
                    GROUP BY product_id, name
                )
                SELECT *, current - baseline AS change FROM amounts WHERE current != baseline
                ORDER BY ABS(current - baseline) DESC, product_id LIMIT :limit
            """, params).fetchall()
    metric = "net_sales" if current["dataset_mode"] == "synthetic" else "sales_after_discounts"
    before, after = [Decimal(summary["totals"][metric]) for summary in (baseline, current)]
    change = after - before
    pct = None
    if before > 0 and baseline["by_day"] and current["by_day"]:
        pct = str((100 * change / before).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    result = {k: v for k, v in current.items() if k in
              ("status", "dataset_mode", "dataset_id", "restaurant", "currency", "source", "warning", "coverage")}

    def period_info(summary: dict[str, Any], duration: int) -> dict[str, Any]:
        return {**summary["period"], "calendar_days": duration, "days_with_rows": len(summary["by_day"]),
                "sales": summary["totals"][metric]}

    result.update(metric=metric, baseline=period_info(baseline, (last - first).days + 1),
                  current=period_info(current, (end - start).days + 1),
                  overlap_days=max(0, (min(last, end) - max(first, start)).days + 1))
    if not baseline["by_day"] or not current["by_day"]:
        return {**result, "status": "insufficient_data", "change": None, "change_percent": None,
                "reason": "One or both periods contain no source rows; a sales change cannot be verified."}
    return {**result,
            "change": str(change), "change_percent": pct,
            "dish_changes": [{"product_id": row["product_id"], "name": row["name"],
                              "baseline": _money(row["baseline"]), "current": _money(row["current"]),
                              "change": _money(row["change"])} for row in rows],
            "other_dishes_change": str(change - Decimal(sum(row["change"] for row in rows)) / 100)}


def export_brief(database: Path, directory: Path, date_from: str | None, date_to: str | None,
                 limit: int = 10) -> Path:
    with open_reports(database) as service:
        status = service.data_status()
        if not status["reconciliation"]["passed"]:
            raise ValueError("the dataset failed reconciliation")
        first, last = status["coverage"]["from"], status["coverage"]["to"]
        summary = service.sales_summary(date_from or first, date_to or last)
        start, end = summary["period"]["from"], summary["period"]["to"]
        dishes = service.top_dishes(start, end, limit=limit)
        stock = status.get("stock_snapshot")
        if stock and not stock["passed"]:
            raise ValueError("warehouse values failed reconciliation")
    demo = status["dataset_mode"] == "synthetic"
    metric = "net_sales" if demo else "sales_after_discounts"
    label = "Учебная выручка после скидок и возвратов" if demo else "Сумма со скидкой по отчету iiko"
    lines = [
        "Отчет по продажам", "УЧЕБНЫЕ ДАННЫЕ" if demo else "Снимок данных iiko",
        f"Ресторан: {status['restaurant']['name']}", f"Учетный период: {start} — {end}",
        f"Валюта: {status['currency']}", f"Источник: {status['source']['provider']}",
        f"Дата снимка: {status['source'].get('imported_at') or status['source'].get('generated_at')}",
        f"{label}: {summary['totals'][metric]}",
        "", "Продажи по учетным дням", "Показаны дни со строками в источнике.",
        *(f"{row['date']}  {row[metric]}" for row in summary["by_day"]), "",
        f"Блюда по выручке (показано {len(dishes['rows'])} из {dishes['detail']['total_rows']})",
        "Блюдо | Количество | Выручка",
        *(f"{row['name']} | {row['quantity']} | {row[metric]}" for row in dishes["rows"]),
    ]
    if "orders" in summary["totals"]:
        lines.append(f"Заказы за период: {summary['totals']['orders']}")
    if stock:
        lines.extend(["", f"Склад: {stock['warehouse']}", f"Дата остатков: {stock['as_of']}",
                      f"Общая стоимость остатков: {stock['value']}", f"Число позиций: {stock['items']}"])
    for kind, source in status.get("document_sources", {}).items():
        if not source["passed"]:
            raise ValueError("document totals failed reconciliation")
        title = "Приходные накладные" if kind == "purchases" else "Акты списания"
        lines.extend(["", title, f"Период документов: {source['coverage']['from']} — {source['coverage']['to']}",
                      f"Число документов: {source['documents']}", f"Сумма документов: {source['total']}"])
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8-sig", suffix=".txt", prefix=f"iiko-{start}_{end}-",
                            delete=False, dir=directory) as file:
        file.write("\n".join(lines) + "\n")
        return Path(file.name)
