import argparse
import os
from collections import defaultdict
from datetime import date, timedelta
from dotenv import load_dotenv
from supabase import create_client

from decimal import Decimal

from loaders import keyset, net_retry, stale_keys
from loaders import wb_ads_allocation as alloc
from loaders import wb_buyout_cohort as cohort
from loaders import wb_money_rules as rules

load_dotenv()
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента

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
    # CPO «Все» со ставкой вне 5 % (classify_cpo_rate: 9…11 % → _10, остальное → _other). С 20.10.2026 Ozon
    # ставит 6 % части заказов; тип 54 начислений = CPO 5 % + other + Selected до копейки (56-я, 20 … 23.10),
    # то есть это трата рекламы, а не «прочее». До 56-й тип падал в прочее как незнакомый.
    "advertising_order_10": "ad_spend",
    "advertising_order_other": "ad_spend",
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
        "storage_cost": 0,
        "other_expenses_amount": 0,
    }


PAGE_SIZE = 1000

# Источник, который не удалось прочитать: витрина из него собрана без его строк, и ключи, державшиеся
# только на нём, выглядели бы застрявшими. Пока список не пуст, чистка витрины ничего не удаляет.
SOURCE_READ_FAILURES = []

# Окно чистки витрины — то же, что у загрузчиков начислений и заказов: сегодня и 30 дней назад.
# Ключи старше окна не трогаются никогда: их источники ночь не пересобирает.
STALE_WINDOW_DAYS = 30
STALE_MARKETPLACE = "ozon"
KPI_TABLE = "daily_sku_kpi"


def read_all_by_id(table, columns="*"):
    """Вся таблица страницами: сортировка по id, следующая страница — id больше последнего.

    Было range() без order(): PostgREST без сортировки не обещает порядка, и страницы могут
    прийти с повторами и пропусками (how-we-work, 15 сентября — так сумма из 7 619 строк
    вышла другой). Страница по ключу, а не по смещению, ещё и не дрейфует, если в таблицу
    пишут во время чтения, и стоит одинаково на первой и на трёхсотой странице.
    """
    rows, last_id = [], None
    while True:
        query = supabase.table(table).select(columns).order("id").limit(PAGE_SIZE)
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


PLACEMENT_TABLE = "ozon_placement_by_product_daily"


PLACEMENT_SELECT = "day,sku,warehouse,amount,offer_id"


def read_placement_by_day(sb=None, page=PAGE_SIZE):
    """Хранение Ozon по дням: на каждый день — запрос `day = D` (индекс первичного ключа day, sku, warehouse) и страницы внутри
    дня по (sku, warehouse); каждая страница через net_retry.

    Было (52-я … 56-я): read_all_by_key по всей таблице со смещением. После истории хранения 25.10 (апрель … июнь, +648 032 строки,
    всего 679 919) глубокие страницы со смещением до ~679 000 упирались в statement_timeout 8 с (57014, ночь 26.10, 02:34:41 UTC) —
    витрина собралась без хранения Ozon. Внутри дня строк ≤ ~7 000, смещение не глубже 7 страниц.
    """
    from datetime import date as _date
    sb = sb or supabase
    first = net_retry.execute(sb.table(PLACEMENT_TABLE).select("day").order("day").limit(1), f"{PLACEMENT_TABLE} первый день").data or []
    if not first:
        return []
    last = net_retry.execute(sb.table(PLACEMENT_TABLE).select("day").order("day", desc=True).limit(1), f"{PLACEMENT_TABLE} последний день").data
    day, end = _date.fromisoformat(str(first[0]["day"])[:10]), _date.fromisoformat(str(last[0]["day"])[:10])
    rows, seen = [], set()
    while day <= end:
        start = 0
        while True:
            qb = (sb.table(PLACEMENT_TABLE).select(PLACEMENT_SELECT).eq("day", day.isoformat())
                  .order("sku").order("warehouse").range(start, start + page - 1))
            data = net_retry.execute(qb, f"{PLACEMENT_TABLE} {day} страница {start // page}").data or []
            for r in data:
                key = (str(r["day"])[:10], r["sku"], r["warehouse"])
                if key in seen:
                    raise RuntimeError(f"{PLACEMENT_TABLE}: повтор ключа {key} в чтении по дням — страницы съехали")
                seen.add(key)
            rows.extend(data)
            if len(data) < page:
                break
            start += page
        day += timedelta(days=1)
    return rows


