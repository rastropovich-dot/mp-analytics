#!/usr/bin/env python3
"""Категория товара из карточки Ozon — одной таблицей (тридцать седьмая §3).

    venv/bin/python3 scripts/ozon_product_catalog.py --collect [--tree-file data/ozon_products/category_tree.json]   снять и разобрать, в БД не писать
    venv/bin/python3 scripts/ozon_product_catalog.py --from-file data/ozon_products/catalog_latest.json              разбор из снимка, в API не ходить
    venv/bin/python3 scripts/ozon_product_catalog.py --from-file … --apply                                            записать ozon_products (по слову)

SKU — все из marketplace_orders ∪ marketplace_buyouts (Ozon, ≈ 5 300). Карточки — POST /v3/product/info/list по 1 000 SKU за
обращение (≈ 6), имена категорий и типов — POST /v1/description-category/tree (1 обращение; --tree-file — взять снятое ранее).
Категория владельца — словарь TYPE_TO_CATEGORY по type_name карточки; тип, которого в словаре нет, — «прочее» И называется
вслух со счётчиком. SKU без карточки в ответе — категория по названию (NAME_RULES), category_source = 'name'.

Печатает: покрытие (SKU с карточкой / с типом / по названию), таблицу «категория · SKU · оборот выкупов за месяц», список типов,
ушедших в «прочее», и две сверки: карточка против названия и карточка против 1С (product_kind из article_unit_costs по
артикулу) — совпало / расходится с примерами. Сырьё и разбор — data/ozon_products/ (вне git).

--apply (по слову владельца; таблица создаётся миграцией sql/20260924_create_ozon_products.sql): upsert по sku, контроль чтением;
отказ в окнах ночного прогона и утреннего алерта. Без --apply в БД не пишет. Ночного шага нет — обновлять по слову (раз в неделю).
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import http_retry  # noqa: E402
from loaders import ozon_finance_accrual as accrual  # noqa: E402
from loaders.pipeline_window import in_morning_alert_window, in_nightly_run_window  # noqa: E402
import report_ozon_month as rep  # noqa: E402

TABLE = "ozon_products"
OUT_DIR = os.path.join(ROOT, "data", "ozon_products")
BATCH = 1000
OWNER_CATEGORIES = ("кольца", "серьги", "подвески", "цепочки", "браслеты", "пирсинг", "колье", "броши")
OTHER = "прочее"
Z = Decimal(0)

# type_name карточки → категория владельца. Решение сессии 2026-09-24 (поправить словарём, если владелец делит иначе):
# крестики и шармы — подвески, бусы — колье, шнурки — цепочки, браслет для часов — браслеты. Всё остальное — «прочее», вслух.
TYPE_TO_CATEGORY = {
    "Кольцо ювелирное": "кольца",
    "Серьги ювелирные": "серьги",
    "Подвеска ювелирная": "подвески", "Крестик ювелирный": "подвески", "Шарм ювелирный": "подвески",
    "Цепочка ювелирная": "цепочки", "Шнурок ювелирный": "цепочки",
    "Браслет ювелирный": "браслеты", "Браслет ювелирный для часов": "браслеты",
    "Пирсинг ювелирный": "пирсинг",
    "Колье ювелирное": "колье", "Бусы ювелирные": "колье",
    "Брошь ювелирная": "броши",
}
# product_kind 1С (article_unit_costs) → категория владельца — для сверки
KIND_TO_CATEGORY = {
    "Кольцо": "кольца", "Обручальное кольцо": "кольца", "Печатка": "кольца",
    "Серьги": "серьги", "Пуссеты": "серьги", "Серьга": "серьги", "Конго": "серьги",
    "Подвеска": "подвески", "Крест": "подвески", "Иконка": "подвески", "Знак зодиака": "подвески",
    "Цепь": "цепочки", "Браслет": "браслеты", "Колье": "колье", "Брошь": "броши", "Пирсинг": "пирсинг",
    "Булавка": OTHER, "Запонки": OTHER, "Зажим": OTHER,
}
# подстрока названия → категория; порядок важен («пирсинг» раньше «кольц»: «кольцо для пирсинга»)
NAME_RULES = (("пирсинг", "пирсинг"), ("серьг", "серьги"), ("пуссет", "серьги"), ("кольц", "кольца"), ("печатк", "кольца"),
              ("подвес", "подвески"), ("кулон", "подвески"), ("крест", "подвески"), ("икон", "подвески"), ("ладанк", "подвески"), ("шарм", "подвески"),
              ("цеп", "цепочки"), ("шнур", "цепочки"), ("браслет", "браслеты"), ("колье", "колье"), ("бусы", "колье"), ("ожерел", "колье"),
              ("брош", "броши"))


def category_by_type(type_name):
    return TYPE_TO_CATEGORY.get(str(type_name or "").strip())


def category_by_name(name):
    low = str(name or "").lower()
    for needle, cat in NAME_RULES:
        if needle in low:
            return cat
    return OTHER


def brand_of(offer_id):
    first = str(offer_id or "").strip()[:1].upper()
    return "Топаз" if first == "T" else ("KARATOV" if first else None)


def metal_of(name):
    m = rep.metal_of(name)
    return None if m == rep.NO_METAL else m


def flatten_tree(tree):
    """type_id → (type_name, путь категорий) и description_category_id → путь."""
    types, cats = {}, {}

    def walk(nodes, path):
        for n in nodes:
            name = n.get("category_name") or n.get("type_name")
            if n.get("type_id"):
                types[int(n["type_id"])] = (n.get("type_name") or name, " / ".join(path))
            if n.get("description_category_id"):
                cats[int(n["description_category_id"])] = " / ".join(path + [name])
            walk(n.get("children") or [], path + [name])
    walk(tree.get("result") or [], [])
    return types, cats


def sku_universe(sb):
    """{sku: {article, name, month_turnover}} — из заказов и выкупов Ozon; артикул — самый частый у SKU (как в генераторе)."""
    info = {}
    orders = rep.fetch(sb, "marketplace_orders", "id,order_date,marketplace_sku,article,product_name,orders_qty,cancelled_orders_qty",
                       [("eq", "marketplace_code", "ozon"), ("gte", "order_date", "2026-03-28")], ["order_date", "marketplace_code", "marketplace_sku", "order_schema"])
    arts = defaultdict(lambda: defaultdict(Decimal))
    for r in orders:
        sku = str(r["marketplace_sku"])
        arts[sku][str(r.get("article") or "")] += rep.D(r.get("orders_qty")) + rep.D(r.get("cancelled_orders_qty"))
        info.setdefault(sku, {"article": "", "name": r.get("product_name") or "", "turnover": Z})
        if not info[sku]["name"] and r.get("product_name"):
            info[sku]["name"] = r["product_name"]
    for sku, a in arts.items():
        info[sku]["article"] = max(sorted(a.items()), key=lambda kv: kv[1])[0]
    buyouts = rep.fetch(sb, "marketplace_buyouts", "id,buyout_date,marketplace_sku,article,product_name,buyouts_amount_seller",
                        [("eq", "marketplace_code", "ozon"), ("gte", "buyout_date", "2026-03-28")], ["buyout_date", "marketplace_code", "marketplace_sku"])
    return info, orders, buyouts


def month_turnover(buyouts, month):
    out = defaultdict(Decimal)
    for r in buyouts:
        if str(r["buyout_date"]).startswith(month):
            out[str(r["marketplace_sku"])] += rep.D(r.get("buyouts_amount_seller"))
    return out


def fetch_cards(skus, stats):
    items = []
    for i in range(0, len(skus), BATCH):
        chunk = [int(s) for s in skus[i:i + BATCH] if str(s).isdigit()]
        r = http_retry.post(f"{accrual.BASE}/v3/product/info/list", label="product/info/list", headers=accrual.headers(),
                            json={"sku": chunk}, timeout=120, stats=stats)
        if r.status_code != 200:
            raise RuntimeError(f"product/info/list HTTP {r.status_code}: {r.text[:200]}")
        items.extend((r.json() or {}).get("items") or [])
    return items


def fetch_tree(stats):
    r = http_retry.post(f"{accrual.BASE}/v1/description-category/tree", label="category/tree", headers=accrual.headers(),
                        json={"language": "RU"}, timeout=180, stats=stats)
    if r.status_code != 200:
        raise RuntimeError(f"description-category/tree HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def build_rows(info, items, tree, observed_at):
    """Строки ozon_products + счётчики. Карточка ищется по sku ответа и по sources[].sku."""
    types, cats = flatten_tree(tree)
    by_sku = {}
    for it in items:
        for s in {str(it.get("sku") or "")} | {str(x.get("sku") or "") for x in (it.get("sources") or [])}:
            if s:
                by_sku.setdefault(s, it)
    rows, counters, other_types = [], Counter(), Counter()
    for sku, meta in sorted(info.items()):
        it = by_sku.get(sku)
        name = (it or {}).get("name") or meta["name"]
        offer_id = (it or {}).get("offer_id") or meta["article"]
        row = {"sku": sku, "product_id": (it or {}).get("id"), "offer_id": offer_id, "name": name,
               "description_category_id": (it or {}).get("description_category_id"), "type_id": (it or {}).get("type_id"),
               "brand": brand_of(offer_id), "metal": metal_of(name), "is_archived": (it or {}).get("is_archived"),
               "status": ((it or {}).get("statuses") or {}).get("status_name"), "observed_at": observed_at}
        row["category_path"] = cats.get(row["description_category_id"]) if row["description_category_id"] else None
        type_name = types.get(int(row["type_id"]))[0] if row["type_id"] and int(row["type_id"]) in types else None
        row["type_name"] = type_name
        if it is None:
            row["category"], row["category_source"] = category_by_name(name), "name"
            counters["no_card"] += 1
        else:
            counters["card"] += 1
            cat = category_by_type(type_name)
            if cat is None:
                other_types[type_name or f"type_id {row['type_id']}"] += 1
                cat = OTHER
            row["category"], row["category_source"] = cat, "card"
        rows.append(row)
    return rows, dict(counters), other_types


def compare(rows, kinds_by_art):
    """Карточка против названия и против 1С: (совпало, расходится, примеры)."""
    vs_name = {"agree": 0, "differ": 0, "examples": []}
    vs_1c = {"agree": 0, "differ": 0, "no_1c": 0, "examples": []}
    for r in rows:
        if r["category_source"] != "card":
            continue
        by_name = category_by_name(r["name"])
        if by_name == r["category"]:
            vs_name["agree"] += 1
        else:
            vs_name["differ"] += 1
            if len(vs_name["examples"]) < 8:
                vs_name["examples"].append((r["sku"], r["type_name"], r["category"], by_name, (r["name"] or "")[:60]))
        kind = kinds_by_art.get(str(r["offer_id"] or "").lower())
        if kind is None:
            vs_1c["no_1c"] += 1
            continue
        c1 = KIND_TO_CATEGORY.get(kind)
        if c1 == r["category"]:
            vs_1c["agree"] += 1
        else:
            vs_1c["differ"] += 1
            if len(vs_1c["examples"]) < 8:
                vs_1c["examples"].append((r["sku"], r["type_name"], r["category"], kind, (r["name"] or "")[:60]))
    return vs_name, vs_1c


def load_kinds(sb, offer_ids, snapshot=rep.SNAP):
    norms = sorted({str(o).lower() for o in offer_ids if o})
    out = {}
    for i in range(0, len(norms), 500):
        res = sb.table("article_unit_costs").select("offer_id_norm,product_kind").eq("snapshot_date", snapshot).in_("offer_id_norm", norms[i:i + 500]).order("offer_id_norm").execute()
        for r in res.data:
            out[r["offer_id_norm"]] = r.get("product_kind")
    return out


def print_report(rows, counters, other_types, turnover, vs_name, vs_1c, month):
    n = len(rows)
    card = counters.get("card", 0)
    print(f"SKU в базе (заказы ∪ выкупы Ozon): {n}; с карточкой {card} ({card / n * 100:.2f} %), без карточки → по названию {counters.get('no_card', 0)}")
    by_cat = defaultdict(lambda: [0, Z])
    for r in rows:
        by_cat[r["category"]][0] += 1
        by_cat[r["category"]][1] += turnover.get(r["sku"], Z)
    total_t = sum((v[1] for v in by_cat.values()), Z)
    print(f"\n{'категория':12}{'SKU':>7}{'оборот выкупов ' + month:>28}{'доля':>8}")
    for cat in list(OWNER_CATEGORIES) + [OTHER]:
        k, t = by_cat.get(cat, (0, Z))
        print(f"{cat:12}{k:>7}{t:>28,.2f}{(t / total_t * 100 if total_t else 0):>7.2f}%")
    print(f"{'итого':12}{n:>7}{total_t:>28,.2f}")
    with_cat = sum(v[0] for c, v in by_cat.items() if c != OTHER)
    print(f"доля SKU с категорией (не «прочее»): {with_cat / n * 100:.2f} %")
    if other_types:
        print("типы карточек, ушедшие в «прочее» (словаря нет): " + ", ".join(f"{t} — {c}" for t, c in other_types.most_common()))
    print(f"\nкарточка против названия: совпало {vs_name['agree']}, расходится {vs_name['differ']}")
    for ex in vs_name["examples"]:
        print(f"   sku {ex[0]}: тип «{ex[1]}» → {ex[2]}, по названию {ex[3]} — {ex[4]}")
    print(f"карточка против 1С (product_kind по артикулу): совпало {vs_1c['agree']}, расходится {vs_1c['differ']}, артикула нет в 1С {vs_1c['no_1c']}")
    for ex in vs_1c["examples"]:
        print(f"   sku {ex[0]}: тип «{ex[1]}» → {ex[2]}, 1С «{ex[3]}» — {ex[4]}")


def table_exists(sb):
    try:
        sb.table(TABLE).select("sku").limit(1).execute()
        return True
    except Exception as exc:
        if TABLE in str(exc) or "PGRST205" in str(exc) or "42P01" in str(exc):
            return False
        raise


def apply(sb, rows):
    now = datetime.now(timezone.utc)
    if in_nightly_run_window(now) or in_morning_alert_window(now):
        raise SystemExit("окно ночного прогона или утреннего алерта — не пишу")
    if not table_exists(sb):
        raise SystemExit(f"таблицы {TABLE} нет — применить sql/20260924_create_ozon_products.sql (по слову владельца)")
    payload = [{k: v for k, v in r.items()} for r in rows]
    for i in range(0, len(payload), 500):
        sb.table(TABLE).upsert(payload[i:i + 500], on_conflict="sku").execute()
    have = 0
    page = 0
    while True:
        res = sb.table(TABLE).select("sku").order("sku").range(page * 1000, page * 1000 + 999).execute()
        have += len(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    print(f"записано (upsert по sku) {len(payload)}; в таблице {have} строк {'=' if have >= len(payload) else '≠ ОШИБКА'}")
    print(f"db_writes = {len(payload)}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true", help="снять карточки и дерево (API), разобрать, сохранить снимок")
    ap.add_argument("--from-file", help="разобрать из сохранённого снимка catalog_*.json, в API не ходить")
    ap.add_argument("--tree-file", help="файл дерева категорий (не снимать заново)")
    ap.add_argument("--month", default=None, help="месяц для колонки «оборот выкупов» (YYYY-MM), по умолчанию текущий")
    ap.add_argument("--apply", action="store_true", help="записать ozon_products (по слову владельца)")
    args = ap.parse_args(argv)
    if not args.collect and not args.from_file:
        raise SystemExit("нужен --collect или --from-file")
    from loaders.ozon_fbo_orders_loader import supabase as sb   # тот же клиент, что у ночных шагов и генератора
    month = args.month or date.today().isoformat()[:7]
    info, _orders, buyouts = sku_universe(sb)
    turnover = month_turnover(buyouts, month)
    stats = {}
    os.makedirs(OUT_DIR, exist_ok=True)
    if args.from_file:
        snap = json.load(open(args.from_file))
        items, tree, observed_at = snap["items"], snap["tree"], snap["observed_at"]
        print(f"снимок {args.from_file}: карточек {len(items)}, снято {observed_at}; в API не ходил")
    else:
        observed_at = datetime.now(timezone.utc).isoformat()
        items = fetch_cards(sorted(info), stats)
        tree = json.load(open(args.tree_file)) if args.tree_file else fetch_tree(stats)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(OUT_DIR, f"catalog_{stamp}.json")
        json.dump({"observed_at": observed_at, "skus": sorted(info), "items": items, "tree": tree}, open(path, "w"), ensure_ascii=False)
        print(f"снимок → {path} (карточек {len(items)} на {len(info)} SKU); обращений к Seller API {stats.get('requests', 0)}, "
              f"повторов {stats.get('retries', 0)}, отказов {stats.get('failures', 0)}" + ("" if args.tree_file else " (включая дерево)"))
    rows, counters, other_types = build_rows(info, items, tree, observed_at)
    kinds = load_kinds(sb, [r["offer_id"] for r in rows])
    vs_name, vs_1c = compare(rows, kinds)
    print_report(rows, counters, other_types, turnover, vs_name, vs_1c, month)
    latest = os.path.join(OUT_DIR, "catalog_latest.json")
    json.dump({"observed_at": observed_at, "rows": rows}, open(latest, "w"), ensure_ascii=False, default=str)
    print(f"разбор → {latest} ({len(rows)} строк)")
    if args.apply:
        apply(sb, rows)
    else:
        print("db_writes = 0" + ("" if table_exists(sb) else f" (таблицы {TABLE} ещё нет — миграция sql/20260924_create_ozon_products.sql по слову)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
