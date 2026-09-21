#!/usr/bin/env python3
"""Разбор сырья scripts/wb_remeasure_probe.py. К WB не обращается вовсе.

Из БД только читает (marketplace_orders по WB за даты пробы), постранично и с
сортировкой по ключу. Деньги — Decimal, JSON разбирается с parse_float=Decimal.

Что с чем сравнивается — вслух, потому что величины одноимённые:

    flag=1 за дату D    все заказы с датой заказа D, как WB отдаёт их СЕЙЧАС
    flag=0 (ночной вид) заказы с lastChangeDate >= today-30; срез по date == D
    база                агрегат (дата, nmId), записанный ПОСЛЕДНЕЙ НОЧЬЮ, когда
                        D попадала в ответ; «заказ» = строка ответа, отменённые
                        (isCancel) входят — это «созданные», не «подтверждённые»

Поэтому flag=1 против flag=0 сверяется по множествам srid и по значениям полей,
а flag=1 против базы — по SKU: штуки и обе суммы. Расхождение «сегодняшний ответ
против ночной записи» на свежих датах законно (заказ изменился после ночи) и
называется поимённо.

Запуск:
    python3 scripts/wb_remeasure_analyze.py --raw logs/wb_probe_20260921
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter
from decimal import Decimal

sys.path.insert(0, ".")

from loaders.wb_orders_loader import supabase  # noqa: E402

PAGE_SIZE = 1000

def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal)


def money(item, first, second):
    for field in (first, second, "totalPrice"):
        value = item.get(field)
        if value:
            return Decimal(str(value))
    return Decimal(0)


def aggregate(items, day):
    """То же правило, что loaders/wb_orders_loader.aggregate_orders, но в Decimal."""
    cells = {}
    for item in items:
        if str(item.get("date"))[:10] != day:
            continue
        sku = str(item["nmId"])
        cell = cells.setdefault(sku, {"qty": Decimal(0), "buyer": Decimal(0), "seller": Decimal(0),
                                      "cancelled": Decimal(0)})
        cell["qty"] += 1
        cell["buyer"] += money(item, "finishedPrice", "priceWithDisc")
        cell["seller"] += money(item, "priceWithDisc", "finishedPrice")
        if item.get("isCancel"):
            cell["cancelled"] += 1
    return cells


def stored(day):
    cells = {}
    start = 0
    while True:
        batch = (
            supabase.table("marketplace_orders")
            .select("marketplace_sku,order_schema,orders_qty,orders_amount_buyer,orders_amount_seller,created_at")
            .eq("marketplace_code", "wb")
            .eq("order_date", day)
            .order("marketplace_sku")
            .order("order_schema")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
            .data
            or []
        )
        for row in batch:
            sku = str(row["marketplace_sku"])
            if sku in cells:
                raise RuntimeError(f"{day}: SKU {sku} пришёл дважды")
            cells[sku] = {
                "qty": Decimal(str(row["orders_qty"])),
                "buyer": Decimal(str(row["orders_amount_buyer"])),
                "seller": Decimal(str(row["orders_amount_seller"])),
            }
        if len(batch) < PAGE_SIZE:
            return cells
        start += PAGE_SIZE


def total(cells, field):
    return sum((c[field] for c in cells.values()), Decimal(0))


def compare_with_db(day, truth_items):
    truth = aggregate(truth_items, day)
    base = stored(day)

    only_truth = sorted(set(truth) - set(base))
    only_base = sorted(set(base) - set(truth))
    # База писалась суммой float, поэтому в numeric лежат хвосты вида
    # 5258633.509999999995. Сверяем до копейки с обеих сторон, а хвосты считаем
    # отдельно — это не расхождение данных, но и не «совпало посимвольно».
    kopeck = Decimal("0.01")
    differs = sorted(
        sku for sku in set(truth) & set(base)
        if any(truth[sku][f].quantize(kopeck) != base[sku][f].quantize(kopeck) for f in ("qty", "buyer", "seller"))
    )
    float_tails = sum(
        1 for cell in base.values() for f in ("buyer", "seller") if cell[f] != cell[f].quantize(kopeck)
    )

    result = {
        "date": day,
        "truth_rows": sum(1 for x in truth_items if str(x.get("date"))[:10] == day),
        "truth_cancelled": int(total(truth, "cancelled")),
        "truth_skus": len(truth), "base_skus": len(base),
        "truth_qty": total(truth, "qty"), "base_qty": total(base, "qty"),
        "truth_seller": total(truth, "seller"), "base_seller": total(base, "seller"),
        "truth_buyer": total(truth, "buyer"), "base_buyer": total(base, "buyer"),
        "sku_only_truth": len(only_truth), "sku_only_base": len(only_base), "sku_differs": len(differs),
        "base_values_with_float_tails": float_tails,
        "sku_only_base_list": only_base[:10],
        "sku_only_base_qty": sum((base[s]["qty"] for s in only_base), Decimal(0)),
        "sku_only_base_seller": sum((base[s]["seller"] for s in only_base), Decimal(0)),
        "sku_differs_list": [
            {"sku": s, "truth": [str(truth[s][f]) for f in ("qty", "buyer", "seller")],
             "base": [str(base[s][f]) for f in ("qty", "buyer", "seller")]}
            for s in differs[:10]
        ],
    }
    return result


def compare_flags(day, flag1_items, flag0_items):
    """Множества srid и значения полей: flag=1 за день против среза flag=0."""
    one = {x["srid"]: x for x in flag1_items if str(x.get("date"))[:10] == day}
    zero = {x["srid"]: x for x in flag0_items if str(x.get("date"))[:10] == day}

    field_diffs = Counter()
    examples = []
    for srid in set(one) & set(zero):
        keys = set(one[srid]) | set(zero[srid])
        for key in keys:
            if one[srid].get(key) != zero[srid].get(key):
                field_diffs[key] += 1
                if len(examples) < 5:
                    examples.append({"srid": srid, "field": key,
                                     "flag1": str(one[srid].get(key)), "flag0": str(zero[srid].get(key))})

    return {
        "date": day,
        "flag1_rows": len(one), "flag0_rows": len(zero),
        "flag1_srid_dupes": sum(1 for x in flag1_items if str(x.get("date"))[:10] == day) - len(one),
        "only_flag1": sorted(set(one) - set(zero))[:10], "only_flag1_count": len(set(one) - set(zero)),
        "only_flag0": sorted(set(zero) - set(one))[:10], "only_flag0_count": len(set(zero) - set(one)),
        "keys_equal": all(set(one[s]) == set(zero[s]) for s in set(one) & set(zero)),
        "field_value_diffs": dict(field_diffs),
        "examples": examples,
    }


def default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True)
    parser.add_argument("--inside", nargs="+", default=["2026-09-05", "2026-08-24"])
    parser.add_argument("--depth", default="2026-03-01")
    parser.add_argument("--eroded", nargs="+",
                        default=["2026-07-21", "2026-06-05", "2026-05-22", "2026-04-02"])
    args = parser.parse_args(argv)

    report = {"calls": load(os.path.join(args.raw, "calls.json"))}

    flag0_path = glob.glob(os.path.join(args.raw, "orders_flag0_from_*.json"))[0]
    flag0 = load(flag0_path)
    window_start = os.path.basename(flag0_path)[len("orders_flag0_from_"):-len(".json")]

    by_date = Counter(str(x["date"])[:10] for x in flag0)
    report["flag0"] = {
        "window_start": window_start,
        "rows": len(flag0),
        "srid_unique": len({x["srid"] for x in flag0}),
        "dates": len(by_date),
        "dates_older_than_window": sum(1 for d in by_date if d < window_start),
        "rows_older_than_window": sum(v for d, v in by_date.items() if d < window_start),
        "oldest_date": min(by_date),
        "last_change_before_order_date": sum(1 for x in flag0 if str(x["lastChangeDate"]) < str(x["date"])),
        "is_cancel": sum(1 for x in flag0 if x.get("isCancel")),
    }

    report["a_inside_window"] = []
    for day in args.inside:
        flag1 = load(os.path.join(args.raw, f"orders_flag1_{day}.json"))
        foreign = sorted({str(x["date"])[:10] for x in flag1} - {day})
        report["a_inside_window"].append({
            "foreign_dates_in_flag1": foreign,
            "flags": compare_flags(day, flag1, flag0),
            "db": compare_with_db(day, flag1),
        })

    depth = load(os.path.join(args.raw, f"orders_flag1_{args.depth}.json"))
    report["b_depth"] = {
        "date": args.depth,
        "rows": len(depth) if isinstance(depth, list) else None,
        "foreign_dates": sorted({str(x["date"])[:10] for x in depth} - {args.depth}) if isinstance(depth, list) else None,
        "db": compare_with_db(args.depth, depth) if isinstance(depth, list) else None,
        "body": depth if not isinstance(depth, list) else None,
    }

    old_stocks = load(os.path.join(args.raw, "stocks_old_get.json"))
    new_stocks = load(os.path.join(args.raw, "stocks_new_post.json"))
    items = ((new_stocks or {}).get("data") or {}).get("items") if isinstance(new_stocks, dict) else None
    report["c_stocks"] = {
        "old_body": old_stocks if not isinstance(old_stocks, list) else f"list[{len(old_stocks)}]",
        "new_top_keys": sorted(new_stocks) if isinstance(new_stocks, dict) else None,
        "new_data_keys": sorted((new_stocks.get("data") or {})) if isinstance(new_stocks, dict) else None,
        "new_items_on_page": len(items) if items is not None else None,
        "new_item_keys": sorted({k for x in items for k in x}) if items else None,
        "new_warehouses": dict(Counter(str((x.get("warehouseId"), x.get("warehouseName"))) for x in items)) if items else None,
        "new_first_item": items[0] if items else None,
    }

    report["d_eroded"] = []
    for day in args.eroded:
        flag1 = load(os.path.join(args.raw, f"orders_flag1_{day}.json"))
        entry = compare_with_db(day, flag1)
        entry["foreign_dates_in_flag1"] = sorted({str(x["date"])[:10] for x in flag1} - {day})
        report["d_eroded"].append(entry)

    out_path = os.path.join(args.raw, "analysis.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=default)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=default))
    return 0


if __name__ == "__main__":
    sys.exit(main())