LEDGER_TABLE = "ozon_accrual_daily_types"
STORAGE_ONLY_DAYS = 30      # = STALE_WINDOW_DAYS: ключ «только хранение» витрина заводит не глубже окна ночи
PLACEMENT_TYPE_ID = 46


def load_ledger_type46():
    """{день: тип 46 «Размещение» положительно} из леджера начислений; не прочитался — {} (перенос п. 1 57-й в этот прогон не делается)."""
    try:
        rows = net_retry.execute(supabase.table(LEDGER_TABLE).select("accrual_date,amount").eq("type_id", PLACEMENT_TYPE_ID)
                                 .order("accrual_date").limit(PAGE_SIZE), f"{LEDGER_TABLE} тип 46").data or []
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ {LEDGER_TABLE} (тип 46) не прочитан ({type(exc).__name__}: {str(exc)[:120]}) — размещение остаётся строкой без SKU")
        return {}
    if len(rows) >= PAGE_SIZE:
        raise RuntimeError(f"{LEDGER_TABLE}: тип 46 — {len(rows)} строк, больше одной страницы; читать страницами")
    return {str(r["accrual_date"])[:10]: -Decimal(str(r["amount"])) for r in rows}


def move_placement_into_sku_logistics(grouped, placement, ledger46):
    """57-я §2 п. 1: размещение (тип 46) — не дважды. Ночь пишет его в marketplace_expenses строкой logistics без SKU (у Ozon в
    by-day нет SKU), а витрина с 52-й держит его же по SKU в storage_cost — сумма logistics + storage_cost считала 46 дважды.
    На день, где Σ хранения по SKU = типу 46 леджера до копейки и в строке без SKU логистики не меньше этой суммы, сумма
    переезжает из строки без SKU в logistics_amount строк SKU (по их storage_cost). storage_cost остаётся — «в т. ч. хранение»
    внутри logistics_amount. Итог логистики дня не меняется (сводная витрина и алерт берут logistics_amount); книга и лист месяца
    берут логистику из расходов / леджера и не задеты. День без совпадения не трогается и называется вслух. → счётчики."""
    by_day = defaultdict(lambda: defaultdict(Decimal))
    for row in placement:
        by_day[str(row["day"])[:10]][str(row["sku"])] += -Decimal(str(row.get("amount") or 0))
    stats = {"moved_days": 0, "moved": Decimal(0), "no_ledger": [], "mismatch": [], "short": []}
    for day, skus in sorted(by_day.items()):
        total = sum(skus.values(), Decimal(0)).quantize(Decimal("0.01"))
        ledger = ledger46.get(day)
        if ledger is None:
            stats["no_ledger"].append(day); continue
        if ledger.quantize(Decimal("0.01")) != total:
            stats["mismatch"].append((day, total, ledger)); continue
        present = {sku: amount for sku, amount in skus.items() if (day, "ozon", sku) in grouped}   # ключи «только хранение» старше окна не заводятся
        moved = sum(present.values(), Decimal(0)).quantize(Decimal("0.01"))
        nosku = grouped.get((day, "ozon", ""))
        if nosku is None or Decimal(str(round(nosku["logistics_amount"], 2))) < moved:
            stats["short"].append((day, moved, None if nosku is None else nosku["logistics_amount"])); continue
        nosku["logistics_amount"] = float(Decimal(str(nosku["logistics_amount"])) - moved)
        for sku, amount in present.items():
            grouped[(day, "ozon", sku)]["logistics_amount"] += float(amount)
        stats["moved_days"] += 1; stats["moved"] += moved; stats["kept_nosku"] = stats.get("kept_nosku", Decimal(0)) + (total - moved)
    print(f"Размещение (тип 46) в логистику SKU: дней {stats['moved_days']}, {stats['moved']:,.2f} ₽ перенесено из строки без SKU"
          + (f" (у ключей без строки витрины осталось без SKU {stats['kept_nosku']:,.2f})" if stats.get("kept_nosku") else "")
          + (f"; без леджера {len(stats['no_ledger'])} дн." if stats["no_ledger"] else "")
          + (f"; Σ хранения ≠ типу 46: {', '.join(f'{d} {t} / {l}' for d, t, l in stats['mismatch'][:5])}" if stats["mismatch"] else "")
          + (f"; в строке без SKU меньше хранения: {', '.join(f'{d} {t}' for d, t, _ in stats['short'][:5])}" if stats["short"] else ""))
    return stats


