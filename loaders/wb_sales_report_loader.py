#!/usr/bin/env python3
"""Отчёт реализации WB → wb_sales_report_rows. Ночной шаг «WB: отчёт реализации», нефатальный.

ИСТОЧНИК. POST finance-api /api/finance/v1/sales-reports/detailed
(spec/wb/13-finances.yaml): тело {dateFrom, dateTo, limit ≤ 100 000, rrdId, period},
ответ — список строк операций (rrdId), 204 — данных нет. Лимит — 1 запрос в
минуту, данные с 2024-01-29.

ПОЧЕМУ period=daily. Незакрытая неделя отдаётся только ежедневными отчётами
(день D появляется D+1: reportId 6338020260922, createDate 2026-09-23); у
закрытой недели ежедневные и недельный отчёты — одни и те же rrdId и значения
(5 979 из 5 979 строк 09-01…09-07, проверено 2026-09-23). Значит ходить всегда
с daily: rrd_id стабилен, upsert идемпотентен, и текущая неделя не ждёт понедельника.

ЗАДНИМ ЧИСЛОМ. Повторный съём закрытой недели через сутки — те же строки, поле в
поле (один замер, 09-22 → 09-23). Гарантии нет, поэтому окно ночи — последние
DEFAULT_DAYS_BACK дней целиком, upsert по rrd_id; а строки окна, которых в свежем
ПОЛНОМ ответе нет, снимаются тем же механизмом, что у Ozon (loaders/stale_keys.py):
только при полном сборе, с порогом, списком до удаления и после записи.

ОБРАЩЕНИЯ. Окно 21 день ≈ 18 тыс. строк — одна страница на 100 000, то есть 1
обращение за ночь (следующая страница нужна, только если пришло ровно limit
строк). 429 — пауза 65 с, до 3 попыток, дальше отказ с причиной; таймаут и сеть —
так же. Числа печатаются в конце.

ДЕНЬГИ. Как в ответе: строки → Decimal → строкой в JSON, без float; в таблице
numeric без точности. Отсутствующее поле — null, не ноль.

    python3 loaders/wb_sales_report_loader.py --dry-run          ничего не пишет, db_writes = 0
    python3 loaders/wb_sales_report_loader.py --days-back 21
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv

try:
    from loaders import stale_keys
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import stale_keys

load_dotenv()

WB_API_KEY = os.getenv("WB_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
TABLE = "wb_sales_report_rows"
PAGE_LIMIT = 100_000
SLEEP_SECONDS = 65            # лимит метода — 1 запрос в минуту
MAX_ATTEMPTS = 3              # 429 / таймаут / сеть: пауза SLEEP_SECONDS, затем отказ с причиной
DEFAULT_DAYS_BACK = 21        # три недели: закрытые отчёты не менялись за сутки, но гарантии нет
TIMEOUT = 300                 # ответ за неделю — 12 МБ

# Поле ответа → колонка таблицы, тип (d — дата, t — момент, i — целое, n — numeric, s — строка).
FIELDS = (
    ("reportId", "report_id", "i"), ("dateFrom", "report_date_from", "d"), ("dateTo", "report_date_to", "d"),
    ("createDate", "report_create_date", "d"), ("rrDate", "rr_date", "d"), ("saleDt", "sale_dt", "t"),
    ("orderDt", "order_dt", "t"), ("nmId", "nm_id", "i"), ("vendorCode", "vendor_code", "s"),
    ("techSize", "tech_size", "s"), ("srid", "srid", "s"), ("shkId", "shk_id", "i"), ("docTypeName", "doc_type", "s"),
    ("sellerOperName", "seller_oper_name", "s"), ("bonusTypeName", "bonus_type_name", "s"),
    ("officeName", "office_name", "s"), ("quantity", "quantity", "i"),
    ("retailPriceWithDisc", "retail_price_with_disc", "n"), ("retailAmount", "retail_amount", "n"),
    ("forPay", "for_pay", "n"), ("ppvzSalesCommission", "ppvz_sales_commission", "n"),
    ("commissionPercent", "commission_percent", "n"), ("ppvzReward", "ppvz_reward", "n"),
    ("rebillLogisticCost", "rebill_logistic_cost", "n"), ("deliveryService", "delivery_service", "n"),
    ("deliveryAmount", "delivery_amount", "i"), ("returnAmount", "return_amount", "i"),
    ("acquiringFee", "acquiring_fee", "n"), ("acquiringPercent", "acquiring_percent", "n"),
    ("paidStorage", "paid_storage", "n"), ("penalty", "penalty", "n"), ("additionalPayment", "additional_payment", "n"),
    ("deduction", "deduction", "n"), ("paidAcceptance", "paid_acceptance", "n"), ("vw", "vw", "n"),
    ("vwNds", "vw_nds", "n"), ("cashbackDiscount", "cashback_discount", "n"), ("cashbackAmount", "cashback_amount", "n"),
    ("cashbackCommissionChange", "cashback_commission_change", "n"),
)
REQUIRED = ("rrdId", "reportId", "dateFrom", "dateTo", "rrDate", "sellerOperName")
TRANSIENT = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)


def _client():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _decimal_text(value, field):
    """Число из ответа — строкой в JSON без потери знаков. Не число — отказ."""
    if value is None or value == "":
        return None
    try:
        return str(Decimal(str(value)))
    except InvalidOperation:
        raise RuntimeError(f"WB sales report: поле {field} не число: {value!r}")


def build_row(item, observed_at):
    """Строка таблицы из строки ответа. Обязательные поля отсутствуют — отказ, не молчаливый null."""
    for field in REQUIRED:
        if item.get(field) in (None, ""):
            raise RuntimeError(f"WB sales report: в строке нет {field}: rrdId={item.get('rrdId')}")
    row = {"rrd_id": int(item["rrdId"]), "report_period": "daily", "observed_at": observed_at}
    for src, dst, kind in FIELDS:
        value = item.get(src)
        if value in (None, ""):
            row[dst] = None
        elif kind == "i":
            row[dst] = int(value)
        elif kind == "n":
            row[dst] = _decimal_text(value, src)
        elif kind == "d":
            row[dst] = str(value)[:10]
        elif kind == "t":
            row[dst] = str(value)
        else:
            row[dst] = str(value)
    return row


def request_page(body, counters, sleep_fn=time.sleep):
    """Одна страница: (status, items). 429 / таймаут / сеть — пауза и повтор, MAX_ATTEMPTS раз."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(URL, headers={"Authorization": WB_API_KEY, "Content-Type": "application/json"},
                                 json=body, timeout=TIMEOUT)
        except TRANSIENT as error:
            counters["transient"] += 1
            print(f"WB sales report: сеть — {type(error).__name__} (попытка {attempt}/{MAX_ATTEMPTS})", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB sales report: сетевой отказ не изжит за {MAX_ATTEMPTS} попыток") from error
            sleep_fn(SLEEP_SECONDS)
            continue
        counters["requests"] += 1
        if resp.status_code == 429:
            counters["429"] += 1
            print(f"WB sales report: 429 (попытка {attempt}/{MAX_ATTEMPTS}), пауза {SLEEP_SECONDS} с", flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"WB sales report: 429 не изжит за {MAX_ATTEMPTS} попыток")
            sleep_fn(SLEEP_SECONDS)
            continue
        if resp.status_code == 204:
            return 204, []
        if resp.status_code != 200:
            raise RuntimeError(f"WB sales report: HTTP {resp.status_code}: {resp.text[:300]}")
        return 200, json.loads(resp.content.decode("utf-8"), parse_float=Decimal)
    raise RuntimeError("WB sales report: недостижимо")


def fetch_period(date_from, date_to, counters, period="daily", sleep_fn=time.sleep, page_limit=PAGE_LIMIT):
    """Все строки периода: страницы по rrdId, пауза между страницами под лимит 1/мин."""
    items, rrd_id = [], 0
    while True:
        if counters["requests"]:
            sleep_fn(SLEEP_SECONDS)
        status, page = request_page({"dateFrom": date_from, "dateTo": date_to, "limit": page_limit,
                                     "rrdId": rrd_id, "period": period}, counters, sleep_fn)
        items.extend(page)
        print(f"WB sales report {date_from}…{date_to}: HTTP {status}, строк {len(page)} (всего {len(items)})", flush=True)
        if status == 204 or len(page) < page_limit:
            return items
        rrd_id = int(page[-1]["rrdId"])


def check_items(items, date_from, date_to):
    """Дубли rrdId и строки вне периода — отказ: на такой ответ ни писать, ни чистить нельзя."""
    ids = [int(x["rrdId"]) for x in items]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"WB sales report: {len(ids) - len(set(ids))} повторов rrdId в ответе")
    outside = sorted({str(x.get("rrDate"))[:10] for x in items if not (date_from <= str(x.get("rrDate"))[:10] <= date_to)})
    if outside:
        raise RuntimeError(f"WB sales report: строки вне периода {date_from}…{date_to}: {outside[:5]}")


