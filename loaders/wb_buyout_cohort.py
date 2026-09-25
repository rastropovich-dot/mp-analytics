"""Выкуп WB по когорте дня заказа — для витрины и алерта (WB-10 §2).

Определение одно с коэффициентом листа «Коэффициенты» (scripts/report_finrez_wb.buyout_rate_for_finrez):
    числитель  — продажи отчёта реализации (Продажа − Возврат; ₽ по retailPriceWithDisc, штуки по quantity) по дню
                 ЗАКАЗА — orderDt в московском времени;
    знаменатель — заказы воронки того же дня (wb_funnel_products_daily: orderCount / orderSum по (день, nmId)).
Зрелый день — возраст ≥ MATURE_DAYS (25) суток: когорта дособрана на ≥ 99,9 % ₽ (WB-9 §3). У незрелого дня факт
показан как есть (sold_*), а ставка — прогноз: Σ sold / Σ created по зрелым дням последних FORECAST_DAYS (30) суток,
с пометкой и окном прогноза. Старые поля витрин (buyouts_qty по дню продажи, buyout_rate = выкупы по продаже /
созданные из marketplace_orders) остаются календарными — их читают алерт, отчёты 7/60 дней, Excel и слой решений
(WB-10 §2.1); когорта живёт в своих таблицах (sql/20260926_create_wb_buyout_cohort.sql).

Только чистые функции и читатели; запись — scripts/wb_buyout_cohort_step.py --apply по слову.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from loaders import keyset, stale_keys

MATURE_DAYS = 25                 # = report_finrez_wb.BUYOUT_RATE_MATURE_DAYS (тест сверяет)
FORECAST_DAYS = 30
MSK = timezone(timedelta(hours=3))
REPORT_TABLE = "wb_sales_report_rows"
FUNNEL_TABLE = "wb_funnel_products_daily"
SKU_TABLE = "wb_buyout_cohort_sku_daily"
DAILY_TABLE = "wb_buyout_cohort_daily"
REPORT_SELECT = "rrd_id,rr_date,order_dt,seller_oper_name,nm_id,retail_price_with_disc,quantity"
FUNNEL_SELECT = "day,nm_id,vendor_code,order_count,order_sum"
Z = Decimal(0)


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Z


def ratio(a, b):
    return (Decimal(a) / Decimal(b)).quantize(Decimal("0.0001")) if b else None


def msk_day(ts):
    """Дата момента в московском времени; пусто / не дата — None."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(MSK).date().isoformat()


def sales_by_order_day(report_rows):
    """{(день заказа МСК, nmId): [штуки, ₽]} — Продажа +, Возврат −; строки без orderDt или nmId считаются вслух."""
    out, skipped = defaultdict(lambda: [0, Z]), {"no_order_dt": 0, "no_nm": 0}
    for r in report_rows:
        op = r.get("seller_oper_name")
        if op not in ("Продажа", "Возврат"):
            continue
        day = msk_day(r.get("order_dt"))
        nm = r.get("nm_id")
        if day is None:
            skipped["no_order_dt"] += 1; continue
        if nm in (None, "", 0, "0"):
            skipped["no_nm"] += 1; continue
        sign = 1 if op == "Продажа" else -1
        a = out[(day, int(nm))]
        a[0] += sign * int(r.get("quantity") or 1)
        a[1] += sign * D(r.get("retail_price_with_disc"))
    return dict(out), skipped


def build_sku_rows(funnel_rows, report_rows, today, mature_days=MATURE_DAYS, day_from=None, day_to=None):
    """Строки (день заказа, nmId): заказы воронки и продажи когорты. Ключи — объединение воронки и продаж (продажа без
    строки воронки в тот день — created 0, ставка None, считается в stats). Окно day_from … day_to режет ключи."""
    today = date.fromisoformat(today) if isinstance(today, str) else today
    sales, skipped = sales_by_order_day(report_rows)
    created = {}
    for r in funnel_rows:
        created[(str(r["day"]), int(r["nm_id"]))] = (int(r.get("order_count") or 0), D(r.get("order_sum")), r.get("vendor_code"))
    keys = set(created) | set(sales)
    if day_from:
        keys = {k for k in keys if k[0] >= day_from}
    if day_to:
        keys = {k for k in keys if k[0] <= day_to}
    rows, stats = [], {"skipped": skipped, "sales_without_funnel_row": 0, "sales_without_funnel_sum": Z, "keys": 0, "mature_keys": 0}
    for day, nm in sorted(keys):
        cq, cs, vendor = created.get((day, nm), (0, Z, None))
        sq, ss = sales.get((day, nm), (0, Z))
        if (day, nm) not in created and (sq or ss):
            stats["sales_without_funnel_row"] += 1; stats["sales_without_funnel_sum"] += ss
        age = (today - date.fromisoformat(day)).days
        mature = age >= mature_days
        rows.append({"day": day, "nm_id": nm, "vendor_code": vendor, "created_qty": cq, "created_sum": cs, "sold_qty": sq, "sold_sum": ss,
                     "age_days": age, "mature": mature, "rate_qty": ratio(sq, cq) if mature else None, "rate_sum": ratio(ss, cs) if mature else None})
        stats["keys"] += 1; stats["mature_keys"] += int(mature)
    return rows, stats