def load_placement():
    """Хранение Ozon по товарам (52-я): строки (day, sku, warehouse) с amount со знаком леджера; таблицы нет или не прочиталась —
    источник помечается непрочитанным (чистка витрины не удаляет), колонка остаётся 0."""
    try:
        rows = read_placement_by_day()
    except Exception as exc:  # noqa: BLE001
        SOURCE_READ_FAILURES.append(PLACEMENT_TABLE)
        print(f"⚠️ {PLACEMENT_TABLE} не прочитана ({type(exc).__name__}: {str(exc)[:120]}) — storage_cost витрины за этот прогон не считается")
        return []
    print(f"Загружено строк хранения: {len(rows)}")
    return rows


# ---- WB: хранение, реклама и «прочее» по артикулу (двадцатая WB, слово владельца 2026-10-23: А1 / Б / В) ----
# Источники по (день, nmId) уже лежат в базе; витрина читала расходы WB только из marketplace_expenses, где строк WB нет (0 за всю
# историю), поэтому у WB-строк ad_spend / other_expenses_amount / storage_cost были нулями. Ключ WB витрины — marketplace_sku = str(nmId).
# Ключ без заказов и выкупов в этот день создаётся (А1, как хранение Ozon 52-й: хранение начисляется каждому SKU на складе каждый день,
# продажи — у немногих; 67,7 % денег хранения и 46 % рекламы лежат на таких ключах). Любой из источников не прочитан → он помечен в
# SOURCE_READ_FAILURES, его колонка в этот прогон не считается, прогон не падает (образец — load_placement).
WB_STORAGE_VIEW = "wb_paid_storage_nm_daily"          # свёртка wb_paid_storage_daily по (день, nmId), sql/20261023_create_wb_cost_views_for_kpi.sql
WB_ADS_TABLE = "wb_ad_spend_daily"                     # списания adv/v1/upd — деньги (только «Баланс»)
WB_ADS_NM_VIEW = "wb_ad_spend_nm_campaign_daily"       # статистика fullstats по (день, кампания, nmId) — только доли
WB_REPORT_TABLE = "wb_sales_report_rows"               # штрафы с nmId, доплаты, «Списание за отзыв … товар <nmId>»
WB_SOURCES_FROM = "2026-01-01"                         # данные WB в базе — с 2026-02-01; раньше в источниках пусто, страница одна и пустая


def load_wb_storage(today=None):
    """[{day, nm_id, warehouse_price, vendor_code}] из вьюхи по (день, nmId): страницы по ключу (day, nm_id) через keyset;
    с НДС, сторно со знаком минус. Вьюхи нет / не прочиталась → источник помечен, [] вслух."""
    day_to = (today or date.today()).isoformat()
    try:
        rows = keyset.read_keyset(supabase, WB_STORAGE_VIEW, "day,nm_id,warehouse_price,vendor_code", WB_SOURCES_FROM, day_to, day_col="day", id_col="nm_id")
    except Exception as exc:  # noqa: BLE001
        SOURCE_READ_FAILURES.append(WB_STORAGE_VIEW)
        print(f"⚠️ {WB_STORAGE_VIEW} не прочитана ({type(exc).__name__}: {str(exc)[:120]}) — storage_cost WB за этот прогон не считается")
        return []
    print(f"Загружено строк хранения WB (день × nmId): {len(rows)}")
    return rows