def upsert_rows(sb, rows, batch=500):
    written = 0
    for i in range(0, len(rows), batch):
        sb.table(TABLE).upsert(rows[i:i + batch], on_conflict="rrd_id").execute()
        written += len(rows[i:i + batch])
    return written


def cleanup_stale(sb, date_from, date_to, rows, complete, apply):
    """Строки окна, которых в свежем полном ответе нет, — снять (stale_keys, со всеми защитами)."""
    try:
        existing = stale_keys.read_window_rows(
            sb, TABLE, "rrd_id,rr_date,seller_oper_name,retail_price_with_disc",
            [("gte", "rr_date", date_from), ("lte", "rr_date", date_to)], ["rr_date", "rrd_id"])
    except Exception as error:  # таблицы ещё нет (миграция не применена) — сказать вслух, не падать в dry-run
        print(f"чистка застрявших пропущена: не прочитать {TABLE} — {str(error)[:200]}", flush=True)
        if not apply:
            return 0
        raise
    built_keys = {r["rrd_id"] for r in rows}
    built_days = {r["rr_date"] for r in rows}
    window = {"day_from": date_from, "day_to": date_to, "complete": complete, "retries": 0, "failures": 0 if complete else 1}
    return stale_keys.cleanup(
        sb, TABLE, window, existing, built_keys, built_days,
        lambda r: r["rrd_id"], lambda r: str(r["rr_date"]),
        lambda r: f"rrd_id {r['rrd_id']} {r['rr_date']} {r['seller_oper_name']} {r.get('retail_price_with_disc')}",
        lambda sb_, stale: _delete_by_rrd_id(sb_, stale), apply)