def build_daily_rows(sku_rows, today, mature_days=MATURE_DAYS, forecast_days=FORECAST_DAYS):
    """По дням: Σ по всем nmId, факт по зрелым дням, прогноз для незрелых — Σ sold / Σ created по зрелым дням последних
    forecast_days суток (окно — от самого позднего зрелого дня назад)."""
    today = date.fromisoformat(today) if isinstance(today, str) else today
    by_day = defaultdict(lambda: {"created_qty": 0, "created_sum": Z, "sold_qty": 0, "sold_sum": Z})
    for r in sku_rows:
        a = by_day[r["day"]]
        a["created_qty"] += r["created_qty"]; a["created_sum"] += r["created_sum"]; a["sold_qty"] += r["sold_qty"]; a["sold_sum"] += r["sold_sum"]
    mature_days_list = sorted(d for d in by_day if (today - date.fromisoformat(d)).days >= mature_days)
    forecast = {"rate_qty": None, "rate_sum": None, "window": None}
    if mature_days_list:
        last = date.fromisoformat(mature_days_list[-1])
        first = (last - timedelta(days=forecast_days - 1)).isoformat()
        win = [d for d in mature_days_list if d >= first]
        cq = sum(by_day[d]["created_qty"] for d in win); cs = sum((by_day[d]["created_sum"] for d in win), Z)
        sq = sum(by_day[d]["sold_qty"] for d in win); ss = sum((by_day[d]["sold_sum"] for d in win), Z)
        forecast = {"rate_qty": ratio(sq, cq), "rate_sum": ratio(ss, cs), "window": f"{first} … {last.isoformat()}" if win else None}   # календарное окно; зрелых дней в нём {len(win)}
    out = []
    for day in sorted(by_day):
        a = by_day[day]
        age = (today - date.fromisoformat(day)).days
        mature = age >= mature_days
        out.append({"day": day, **a, "age_days": age, "mature": mature,
                    "rate_qty": ratio(a["sold_qty"], a["created_qty"]) if mature else None, "rate_sum": ratio(a["sold_sum"], a["created_sum"]) if mature else None,
                    "forecast_rate_qty": None if mature else forecast["rate_qty"], "forecast_rate_sum": None if mature else forecast["rate_sum"],
                    "forecast_window": None if mature else forecast["window"]})
    return out, forecast


def month_table(daily_rows):
    """По месяцам: Σ created / sold по всем дням и по зрелым; ставка по зрелым дням (₽ и шт); доля зрелых дней."""
    out = defaultdict(lambda: {"days": 0, "mature_days": 0, "created_qty": 0, "created_sum": Z, "sold_qty": 0, "sold_sum": Z,
                               "m_created_qty": 0, "m_created_sum": Z, "m_sold_qty": 0, "m_sold_sum": Z})
    for r in daily_rows:
        m = out[r["day"][:7]]
        m["days"] += 1; m["created_qty"] += r["created_qty"]; m["created_sum"] += r["created_sum"]; m["sold_qty"] += r["sold_qty"]; m["sold_sum"] += r["sold_sum"]
        if r["mature"]:
            m["mature_days"] += 1; m["m_created_qty"] += r["created_qty"]; m["m_created_sum"] += r["created_sum"]; m["m_sold_qty"] += r["sold_qty"]; m["m_sold_sum"] += r["sold_sum"]
    for m in out.values():
        m["rate_qty"] = ratio(m["m_sold_qty"], m["m_created_qty"]); m["rate_sum"] = ratio(m["m_sold_sum"], m["m_created_sum"])
        m["all_rate_sum"] = ratio(m["sold_sum"], m["created_sum"])
    return dict(sorted(out.items()))


# ---------- чтение (только PostgREST, страницы по ключу) ----------

def read_report_rows(sb, day_from, day_to):
    """Продажи и возвраты отчёта с rr_date day_from … day_to (продажа когорты проходит и через 40 дней после заказа —
    брать до сегодня), узкий SELECT; страницы по ключу (rr_date, rrd_id) индексными запросами (loaders.keyset)."""
    return keyset.read_keyset(sb, REPORT_TABLE, REPORT_SELECT, day_from, day_to, day_col="rr_date", id_col="rrd_id",
                              filters=[("in_", "seller_oper_name", ["Продажа", "Возврат"])])


def read_funnel_rows(sb, day_from, day_to):
    """Карточки воронки с заказами за окно (фильтр на сервере: карточки без заказов когорте не нужны)."""
    return stale_keys.read_window_rows(sb, FUNNEL_TABLE, FUNNEL_SELECT,
                                       [("gte", "day", day_from), ("lte", "day", day_to), ("or_", "order_count.gt.0,order_sum.gt.0")], ["day", "nm_id"])


def read_showcase_by_month(sb, day_from, day_to):
    """«Было»: ночное правило — Σ buyouts_qty витрины daily_marketplace_kpi (по дню продажи) / Σ созданных (orders_qty +
    cancelled_orders_qty) из marketplace_orders по дню заказа, помесячно; так считает reports_daily_marketplace_kpi."""
    kpi = stale_keys.read_window_rows(sb, "daily_marketplace_kpi", "id,kpi_date,buyouts_qty,buyout_rate",
                                      [("eq", "marketplace_code", "wb"), ("gte", "kpi_date", day_from), ("lte", "kpi_date", day_to)], ["kpi_date", "id"])
    orders = stale_keys.read_window_rows(sb, "marketplace_orders", "id,order_date,orders_qty,cancelled_orders_qty",
                                         [("eq", "marketplace_code", "wb"), ("gte", "order_date", day_from), ("lte", "order_date", day_to)], ["order_date", "id"])
    out = defaultdict(lambda: {"buyouts_qty": Z, "created_qty": Z})
    for r in kpi:
        out[str(r["kpi_date"])[:7]]["buyouts_qty"] += D(r.get("buyouts_qty"))
    for r in orders:
        out[str(r["order_date"])[:7]]["created_qty"] += D(r.get("orders_qty")) + D(r.get("cancelled_orders_qty"))
    for m in out.values():
        m["rate"] = ratio(m["buyouts_qty"], m["created_qty"])
    return dict(sorted(out.items()))