def load_wb_ads(today=None):
    """{(день, nmId): реклама ₽ с НДС} — списания «Баланс» дня (wb_ad_spend_daily), разнесённые по nmId долями fullstats правилом книги
    (loaders.wb_ads_allocation: доля «Баланса» кампании в день, нормировка к списаниям дня — Σ по дню = списаниям до копейки).
    День со списаниями без статистики не разносится и называется вслух. Любое чтение не удалось → источник помечен, {} вслух."""
    day_to = (today or date.today()).isoformat()
    try:
        upd_rows = read_all_by_key(WB_ADS_TABLE, ("advert_id", "upd_time"))
        nm_rows = keyset.read_keyset(supabase, WB_ADS_NM_VIEW, "day,campaign_nm_key,advert_id,nm_id,sum", WB_SOURCES_FROM, day_to, day_col="day", id_col="campaign_nm_key")   # ключ страницы обязан быть в select
    except Exception as exc:  # noqa: BLE001
        SOURCE_READ_FAILURES.append(WB_ADS_TABLE)
        print(f"⚠️ реклама WB не прочитана ({type(exc).__name__}: {str(exc)[:120]}) — ad_spend WB за этот прогон не считается")
        return {}
    split = alloc.ads_split_of(upd_rows)
    weights = alloc.nm_weights(nm_rows, split["advert_day"], "balance")
    allocated = alloc.nm_ads_by_day_and_nm(split["balance"], weights)
    silent = alloc.days_without_weights(split["balance"], weights)
    print(f"Реклама WB: списаний {len(upd_rows)} строк, «Баланс» {sum(split['balance'].values(), alloc.Z):,.2f} за {len(split['balance'])} дн., "
          f"статистика {len(nm_rows)} строк (день × кампания × nmId); разнесено {sum(allocated.values(), alloc.Z):,.2f} на {len(allocated)} ключей (день, nmId)")
    if silent:
        print(f"⚠️ реклама WB без статистики по номенклатурам — не разнесена ни на один артикул: {len(silent)} дн., "
              f"{sum(silent.values(), alloc.Z):,.2f} ₽: " + ", ".join(f"{d} {v:,.2f}" for d, v in sorted(silent.items())[:10]) + (" …" if len(silent) > 10 else ""))
    return allocated


def load_wb_sku_other(today=None):
    """[(день, nmId, сумма с НДС, вид)] — «прочее» по артикулу из отчёта реализации (loaders.wb_money_rules.sku_other_rows): штрафы и
    удержания вида OTHER минус доплаты у строк с nmId, списания за отзыв — nmId из текста. Чтение по индексу rr_date с фильтром
    SKU_OTHER_FILTER (частичный индекс под тот же предикат). Не прочиталось → источник помечен, [] вслух."""
    day_to = (today or date.today()).isoformat()
    try:
        rows = keyset.read_keyset(supabase, WB_REPORT_TABLE, rules.SKU_OTHER_SELECT, WB_SOURCES_FROM, day_to, day_col="rr_date", id_col="rrd_id",
                                  filters=[("or_", rules.SKU_OTHER_FILTER)])
    except Exception as exc:  # noqa: BLE001
        SOURCE_READ_FAILURES.append(WB_REPORT_TABLE)
        print(f"⚠️ {WB_REPORT_TABLE} (строки «прочего» по артикулу) не прочитана ({type(exc).__name__}: {str(exc)[:120]}) — other_expenses_amount WB за этот прогон не считается")
        return []
    out = rules.sku_other_rows(rows)
    kinds = defaultdict(lambda: [0, alloc.Z])
    for _d, _nm, amount, kind in out:
        kinds[kind][0] += 1; kinds[kind][1] += amount
    print(f"«Прочее» WB по артикулу: строк отчёта {len(rows)}, записей {len(out)}: " + ", ".join(f"{k} {n} / {v:,.2f}" for k, (n, v) in sorted(kinds.items())))
    return out


