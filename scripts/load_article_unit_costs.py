"""Загрузка снимка себестоимости из файла 1С в article_unit_costs.

Запускает владелец руками. Без --apply ничего не пишет (db_writes = 0), а
печатает всё, что напишет. Таблицу создаёт миграция
sql/20260914_create_article_unit_costs.sql — её применить раньше, в SQL-редакторе.

    venv/bin/python3 scripts/load_article_unit_costs.py --file data/cost_20260520.xlsx --snapshot-date 2026-05-20
    venv/bin/python3 scripts/load_article_unit_costs.py --file data/cost_20260520.xlsx --snapshot-date 2026-05-20 --apply
    venv/bin/python3 scripts/load_article_unit_costs.py --check-only --snapshot-date 2026-05-20

Что делает:
  1. читает лист TDSheet, из каждой строки берёт три offer_id (F / S / T) и
     колонку «Себестоимость» (решение владельца 2026-09-14: не «единая»);
  2. offer_id, встречающийся в нескольких строках с РАЗНОЙ себестоимостью, —
     конфликт: не грузится, не усредняется, печатается поимённо;
     с одинаковой — дубль, берётся первая строка;
  3. пишет upsert по ключу (offer_id_norm, snapshot_date) батчами по 500;
  4. после записи сверяет: сколько ключей marketplace_orders за 90 дней
     находится в таблице и какая доля выручки покрыта.

Стыковка регистронезависимая: у Ozon «-Изгт», в 1С «-ИЗгт». Внутри файла
коллизий по lower() нет (проверено 2026-09-14), поэтому lower() — ключ.
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from supabase import create_client  # noqa: E402

load_dotenv()
import cabinet  # noqa: E402
from loaders import rbh_cost_price  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента

TABLE = "article_unit_costs"
SHEET = "TDSheet"
# Имена колонок по снимкам 1С разные (сорок шестая §2, шесть файлов чата «Товары ECOM» 30.03 … 20.05.26): разбор — по имени колонки,
# не по номеру; сравнение без регистра и лишних пробелов. Первое имя в кортеже — как в снимке 20.05 (наш data/cost_20260520.xlsx).
COLUMN_ALIASES = {
    "main": ("Идентификатор Ozon", "ОЗОН основной"),
    "select": ("селект Ozon", "СЕЛЕКТ Ozon", "Идентификатор Селект", "ОЗОН селект", "Озон селект", "озон селект"),
    "discount": ("дискаунтер Ozon", "Дискаунтер Ozon", "Идентификатор Дискаунтер", "ОЗОН дискаунтер", "озон дискаунтер"),
    "cost": ("Себестоимость", "Себестоимость Озон и ЛК ВБ", "Себестоимость Озон"),
    "uniform": ("единая", "Единая сс", "единая сс", "единая сс для ВБ"),
    "wb_main": ("Идентификатор WB", "Основной ВБ"),
    "wb_discount": ("дискаунтер WB", "Дискаунтер WB", "Идентификатор WB Дискаунтер", "Идентификатор WB дискаунтер", "ВБ дискаунтер", "WB дискаунтерр"),
}
REQUIRED_ROLES = ("main", "cost")
VARIANT_ROLES = ("main", "select", "discount")
BATCH = 500


def _norm_header(name):
    return " ".join(str(name).split()).casefold()


def resolve_columns(columns):
    """{роль: имя колонки файла | None} по COLUMN_ALIASES; нет обязательной (main, cost) — RuntimeError с перечнем шапки."""
    by_norm = {_norm_header(c): c for c in columns}
    out = {}
    for role, aliases in COLUMN_ALIASES.items():
        out[role] = next((by_norm[_norm_header(a)] for a in aliases if _norm_header(a) in by_norm), None)
    missing = [r for r in REQUIRED_ROLES if out[r] is None]
    if missing:
        raise RuntimeError(f"в шапке нет колонок для {missing}; шапка файла: {list(columns)}")
    return out


def money(value):
    """Decimal с двумя знаками или None. float здесь запрещён: how-we-work.md."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text or text.lower() == "nan":
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise RuntimeError(f"не число в колонке себестоимости: {value!r}")


