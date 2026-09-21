import os
from collections import defaultdict
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# Явный список: что в него не входит, попадает в прочее И поднимается в отчёте.
# Тип, который не должен попадать в группу, не должен начинаться с её префикса —
# пять мест берут рекламу по startswith("advertising"), поэтому внешнее
# продвижение называется external_promo, а не advertising_external.
EXPENSE_TYPE_BUCKETS = {
    "commission": "commission_amount",
    "logistics": "logistics_amount",
    "advertising_clicks": "ad_spend",
    "advertising_order_5": "ad_spend",
    "advertising_order_selected_cpo": "ad_spend",
    "advertising_other": "ad_spend",
    "other": "other_expenses_amount",
    "subscription": "other_expenses_amount",
    "external_promo": "other_expenses_amount",
}


def empty_kpi_row(kpi_date, marketplace_code, marketplace_sku, article="", product_name=None):
    return {
        "kpi_date": kpi_date,
        "marketplace_code": marketplace_code,
        "marketplace_sku": marketplace_sku,
        "article": article,
        "product_name": product_name,
        "orders_qty": 0,
        "orders_amount_seller": 0,
        "buyouts_qty": 0,
        "buyouts_amount_seller": 0,
        "buyout_rate": 0,
        "ad_spend": 0,
        "ad_orders_qty": 0,
        "ad_orders_revenue": 0,
        "organic_orders_qty": 0,
        "organic_orders_revenue": 0,
        "ad_share_of_orders": 0,
        "ad_share_orders": 0,
        "ad_share_revenue": 0,
        "roas": 0,
        "commission_amount": 0,
        "logistics_amount": 0,
        "other_expenses_amount": 0,
    }


PAGE_SIZE = 1000


def read_all_by_id(table):
    """Вся таблица страницами: сортировка по id, следующая страница — id больше последнего.

    Было range() без order(): PostgREST без сортировки не обещает порядка, и страницы могут
    прийти с повторами и пропусками (how-we-work, 15 сентября — так сумма из 7 619 строк
    вышла другой). Страница по ключу, а не по смещению, ещё и не дрейфует, если в таблицу
    пишут во время чтения, и стоит одинаково на первой и на трёхсотой странице.
    """
    rows, last_id = [], None
    while True:
        query = supabase.table(table).select("*").order("id").limit(PAGE_SIZE)
        if last_id is not None:
            query = query.gt("id", last_id)
        page = query.execute().data or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        last_id = page[-1]["id"]
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        # Невозможно при чтении по ключу; если случилось — чтение сломано, и молча считать KPI из него нельзя.
        raise RuntimeError(f"{table}: {len(ids) - len(set(ids))} повторов id в постраничном чтении")
    return rows


def read_all_by_key(table, key_columns):
    """Таблица без id: range() с сортировкой по ПОЛНОМУ первичному ключу — порядок однозначен."""
    rows, start = [], 0
    while True:
        query = supabase.table(table).select("*")
        for column in key_columns:
            query = query.order(column)
        page = query.range(start, start + PAGE_SIZE - 1).execute().data or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    keys = [tuple(row.get(column) for column in key_columns) for row in rows]
    if len(set(keys)) != len(keys):
        raise RuntimeError(f"{table}: {len(keys) - len(set(keys))} повторов ключа {key_columns} в постраничном чтении")
    return rows


def load_orders():
    all_rows = read_all_by_id("marketplace_orders")
    print(f"Загружено заказов: {len(all_rows)}")
    return all_rows


def load_buyouts():
    all_rows = read_all_by_id("marketplace_buyouts")
    print(f"Загружено выкупов: {len(all_rows)}")
    return all_rows


def load_expenses():
    all_rows = read_all_by_id("marketplace_expenses")
    print(f"Загружено расходов: {len(all_rows)}")
    return all_rows


def load_ozon_organic():
    try:
        # id у таблицы нет; первичный ключ — (sale_date, marketplace_code, marketplace_sku)
        all_rows = read_all_by_key("ozon_daily_sku_organic", ("sale_date", "marketplace_code", "marketplace_sku"))
    except RuntimeError:
        raise
    except Exception as e:
        print(
            "Не удалось загрузить ozon_daily_sku_organic. "
            "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
            f"Ошибка: {e}"
        )
        return []

    print(f"Загружено Ozon organic строк: {len(all_rows)}")
    return all_rows