def load_wb_costs(today=None):
    """Три источника WB одним вызовом (тесты витрины подменяют его): storage — строки вьюхи, ads — {(день, nmId): ₽},
    other — [(день, nmId, ₽, вид)], articles — {nmId: артикул продавца} из таблицы хранения (откат для ключей без заказов)."""
    storage = load_wb_storage(today)
    articles = {}
    for r in storage:
        if r.get("vendor_code") and int(r["nm_id"]) not in articles:
            articles[int(r["nm_id"])] = str(r["vendor_code"]).strip()
    return {"storage": storage, "ads": load_wb_ads(today), "other": load_wb_sku_other(today), "articles": articles}


def fill_wb_articles(grouped, articles):
    """Артикул WB-строкам, которым не хватило карты из заказов: артикул продавца из таблицы хранения (vendor_code). Суммы не трогает."""
    filled = 0
    for (_day, marketplace_code, marketplace_sku), row in grouped.items():
        if marketplace_code != "wb" or str(row.get("article") or "").strip():
            continue
        try:
            article = articles.get(int(marketplace_sku))
        except (TypeError, ValueError):
            article = None
        if article:
            row["article"] = article
            filled += 1
    return filled


def load_ozon_organic():
    try:
        # id у таблицы нет; первичный ключ — (sale_date, marketplace_code, marketplace_sku)
        all_rows = read_all_by_key("ozon_daily_sku_organic", ("sale_date", "marketplace_code", "marketplace_sku"))
    except RuntimeError:
        raise
    except Exception as e:
        SOURCE_READ_FAILURES.append("ozon_daily_sku_organic")
        print(
            "Не удалось загрузить ozon_daily_sku_organic. "
            "Проверьте миграцию sql/20260506_create_ozon_daily_sku_organic.sql. "
            f"Ошибка: {e}"
        )
        return []

    print(f"Загружено Ozon organic строк: {len(all_rows)}")
    return all_rows


# Выкупаемость — к СОЗДАННЫМ заказам (подтверждённые + отменённые), одинаково для Ozon и WB (тридцать четвёртая
# задача, §3). После правила «orders_* = подтверждённые» (Ozon 2026-09-17, WB 2026-09-21) выкупы / orders_qty
# потеряли смысл: у WB 1,005 на плато и 1,843 в окне — выкупы по дате продажи против подтверждённых по дате
# заказа. Созданные — прежняя семантика показателя. ad_share_of_orders, ad_share_orders и roas остаются на
# подтверждённых (orders_qty / orders_amount_seller). Созданные штуки живут в строке только до расчёта —
# колонки в daily_sku_kpi для них нет, в базу поле не уходит.
CREATED_QTY = "_created_orders_qty"


def created_orders_qty(order_row):
    return float(order_row.get("orders_qty") or 0) + float(order_row.get("cancelled_orders_qty") or 0)


def buyout_rate(buyouts_qty, created_qty):
    return round(buyouts_qty / created_qty, 4) if created_qty > 0 else 0


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


def load_wb_cohort():
    """(по дням, по ключам) выкупа WB по когорте для строк WB витрины (WB-11 §3); отказ — ({}, {}) вслух, WB на календарном правиле."""
    try:
        daily, sku = cohort.read_daily_rates(supabase), cohort.read_sku_rates(supabase)
        print(f"Выкуп WB по когорте: дней {len(daily)}, зрелых ключей {len(sku)}")
        return daily, sku
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ выкуп WB по когорте не прочитан ({str(error)[:120]}) — строки WB на календарном правиле")
        return {}, {}