def text_or_none(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def read_snapshot(path, snapshot_date, marketplace_code, with_analytics_group=False):
    df = pd.read_excel(path, sheet_name=SHEET, dtype=str)
    source_file = os.path.basename(path)
    cols = resolve_columns(df.columns)
    stats = {"rows_read": len(df), "offer_ids_seen": 0, "empty_offer_id": 0,
             "empty_cost": 0, "duplicates_same_cost": 0, "conflicts": 0,
             "columns": cols, "wb_ids_seen": 0, "wb_ids_unique": 0, "rows_main": 0, "sum_cost_main_rows": Decimal(0),
             "by_variant_seen": defaultdict(int)}
    by_norm = defaultdict(list)
    wb_ids = set()

    for idx, row in df.iterrows():
        for role in ("wb_main", "wb_discount"):
            wb_id = text_or_none(row.get(cols[role])) if cols[role] else None
            if wb_id:
                stats["wb_ids_seen"] += 1
                wb_ids.add(wb_id.lower())
        unit_cost = money(row.get(cols["cost"]))
        uniform = money(row.get(cols["uniform"])) if cols["uniform"] else None
        if unit_cost is None or unit_cost <= 0:
            stats["empty_cost"] += 3
            continue
        if text_or_none(row.get(cols["main"])):
            stats["rows_main"] += 1
            stats["sum_cost_main_rows"] += unit_cost
        for variant in VARIANT_ROLES:
            column = cols[variant]
            if column is None:
                continue
            offer_id = text_or_none(row.get(column))
            if not offer_id:
                stats["empty_offer_id"] += 1
                continue
            stats["offer_ids_seen"] += 1
            stats["by_variant_seen"][variant] += 1
            by_norm[offer_id.lower()].append({
                "marketplace_code": marketplace_code,
                "offer_id": offer_id,
                "offer_id_norm": offer_id.lower(),
                "snapshot_date": snapshot_date,
                "unit_cost": str(unit_cost),
                "unit_cost_uniform": str(uniform) if uniform is not None else None,
                "currency": "RUB",
                "variant_type": variant,
                "article_1c": text_or_none(row.get("Артикул")) or "",
                "size": text_or_none(row.get("Размер")),
                "insert_category": text_or_none(row.get("КатегорияКамней")),
                "product_kind": text_or_none(row.get("ВидИзделия")),
                "metal_fineness": text_or_none(row.get("Проба")),
                "weight_g": str(money(row.get("Вес"))) if money(row.get("Вес")) is not None else None,
                "discontinued": (text_or_none(row.get("Непроизводится")) or "").lower() == "да",
                "source_file": source_file,
                "source_row": int(idx) + 2,
                # «Аналитическая группа»: колонка добавлена миграцией 20260916_*; без
                # флага не пишем, чтобы загрузчик не падал на таблице без колонки.
                **({"analytics_group": text_or_none(row.get("Аналитическая группа"))} if with_analytics_group else {}),
            })

    stats["wb_ids_unique"] = len(wb_ids)
    rows, conflicts = [], []
    for norm, candidates in by_norm.items():
        costs = sorted({c["unit_cost"] for c in candidates}, key=Decimal)
        if len(costs) > 1:
            stats["conflicts"] += 1
            conflicts.append((candidates[0]["offer_id"], costs,
                              sorted({c["article_1c"] for c in candidates}),
                              sorted(c["source_row"] for c in candidates)))
            continue
        if len(candidates) > 1:
            stats["duplicates_same_cost"] += len(candidates) - 1
        rows.append(candidates[0])
    return rows, conflicts, stats


def print_plan(rows, conflicts, stats):
    print("=== файл")
    print("колонки по ролям:", {k: v for k, v in stats.get("columns", {}).items()})
    print(f"строк прочитано: {stats['rows_read']}; строк с основным Ozon-идентификатором и ценой: {stats.get('rows_main', 0)}, "
          f"Σ себестоимости по ним {stats.get('sum_cost_main_rows', 0):,.2f}; WB-идентификаторов {stats.get('wb_ids_seen', 0)} "
          f"(уникальных без регистра {stats.get('wb_ids_unique', 0)}) — в таблицу Ozon не грузятся")
    print("offer_id по колонкам:", dict(stats.get("by_variant_seen", {})))
    print(f"offer_id получено (три колонки): {stats['offer_ids_seen']}, уникальных по lower(): {len(rows) + stats['conflicts']}")
    print(f"пропущено: пустой offer_id {stats['empty_offer_id']}, пустая/нулевая себестоимость {stats['empty_cost']}, "
          f"конфликтных offer_id {stats['conflicts']}, дублей с той же ценой (свёрнуто) {stats['duplicates_same_cost']}")
    print(f"к записи: {len(rows)} строк")
    if conflicts:
        print(f"=== конфликты — НЕ грузятся, не усредняются ({len(conflicts)}):")
        for offer_id, costs, articles, src in sorted(conflicts):
            print(f"  {offer_id:<22} {' / '.join(costs):<24} артикул 1С {', '.join(articles)}  строки файла {src}")
    by_variant = defaultdict(int)
    for r in rows:
        by_variant[r["variant_type"]] += 1
    print("по типу варианта:", dict(by_variant))


def supabase_client():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("нет SUPABASE_URL / SUPABASE_SERVICE_KEY в окружении")
    return create_client(url, key)


def table_exists(sb):
    try:
        sb.table(TABLE).select("offer_id_norm").limit(1).execute()
        return True
    except Exception as exc:  # PGRST205 — таблицы нет
        if "PGRST205" in str(exc) or TABLE in str(exc):
            return False
        raise


def count_snapshot(sb, snapshot_date):
    res = sb.table(TABLE).select("offer_id_norm", count="exact").eq("snapshot_date", snapshot_date).limit(1).execute()
    return res.count or 0


def apply_rows(sb, rows):
    written = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        sb.table(TABLE).upsert(batch, on_conflict="offer_id_norm,snapshot_date").execute()
        written += len(batch)
        print(f"  записано {written} / {len(rows)}", flush=True)
    return written


def coverage_check(sb, snapshot_date, marketplace_code, days=90, today=None):
    """Ключи marketplace_orders за N дней против таблицы. Ожидание 2026-09-14: 4 621 из 4 622, 99,997 % выручки.

    Вес ключа — выручка по цене продавца (orders_amount_seller). До 2026-09-24 читалась orders_amount_buyer, тогда — дубль
    цены продавца; с тридцать шестой в ней оплачено покупателем (или null) — как вес покрытия она занижала бы дорогие SKU."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    keys = defaultdict(lambda: Decimal(0))
    page = 0
    # ORDER BY обязателен: range без сортировки у PostgREST отдаёт страницы с
    # повторами и пропусками (поймано 2026-09-15 на реализации — мнимые
    # расхождения 09-01 и 09-08 при нулевых по SKU).
    query = (sb.table("marketplace_orders").select("marketplace_sku,article,orders_amount_seller")
             .eq("marketplace_code", marketplace_code).gte("order_date", since)
             .order("order_date").order("marketplace_sku").order("order_schema"))
    while True:
        res = query.range(page * 1000, page * 1000 + 999).execute()
        for r in res.data:
            keys[(str(r["marketplace_sku"] or ""), str(r["article"] or ""))] += Decimal(str(r["orders_amount_seller"] or 0))
        if len(res.data) < 1000:
            break
        page += 1
    norms = sorted({k[1].lower() for k in keys})
    found = set()
    for i in range(0, len(norms), BATCH):
        res = (sb.table(TABLE).select("offer_id_norm").eq("snapshot_date", snapshot_date)
               .in_("offer_id_norm", norms[i:i + BATCH]).execute())
        found.update(r["offer_id_norm"] for r in res.data)
    total = sum(keys.values())
    hit_rev = sum(v for k, v in keys.items() if k[1].lower() in found)
    hit = sum(1 for k in keys if k[1].lower() in found)
    missing = sorted(((k, v) for k, v in keys.items() if k[1].lower() not in found), key=lambda x: -x[1])
    print(f"=== сверка с marketplace_orders за {days} дней (с {since}), снимок {snapshot_date}")
    print(f"ключей {len(keys)}, найдено {hit} ({hit / len(keys) * 100:.2f} %), выручка {total:,.2f}, покрыто {hit_rev:,.2f} ({hit_rev / total * 100:.3f} %)")
    print(f"не найдено {len(missing)}, их выручка {total - hit_rev:,.2f} ({(total - hit_rev) / total * 100:.3f} %)")
    for (sku, article), rev in missing[:20]:
        print(f"  {article:<24} sku {sku:<12} {rev:>14,.2f}")
    print("ожидание по проверке 2026-09-14: 4 621 из 4 622 ключей, не найден один — F007780899-ТП (26 389,00, новее снимка);"
          " 99,97 % из inbox считались с учётом регистра, здесь регистр снят, поэтому выше.")
    return hit, len(keys), hit_rev, total


def print_plan_rbh(rows, conflicts, stats, path):
    print(f"=== прайс РБХ {os.path.basename(path)}: колонки {stats['columns']}")
    print(f"строк прочитано {stats['rows_read']}; пропущено: пустой артикул {stats['empty_article']}, пустая/нулевая цена {stats['empty_cost']}, "
          f"артикул «0» {stats.get('zero_article', 0)} (из них с Sku OZ {stats.get('zero_article_sku_oz', 0)}); "
          f"конфликтных артикулов {stats['conflicts']}, дублей с той же ценой {stats['duplicates_same_cost']}")
    print(f"к записи: {len(rows)} строк; НДС: ставка {stats['vat_rate']} — unit_cost = цена ÷ ставка (без НДС), цена с НДС в таблицу не пишется (восстановима как unit_cost × ставка; источник — файл)")
    print(f"Σ без НДС {stats['sum_without_vat']:,.2f}; Σ с НДС {stats['sum_with_vat']:,.2f}; Sku OZ заполнен у {stats['sku_oz_filled']}, "
          f"дублей Sku OZ {len(stats['sku_oz_duplicates'])}: {stats['sku_oz_duplicates'][:5]}")
    if conflicts:
        print(f"=== конфликты — НЕ грузятся ({len(conflicts)}):")
        for art, prices, src in sorted(conflicts):
            print(f"  {art:<16} {' / '.join(prices):<24} строки файла {src}")
    print(f"=== бренды ({len(stats['brands'])}): артикулов / Σ без НДС / Σ с НДС")
    for brand, (n, s_wo, s_w) in sorted(stats["brands"].items(), key=lambda kv: -kv[1][0]):
        print(f"  {brand:<28} {n:>5} {s_wo:>14,.2f} {s_w:>14,.2f}")


def _paged(query, order_cols, page=1000):
    out, start = [], 0
    for c in order_cols:
        query = query.order(c)
    while True:
        res = query.range(start, start + page - 1).execute()
        out.extend(res.data or [])
        if len(res.data or []) < page:
            return out
        start += page


def coverage_check_rbh(sb, price_rows, marketplace_code="ozon"):
    """Покрытие кабинета прайсом: Ozon — карточки ozon_products (offer_id ↔ артикул, Sku OZ ↔ sku) и выкупы Ozon по SKU (вся таблица,
    у РБХ это окно §5.4 60 дней); WB — nm_id выкупов WB ↔ vendor_code (воронка за последние дни + отчёт реализации). Только чтение."""
    norms = {r["offer_id_norm"] for r in price_rows}
    sku_map = rbh_cost_price.sku_oz_map(price_rows)
    # --- Ozon
    products = _paged(sb.table("ozon_products").select("sku,offer_id,name"), ["sku"])
    by_art = sum(1 for p in products if (p.get("offer_id") or "").lower() in norms)
    by_sku = sum(1 for p in products if str(p.get("sku")) in sku_map)
    mismatch = [(p["sku"], p.get("offer_id"), sku_map[str(p["sku"])]) for p in products
                if str(p.get("sku")) in sku_map and (p.get("offer_id") or "").lower() != sku_map[str(p["sku"])].lower()]
    offer_by_sku = {str(p["sku"]): (p.get("offer_id") or "", p.get("name") or "") for p in products}
    buy = _paged(sb.table("marketplace_buyouts").select("marketplace_sku,buyouts_amount_seller").eq("marketplace_code", marketplace_code),
                 ["buyout_date", "marketplace_sku"])
    rub = defaultdict(Decimal)
    for b in buy:
        rub[str(b["marketplace_sku"])] += Decimal(str(b.get("buyouts_amount_seller") or 0))
    covered = {s for s in rub if offer_by_sku.get(s, ("", ""))[0].lower() in norms or s in sku_map}
    total = sum(rub.values()); hit = sum(rub[s] for s in covered)
    print(f"=== покрытие Ozon: карточек {len(products)}, с ценой по артикулу {by_art} ({by_art / max(len(products), 1) * 100:.2f} %), по Sku OZ {by_sku}; "
          f"артикул карточки ≠ артикулу прайса при том же Sku OZ: {len(mismatch)} {mismatch[:5]}")
    print(f"    выкупы Ozon: SKU {len(rub)}, с ценой {len(covered)}; оборот {total:,.2f}, покрыт {hit:,.2f} ({(hit / total * 100) if total else 0:.2f} %)")
    miss = sorted(((s, rub[s]) for s in rub if s not in covered), key=lambda x: -x[1])
    print(f"    SKU без цены: {len(miss)}, их оборот {total - hit:,.2f}; топ-20:")
    for s, v in miss[:20]:
        off, name = offer_by_sku.get(s, ("(нет карточки)", ""))
        print(f"      sku {s:<12} артикул {off:<14} {v:>12,.2f}  {name[:60]}")
    # --- WB
    wbuy = _paged(sb.table("marketplace_buyouts").select("marketplace_sku,buyouts_amount_seller").eq("marketplace_code", "wb"), ["buyout_date", "marketplace_sku"])
    wrub = defaultdict(Decimal)
    for b in wbuy:
        wrub[str(b["marketplace_sku"])] += Decimal(str(b.get("buyouts_amount_seller") or 0))
    nm2vendor = {}
    days = sb.table("wb_funnel_products_daily").select("day").order("day", desc=True).limit(1).execute().data
    if days:
        last = days[0]["day"]
        for r in _paged(sb.table("wb_funnel_products_daily").select("nm_id,vendor_code").eq("day", last), ["nm_id"]):
            nm2vendor[str(r["nm_id"])] = (r.get("vendor_code") or "").strip()      # у части карточек WB vendorCode с переводом строки в конце
    missing_nm = sorted(n for n in wrub if n not in nm2vendor)
    for i in range(0, len(missing_nm), 200):
        for r in (sb.table("wb_sales_report_rows").select("nm_id,vendor_code").in_("nm_id", missing_nm[i:i + 200]).limit(5000).execute().data or []):
            nm2vendor.setdefault(str(r["nm_id"]), (r.get("vendor_code") or "").strip())
    wcov = {n for n in wrub if nm2vendor.get(n, "").lower() in norms}
    wtotal = sum(wrub.values()); whit = sum(wrub[n] for n in wcov)
    print(f"=== покрытие WB: nm_id в выкупах {len(wrub)}, артикул найден у {sum(1 for n in wrub if nm2vendor.get(n))}, с ценой {len(wcov)}; "
          f"оборот {wtotal:,.2f}, покрыт {whit:,.2f} ({(whit / wtotal * 100) if wtotal else 0:.2f} %)")
    wmiss = sorted(((n, wrub[n]) for n in wrub if n not in wcov), key=lambda x: -x[1])
    print(f"    nm_id без цены: {len(wmiss)}, их оборот {wtotal - whit:,.2f}; топ-20:")
    for n, v in wmiss[:20]:
        print(f"      nm {n:<12} vendorCode {nm2vendor.get(n, '(нет)'):<14} {v:>12,.2f}")
    return {"ozon_products": len(products), "ozon_by_article": by_art, "ozon_share": (hit / total) if total else None,
            "wb_share": (whit / wtotal) if wtotal else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=(cabinet.data_path(CABINET.COST_SNAPSHOT_FILE, prof=CABINET) if CABINET.COST_SNAPSHOT_FILE else None),
                        required=not CABINET.COST_SNAPSHOT_FILE, help="файл 1С; по умолчанию — снимок из профиля кабинета (COST_SNAPSHOT_FILE)")
    parser.add_argument("--snapshot-date", required=True, help="дата снимка 1С, YYYY-MM-DD")
    parser.add_argument("--marketplace-code", default="ozon")
    parser.add_argument("--apply", action="store_true", help="писать в БД; без флага — только план, db_writes = 0")
    parser.add_argument("--force", action="store_true", help="писать, даже если строки за этот snapshot_date уже есть")
    parser.add_argument("--check-only", action="store_true", help="не читать файл, только сверить таблицу с заказами")
    parser.add_argument("--format", choices=("1c", "rbh"), default=getattr(CABINET, "COST_PRICE_FORMAT", "1c") or "1c",
                        help="1c — файл 1С (TDSheet, F/S/T); rbh — прайс РБХ (Артикул / Бренд / «С-ть до МП» с НДС / Sku OZ); умолчание — COST_PRICE_FORMAT профиля")
    parser.add_argument("--vat-rate", default=None, help="формат rbh: цена с НДС ÷ ставка → unit_cost; умолчание — COST_PRICE_VAT_RATE профиля (1,22)")
    parser.add_argument("--side-block", action="store_true", help="формат rbh: добавить боковой блок «Артикул поставщика | … | Себестоимость по "
                        "умолчанию» — только артикулы, которых нет в основном, с одной ценой (прайс «до 01.09», слово 27.10)")
    parser.add_argument("--analytics-group", action="store_true", help="писать «Аналитическая группа» (нужна миграция 20260916_add_analytics_group_*)")
    args = parser.parse_args()
    date.fromisoformat(args.snapshot_date)

    if args.check_only:
        sb = supabase_client()
        if not table_exists(sb):
            raise SystemExit(f"таблицы {TABLE} нет — применить sql/20260914_create_article_unit_costs.sql")
        coverage_check(sb, args.snapshot_date, args.marketplace_code)
        print("db_writes = 0")
        return

    if args.format == "rbh":
        vat = Decimal(str(args.vat_rate)) if args.vat_rate else getattr(CABINET, "COST_PRICE_VAT_RATE", None)
        price_rows, conflicts, stats = rbh_cost_price.read_price(args.file, args.snapshot_date, args.marketplace_code, vat_rate=vat)
        print_plan_rbh(price_rows, conflicts, stats, args.file)
        if args.side_block:                              # прайс «до 01.09»: карта артикулов WB с суффиксами (слово владельца 27.10)
            side, sst = rbh_cost_price.read_side_block(args.file, {r["offer_id_norm"] for r in price_rows}, args.snapshot_date,
                                                       args.marketplace_code, vat_rate=vat)
            print(f"=== боковой блок: шапка {sst['header']}, строк {sst['side_rows']}, артикулов {sst['side_keys']}; есть в основном {sst['in_main']}; "
                  f"двойная цена {len(sst['double_price'])} (не грузятся): {sst['double_price'][:8]}; к записи {sst['added']}")
            price_rows = price_rows + side
        rows = rbh_cost_price.db_rows(price_rows)
        sb = supabase_client()
        coverage_check_rbh(sb, price_rows, args.marketplace_code)
    else:
        rows, conflicts, stats = read_snapshot(args.file, args.snapshot_date, args.marketplace_code, args.analytics_group)
        print_plan(rows, conflicts, stats)

    if not args.apply:
        print("\nрежим плана: db_writes = 0. Для записи добавить --apply.")
        return

    sb = supabase_client()
    if not table_exists(sb):
        raise SystemExit(f"таблицы {TABLE} нет — сначала применить sql/20260914_create_article_unit_costs.sql в SQL-редакторе")
    existing = count_snapshot(sb, args.snapshot_date)
    if existing and not args.force:
        raise SystemExit(f"за snapshot_date {args.snapshot_date} уже {existing} строк; повторная запись — с --force (upsert по ключу)")
    print(f"\n=== запись: снимок {args.snapshot_date}, было строк {existing}")
    written = apply_rows(sb, rows)
    after = count_snapshot(sb, args.snapshot_date)
    if args.format == "rbh":
        print(f"записано {written}, в таблице за снимок {after}; db_writes = {written}")
        coverage_check_rbh(sb, price_rows, args.marketplace_code)
        return
    print(f"строк записано: {written}, в таблице за снимок теперь {after}, db_writes = {written}")
    if after != len(rows):
        print(f"⚠ в таблице {after}, а к записи было {len(rows)} — разница {after - len(rows)} объяснения не имеет, проверить")
    coverage_check(sb, args.snapshot_date, args.marketplace_code)


if __name__ == "__main__":
    main()