def build_article_map(orders):
    """(площадка, sku) → (артикул, название) по строкам заказов.

    Артикул в витрину приносит только строка заказа: у выкупов и расходов он пуст, поэтому ключ, у
    которого в этот день заказа не было (выкуп приходит через неделю после заказа, расход — когда
    угодно), оставался без артикула — 30,6 % выручки выкупов со строкой, у которой article непустой
    (замер 2026-09-21). Карта берёт артикул по SKU, а не по дню.

    Другие источники проверены и не годятся: в sku_catalog.marketplace_sku лежит product_id, а не SKU;
    у ozon_product_identity SKU заполнен в 108 строках из 16 577. Заказы покрывают 100,00 % выручки.
    Если у SKU в заказах встречалось несколько артикулов — берётся тот, по которому больше создано
    заказов (подтверждённые + отменённые), при равенстве — меньший по алфавиту: выбор не должен
    зависеть от порядка чтения.
    """
    weights = defaultdict(lambda: defaultdict(float))
    names = {}
    for row in orders:
        article = str(row.get("article") or "").strip()
        if not article:
            continue
        key = (row["marketplace_code"], str(row["marketplace_sku"]))
        weights[key][article] += float(row.get("orders_qty") or 0) + float(row.get("cancelled_orders_qty") or 0)
        if row.get("product_name") and (key, article) not in names:
            names[(key, article)] = row["product_name"]
    out = {}
    for key, by_article in weights.items():
        article = sorted(by_article.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out[key] = (article, names.get((key, article)))
    return out


def fill_articles(grouped, article_map):
    """Дописывает article (и название, если пусто) там, где строка-источник его не принесла. Суммы не трогает."""
    filled = 0
    for (kpi_date, marketplace_code, marketplace_sku), row in grouped.items():
        if str(row.get("article") or "").strip():
            continue
        found = article_map.get((marketplace_code, str(marketplace_sku)))
        if not found:
            continue
        row["article"] = found[0]
        if not row.get("product_name") and found[1]:
            row["product_name"] = found[1]
        filled += 1
    return filled


def build_kpi():
    orders = load_orders()
    buyouts = load_buyouts()
    expenses = load_expenses()
    ozon_organic = load_ozon_organic()

    grouped = {}

    unknown_expense_types = defaultdict(float)

    for row in orders:
        key = (
            row["order_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["order_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                row.get("product_name"),
            )

        grouped[key]["orders_qty"] += float(row.get("orders_qty") or 0)
        grouped[key]["orders_amount_seller"] += float(row.get("orders_amount_seller") or 0)

    for row in buyouts:
        key = (
            row["buyout_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["buyout_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                row.get("product_name"),
            )

        grouped[key]["buyouts_qty"] += float(row.get("buyouts_qty") or 0)
        grouped[key]["buyouts_amount_seller"] += float(row.get("buyouts_amount_seller") or 0)

    for row in expenses:
        key = (
            row["expense_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["expense_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                None,
            )

        expense_type = str(row.get("expense_type") or "")
        amount = float(row.get("expense_amount") or 0)

        bucket = EXPENSE_TYPE_BUCKETS.get(expense_type)
        if bucket is None:
            # Молчаливого поглощения больше нет. Прежний else забирал ЛЮБОЙ
            # незнакомый тип в прочие расходы, и так туда вошли unknown_* —
            # витрина их считала, а мы три дня докладывали, что не считает.
            # Незнакомый тип по-прежнему попадает в прочее (деньги не теряем),
            # но теперь он называется вслух.
            unknown_expense_types[expense_type] += amount
            bucket = "other_expenses_amount"
        grouped[key][bucket] += amount

    for row in ozon_organic:
        key = (
            row["sale_date"],
            row["marketplace_code"],
            row["marketplace_sku"],
        )

        if key not in grouped:
            grouped[key] = empty_kpi_row(
                row["sale_date"],
                row["marketplace_code"],
                row["marketplace_sku"],
                row.get("article"),
                row.get("product_name"),
            )

        grouped[key]["ad_orders_qty"] += float(row.get("ad_orders_qty") or 0)
        grouped[key]["ad_orders_revenue"] += float(row.get("ad_orders_revenue") or 0)
        grouped[key]["organic_orders_qty"] += float(row.get("organic_orders_qty") or 0)
        grouped[key]["organic_orders_revenue"] += float(row.get("organic_orders_revenue") or 0)

        if not grouped[key].get("article") and row.get("article"):
            grouped[key]["article"] = row.get("article")
        if not grouped[key].get("product_name") and row.get("product_name"):
            grouped[key]["product_name"] = row.get("product_name")

    filled = fill_articles(grouped, build_article_map(orders))
    with_article = sum(1 for row in grouped.values() if str(row.get("article") or "").strip())
    print(f"Артикул: дописан по карте sku → article из заказов у {filled} строк; с артикулом {with_article} из {len(grouped)}")

    rows = []

    for row in grouped.values():
        orders_qty = row["orders_qty"]
        buyouts_qty = row["buyouts_qty"]

        if orders_qty > 0:
            row["buyout_rate"] = round(buyouts_qty / orders_qty, 4)
        else:
            row["buyout_rate"] = 0

        orders_amount = row["orders_amount_seller"]
        ad_spend = row.get("ad_spend") or 0

        if orders_amount > 0:
            row["ad_share_of_orders"] = round(ad_spend / orders_amount, 4)
            row["ad_share_revenue"] = round((row.get("ad_orders_revenue") or 0) / orders_amount, 4)
        else:
            row["ad_share_of_orders"] = 0
            row["ad_share_revenue"] = 0

        if orders_qty > 0:
            row["ad_share_orders"] = round((row.get("ad_orders_qty") or 0) / orders_qty, 4)
        else:
            row["ad_share_orders"] = 0

        if ad_spend > 0:
            row["roas"] = round(orders_amount / ad_spend, 4)
        else:
            row["roas"] = 0

        rows.append(row)

    if unknown_expense_types:
        print("⚠️  НЕЗНАКОМЫЕ ТИПЫ РАСХОДА (учтены в прочих, но не классифицированы):")
        for name, amount in sorted(unknown_expense_types.items(), key=lambda x: -abs(x[1])):
            print(f"    {name:<28}{amount:>16,.2f}")
        print(f"    итого {sum(unknown_expense_types.values()):,.2f} — "
              "классификация ждёт в docs/ozon_finance_migration.md")

    return rows


def save_kpi(rows):
    if not rows:
        print("Нет KPI для записи")
        return

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        supabase.table("daily_sku_kpi").upsert(
            batch,
            on_conflict="kpi_date,marketplace_code,marketplace_sku"
        ).execute()

    print(f"✅ daily_sku_kpi обновлена: {len(rows)} строк")


if __name__ == "__main__":
    rows = build_kpi()
    save_kpi(rows)