def build_kpi(wb_cohort=None, with_source=False, today=None):
    orders = load_orders()
    buyouts = load_buyouts()
    expenses = load_expenses()
    ozon_organic = load_ozon_organic()
    placement = load_placement()
    wb_costs = load_wb_costs()

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
        grouped[key][CREATED_QTY] = grouped[key].get(CREATED_QTY, 0) + created_orders_qty(row)

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

    # 57-я, слово 2026-10-26: строка витрины «только хранение» (ключ без продаж, расходов и органики) — только за последние
    # STORAGE_ONLY_DAYS дней (окно ночи); старше — хранение идёт только в ключи, у которых есть другой источник. Иначе история хранения
    # с 01.04 удваивала витрину (сухая сборка 26.10: 470 996 → 946 728 строк). Хранение таких ключей остаётся в строке без SKU (п. 1).
    storage_only_from = ((today or date.today()) - timedelta(days=STORAGE_ONLY_DAYS)).isoformat()
    skipped_storage_only = [0, 0.0]
    for row in placement:      # 52-я: хранение по SKU за день = Σ по складам, положительно (в таблице знак леджера)
        key = (str(row["day"])[:10], "ozon", str(row["sku"]))
        if key not in grouped:
            if key[0] < storage_only_from:
                skipped_storage_only[0] += 1; skipped_storage_only[1] += -float(row.get("amount") or 0)
                continue
            grouped[key] = empty_kpi_row(key[0], "ozon", key[2], row.get("offer_id") or "", None)
        grouped[key]["storage_cost"] += -float(row.get("amount") or 0)
    if skipped_storage_only[0]:
        print(f"Хранение без строки витрины (ключ только с хранением старше {storage_only_from}): строк хранения {skipped_storage_only[0]}, "
              f"{skipped_storage_only[1]:,.2f} ₽ — остаются в логистике без SKU")
    if placement:
        move_placement_into_sku_logistics(grouped, placement, load_ledger_type46())

    # 20-я WB: хранение, реклама и «прочее» по артикулу у WB — с НДС и положительные, как остальные колонки расходов витрины
    wb_new_keys = 0

    def wb_row(day, nm):
        nonlocal wb_new_keys
        key = (day, "wb", str(nm))
        if key not in grouped:
            grouped[key] = empty_kpi_row(day, "wb", str(nm), "", None)
            wb_new_keys += 1
        return grouped[key]

    wb_storage_sum = wb_ads_sum = wb_other_sum = Decimal(0)
    for row in wb_costs["storage"]:
        amount = Decimal(str(row.get("warehouse_price") or 0))
        if amount == 0 and (str(row["day"])[:10], "wb", str(row["nm_id"])) not in grouped:
            continue            # сторно съело день целиком: писать нечего, ключ ради нуля не создаём
        wb_row(str(row["day"])[:10], row["nm_id"])["storage_cost"] += float(amount)
        wb_storage_sum += amount
    for (day, nm), amount in wb_costs["ads"].items():
        wb_row(str(day)[:10], nm)["ad_spend"] += float(amount)
        wb_ads_sum += amount
    for day, nm, amount, _kind in wb_costs["other"]:
        wb_row(str(day)[:10], nm)["other_expenses_amount"] += float(amount)
        wb_other_sum += amount
    print(f"WB по артикулу: хранение {wb_storage_sum:,.2f}, реклама {wb_ads_sum:,.2f}, прочее {wb_other_sum:,.2f} (с НДС); "
          f"новых ключей WB без заказов и выкупов в тот день: {wb_new_keys}")

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
    filled_wb = fill_wb_articles(grouped, wb_costs["articles"])
    with_article = sum(1 for row in grouped.values() if str(row.get("article") or "").strip())
    print(f"Артикул: дописан по карте sku → article из заказов у {filled} строк, по артикулу продавца из хранения WB у {filled_wb}; с артикулом {with_article} из {len(grouped)}")

    rows = []
    wb_daily, wb_sku = load_wb_cohort() if wb_cohort is None else wb_cohort
    sources = {"cohort": 0, "cohort_forecast": 0, "calendar": 0}

    for row in grouped.values():
        orders_qty = row["orders_qty"]
        buyouts_qty = row["buyouts_qty"]
        row["buyout_rate"] = buyout_rate(buyouts_qty, row.pop(CREATED_QTY, 0))
        source = "calendar"
        if row["marketplace_code"] == "wb":      # WB-11 §3: когорта дня заказа; ключ без когорты (день раньше 04-01) — как было
            row["buyout_rate"], source = cohort.showcase_rate(row["kpi_date"], wb_daily, row["buyout_rate"], sku=wb_sku, nm=row["marketplace_sku"])
            sources[source] += 1
        if with_source:
            row["buyout_rate_source"] = source

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

    print(f"buyout_rate WB: когорта {sources['cohort']}, прогноз {sources['cohort_forecast']}, календарное {sources['calendar']}"
          + ("" if with_source else " (колонки buyout_rate_source нет — источник не пишется)"))
    if unknown_expense_types:
        print("⚠️  НЕЗНАКОМЫЕ ТИПЫ РАСХОДА (учтены в прочих, но не классифицированы):")
        for name, amount in sorted(unknown_expense_types.items(), key=lambda x: -abs(x[1])):
            print(f"    {name:<28}{amount:>16,.2f}")
        print(f"    итого {sum(unknown_expense_types.values()):,.2f} — "
              "классификация ждёт в docs/ozon_finance_migration.md")

    return rows