def _delete_by_rrd_id(sb, rows, batch=200):
    ids = [r["rrd_id"] for r in rows]
    n = 0
    for i in range(0, len(ids), batch):
        res = sb.table(TABLE).delete().in_("rrd_id", ids[i:i + batch]).execute()
        n += len(res.data or [])
    return n


def summarize(rows):
    """Итог для лога: дни, операции, оборот нетто (Продажа − Возврат)."""
    from collections import Counter
    days = sorted({r["rr_date"] for r in rows})
    opers = Counter(r["seller_oper_name"] for r in rows)
    turnover = Decimal(0)
    for r in rows:
        if r["seller_oper_name"] in ("Продажа", "Возврат") and r["retail_price_with_disc"] is not None:
            turnover += (-1 if r["seller_oper_name"] == "Возврат" else 1) * Decimal(r["retail_price_with_disc"])
    return days, opers, turnover


def run(sb, days_back=DEFAULT_DAYS_BACK, today=None, dry_run=False, sleep_fn=time.sleep):
    today = today or date.today()
    date_to = (today - timedelta(days=1)).isoformat()          # день D появляется D+1
    date_from = (today - timedelta(days=days_back)).isoformat()
    observed_at = datetime.now(timezone.utc).isoformat()
    counters = {"requests": 0, "429": 0, "transient": 0}
    print(f"WB отчёт реализации: окно {date_from} … {date_to} (period=daily), {'dry-run, db_writes = 0' if dry_run else 'запись'}", flush=True)

    complete = True
    try:
        items = fetch_period(date_from, date_to, counters, sleep_fn=sleep_fn)
        check_items(items, date_from, date_to)
    except RuntimeError as error:
        print(f"❌ {error}", flush=True)
        print(f"обращений {counters['requests']}, 429 — {counters['429']}, сетевых отказов {counters['transient']}", flush=True)
        raise

    rows = [build_row(x, observed_at) for x in items]
    days, opers, turnover = summarize(rows)
    print(f"строк {len(rows)}, дней {len(days)}" + (f" ({days[0]} … {days[-1]})" if days else "") +
          f", оборот нетто (Продажа − Возврат) {turnover:,.2f}; операции: " + ", ".join(f"{k} {v}" for k, v in opers.most_common()), flush=True)
    print(f"обращений {counters['requests']}, 429 — {counters['429']}, сетевых отказов {counters['transient']}", flush=True)

    if dry_run:
        print("dry-run: ничего не пишу, db_writes = 0", flush=True)
        cleanup_stale(sb, date_from, date_to, rows, complete, apply=False)
        return {"rows": len(rows), "written": 0, "deleted": 0, "requests": counters["requests"], "429": counters["429"]}

    written = upsert_rows(sb, rows) if rows else 0
    print(f"✅ {TABLE}: upsert {written} строк", flush=True)
    # Чистка — ПОСЛЕ записи и только при полном сборе (loaders/stale_keys.py).
    deleted = cleanup_stale(sb, date_from, date_to, rows, complete, apply=True)
    return {"rows": len(rows), "written": written, "deleted": deleted, "requests": counters["requests"], "429": counters["429"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="WB sales report (finance-api) → wb_sales_report_rows")
    ap.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, db_writes = 0")
    args = ap.parse_args(argv)
    if not WB_API_KEY:
        raise SystemExit("WB_API_KEY не задан")
    run(_client(), days_back=args.days_back, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
