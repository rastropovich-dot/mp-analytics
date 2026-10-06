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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=(cabinet.data_path(CABINET.COST_SNAPSHOT_FILE, prof=CABINET) if CABINET.COST_SNAPSHOT_FILE else None),
                        required=not CABINET.COST_SNAPSHOT_FILE, help="файл 1С; по умолчанию — снимок из профиля кабинета (COST_SNAPSHOT_FILE)")
    parser.add_argument("--snapshot-date", required=True, help="дата снимка 1С, YYYY-MM-DD")
    parser.add_argument("--marketplace-code", default="ozon")
    parser.add_argument("--apply", action="store_true", help="писать в БД; без флага — только план, db_writes = 0")
    parser.add_argument("--force", action="store_true", help="писать, даже если строки за этот snapshot_date уже есть")
    parser.add_argument("--check-only", action="store_true", help="не читать файл, только сверить таблицу с заказами")
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
    print(f"строк записано: {written}, в таблице за снимок теперь {after}, db_writes = {written}")
    if after != len(rows):
        print(f"⚠ в таблице {after}, а к записи было {len(rows)} — разница {after - len(rows)} объяснения не имеет, проверить")
    coverage_check(sb, args.snapshot_date, args.marketplace_code)


if __name__ == "__main__":
    main()