# Источник не прочитан → его колонку у строк его площадки в upsert не слать: витрина сохраняет прежнее значение, а не пишет 0.
# 26.10 хранение Ozon не прочиталось (57014), и upsert перезаписал storage_cost нулём у всех строк Ozon (ВБ 2: 20 … 24.10 меньше на
# 4,40 … 13,25 ₽ в день, 25.10 — 0 из 266,88). Органика сюда не входит: от неё зависят производные доли, и она выключена.
SOURCE_COLUMNS = {
    PLACEMENT_TABLE: ("ozon", ("storage_cost",)),
    WB_STORAGE_VIEW: ("wb", ("storage_cost",)),
    WB_ADS_TABLE: ("wb", ("ad_spend",)),
    WB_REPORT_TABLE: ("wb", ("other_expenses_amount",)),
}


def columns_to_keep(failures):
    """{площадка: колонки, которые не слать} по непрочитанным источникам."""
    keep = defaultdict(set)
    for source in failures or ():
        if source in SOURCE_COLUMNS:
            marketplace, columns = SOURCE_COLUMNS[source]
            keep[marketplace].update(columns)
    return keep


def upsert_batches(rows, failures=None, size=500):
    """Пачки для upsert: строки без колонок непрочитанных источников; в пачке у всех строк одинаковый набор полей (PostgREST
    требует одинаковые ключи в массовой вставке). → [(список строк, набор опущенных колонок)]."""
    keep = columns_to_keep(failures)
    groups = defaultdict(list)
    for row in rows:
        omit = keep.get(str(row.get("marketplace_code")), set()) & set(row)
        out = {k: v for k, v in row.items() if k not in omit} if omit else row
        groups[tuple(sorted(out))].append(out)
    batches = []
    for _fields, group in groups.items():
        omitted = keep.get(str(group[0].get("marketplace_code")), set()) - set(group[0])
        for i in range(0, len(group), size):
            batches.append((group[i:i + size], omitted))
    return batches


def save_kpi(rows, failures=None):
    if not rows:
        print("Нет KPI для записи")
        return
    failures = SOURCE_READ_FAILURES if failures is None else failures
    kept = defaultdict(int)
    for batch, omitted in upsert_batches(rows, failures):
        supabase.table("daily_sku_kpi").upsert(
            batch,
            on_conflict="kpi_date,marketplace_code,marketplace_sku"
        ).execute()
        for column in omitted:
            kept[column] += len(batch)
    for column, n in sorted(kept.items()):
        print(f"⚠️ {column}: у {n} строк не отправлена — источник не прочитан, в витрине осталось прежнее значение")
    print(f"✅ daily_sku_kpi обновлена: {len(rows)} строк")


def kpi_key(row):
    return (str(row["kpi_date"]), str(row["marketplace_code"]), str(row["marketplace_sku"]))


def cleanup_stale_kpi_keys(rows, today=None, apply=True, sb=None):
    """Ключи витрины без единой строки-источника — удалить (Ozon, окно 30 дней). Возвращает число удалённых.

    Витрина пишется upsert-ом и ключ, у которого не осталось ни заказа, ни выкупа, ни расхода, ни органики,
    не строит и не удаляет: 2026-09-23 механизм загрузчиков удалил фантомный выкуп 08-25 (171 227,00), а ключ
    витрины остался с той же суммой (тридцать вторая, §1). «Построенные ключи» здесь — ключи, которые этот
    прогон собрал из четырёх таблиц; ключ есть в витрине, среди построенных нет — источника у него нет.

    Защиты — те же, что у загрузчиков (loaders/stale_keys.py): только ключи окна и только Ozon; ничего не
    удалять, если хоть один источник не прочитан (SOURCE_READ_FAILURES) — «полнота сбора» витрины; день окна,
    за который не построено ни одного ключа, не трогается; порог MAX_STALE_ROWS / MAX_STALE_SHARE; список
    печатается целиком до удаления; удаление — после записи витрины.
    """
    sb = sb or supabase
    today = today or date.today()
    day_from = (today - timedelta(days=STALE_WINDOW_DAYS)).isoformat()
    day_to = today.isoformat()
    in_window = [r for r in rows if str(r["marketplace_code"]) == STALE_MARKETPLACE and day_from <= str(r["kpi_date"]) <= day_to]
    built_keys = {kpi_key(r) for r in in_window}
    built_days = {str(r["kpi_date"]) for r in in_window}
    existing = stale_keys.read_window_rows(
        sb, KPI_TABLE,
        "id,kpi_date,marketplace_code,marketplace_sku,orders_amount_seller,buyouts_amount_seller,ad_spend,other_expenses_amount",
        [("eq", "marketplace_code", STALE_MARKETPLACE), ("gte", "kpi_date", day_from), ("lte", "kpi_date", day_to)],
        ["kpi_date", "marketplace_code", "marketplace_sku"])
    window = {"day_from": day_from, "day_to": day_to, "complete": not SOURCE_READ_FAILURES,
              "retries": 0, "failures": len(SOURCE_READ_FAILURES)}
    if SOURCE_READ_FAILURES:
        print(f"Чистка витрины: не прочитаны источники {', '.join(SOURCE_READ_FAILURES)} — ключи без источника не ищу")

    def describe(r):
        return (f"{r['kpi_date']} sku {r['marketplace_sku']} заказы {float(r.get('orders_amount_seller') or 0):,.2f} "
                f"выкупы {float(r.get('buyouts_amount_seller') or 0):,.2f} реклама {float(r.get('ad_spend') or 0):,.2f} "
                f"прочее {float(r.get('other_expenses_amount') or 0):,.2f} (id {r['id']})")

    return stale_keys.cleanup(
        sb, KPI_TABLE, window, existing, built_keys, built_days, kpi_key, lambda r: str(r["kpi_date"]), describe,
        lambda client, stale: stale_keys.delete_by_id(client, KPI_TABLE, stale), apply)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="собрать витрину и план чистки ключей; в БД не писать ни строки")
    args = ap.parse_args()
    rows = build_kpi(with_source=cohort.table_has_column(supabase, "daily_sku_kpi", "buyout_rate_source"))
    if args.dry_run:
        print(f"--dry-run: витрина собрана ({len(rows)} строк), не записываю")
    else:
        save_kpi(rows)
    try:
        cleanup_stale_kpi_keys(rows, apply=not args.dry_run)
    except Exception as exc:  # витрина уже записана; отказ чистки не должен ронять фатальный шаг KPI
        print(f"⚠️ Чистка ключей витрины не выполнена: {type(exc).__name__}: {exc}")
    if args.dry_run:
        print("db_writes = 0")
