"""Измеритель: marketplace_buyouts против /v1/finance/accrual/by-day по каждой дате. Только чтение.

Зачем: история выкупов 03-28 … 08-17 собрана до миграции 09-11 старым
/v3/finance/transaction/list; 07-08 уже показал +9 562,00 против accrual.
Прежде чем переписывать историю, измерить: сколько дат расходится и на сколько.

Запускает владелец руками, не в окне ночного пайплайна (00:15 … 03:15 UTC —
скрипт откажется стартовать). Ничего не пишет в БД: db_writes = 0 всегда.
Сырые начисления каждой даты кладёт в data/accrual_history/<дата>.json
(папка в .gitignore) и при повторном запуске читает оттуда, а не из API —
так один прогон стоит одну выгрузку, а сырьё пригодится для перезаписи,
если её решат делать.

    venv/bin/python3 scripts/measure_buyouts_history_vs_accrual.py                      # 2026-03-28 … 2026-08-17
    venv/bin/python3 scripts/measure_buyouts_history_vs_accrual.py --date-from 2026-07-01 --date-to 2026-07-09
    venv/bin/python3 scripts/measure_buyouts_history_vs_accrual.py --refetch            # снять заново даже при наличии файлов

Обращения: у accrual/by-day 1–3 страницы на дату (июль 1–9 — 10 обращений на
9 дат): на 143 даты ≈ 160–190 обращений, пауза 1,5 с между обращениями, на 429 —
60 с и до трёх попыток, число пауз печатается. Если по ходу окажется больше
250 обращений — скрипт останавливается и говорит об этом.

Сравнение по каждой дате — тем же штатным build_buyout_rows, что пишет таблицу
ночью: строки (date, sku) с buyouts_qty и buyouts_amount_seller. Печатает:
дат сверено, дат с расхождением, суммарное расхождение, таблицу расхождений
по датам (Σ по таблице, Σ по accrual, разница, SKU только в таблице / только
в accrual / с разной суммой).
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import requests  # noqa: E402
from loaders import ozon_finance_accrual as accrual  # noqa: E402

RAW_DIR = os.path.join("data", "accrual_history")
DEFAULT_FROM, DEFAULT_TO = "2026-03-28", "2026-08-17"
PAUSE_SECONDS = 1.5
ANTISPAM_PAUSE_SECONDS = 60
ANTISPAM_MAX_ATTEMPTS = 3
MAX_REQUESTS = 250
NIGHT_WINDOW = ((0, 15), (3, 15))  # UTC


def D(v):
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def in_night_window(now_utc):
    minutes = now_utc.hour * 60 + now_utc.minute
    return NIGHT_WINDOW[0][0] * 60 + NIGHT_WINDOW[0][1] <= minutes <= NIGHT_WINDOW[1][0] * 60 + NIGHT_WINDOW[1][1]


def fetch_day_counted(day, counters):
    """Одна дата с пагинацией; 429 — пауза 60 с, до трёх попыток."""
    out, last_id = [], None
    while True:
        body = {"date": day}
        if last_id:
            body["last_id"] = last_id
        for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
            time.sleep(PAUSE_SECONDS)
            counters["requests"] += 1
            if counters["requests"] > MAX_REQUESTS:
                raise SystemExit(f"обращений уже {counters['requests']} — больше {MAX_REQUESTS}, стоп; спланировать отдельно")
            r = requests.post(f"{accrual.BASE}/v1/finance/accrual/by-day", headers=accrual.headers(), json=body, timeout=180)
            if r.status_code == 429:
                counters["429"] += 1
                print(f"  {day}: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS} ({r.text[:100]})", flush=True)
                time.sleep(ANTISPAM_PAUSE_SECONDS)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"accrual/by-day {day}: HTTP {r.status_code} {r.text[:200]}")
            break
        else:
            raise RuntimeError(f"{day}: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")
        data = r.json() or {}
        batch = data.get("accruals") or []
        out.extend(batch)
        last_id = data.get("last_id")
        if not batch or not last_id:
            return out


def load_or_fetch(day, counters, refetch):
    path = os.path.join(RAW_DIR, f"{day}.json")
    if not refetch and os.path.exists(path):
        return json.load(open(path))["accruals"], "file"
    accruals = fetch_day_counted(day, counters)
    os.makedirs(RAW_DIR, exist_ok=True)
    json.dump({"date": day, "fetched_at": datetime.now(timezone.utc).isoformat(), "accruals": accruals},
              open(path, "w"), ensure_ascii=False)
    return accruals, "api"


def table_rows(sb, day):
    """Строки таблицы за дату — с ORDER BY (правило 2026-09-15)."""
    out, page = [], 0
    while True:
        res = (sb.table("marketplace_buyouts").select("marketplace_sku,buyouts_qty,buyouts_amount_seller")
               .eq("marketplace_code", "ozon").eq("buyout_date", day).order("marketplace_sku")
               .range(page * 1000, page * 1000 + 999).execute())
        out.extend(res.data)
        if len(res.data) < 1000:
            break
        page += 1
    return {str(r["marketplace_sku"]): (D(r["buyouts_qty"]), D(r["buyouts_amount_seller"])) for r in out}


# Таблица хранит суммы как float: 5 373 634,00 лежит как 5 373 633,99. Копейки
# округления — не расхождение; всё, что больше TOLERANCE, — расхождение.
TOLERANCE = Decimal("0.05")


def compare_day(day, table, accruals):
    rows, _ = accrual.build_buyout_rows(accruals)
    src = {r["marketplace_sku"]: (D(r["buyouts_qty"]), D(r["buyouts_amount_seller"])) for r in rows if r["buyout_date"] == day}
    sum_t = sum(v[1] for v in table.values())
    sum_s = sum(v[1] for v in src.values())
    # Нулевые строки старого загрузчика (продажа и возврат в ноль) штатный
    # сборщик не пишет — это не расхождение в деньгах, считаем отдельно.
    zero_t = sorted(k for k in set(table) - set(src) if table[k][1] == 0)
    only_t = sorted(k for k in set(table) - set(src) if table[k][1] != 0)
    only_s = sorted(set(src) - set(table))
    diff_amount = sorted(k for k in set(table) & set(src) if abs(table[k][1] - src[k][1]) > TOLERANCE)
    diff = sum_t - sum_s
    # Расхождение по деньгам дня — критерий. Разное распределение той же суммы по
    # SKU — не расхождение дня: старый API делил сумму операции поровну между
    # позициями, accrual даёт sale_amount по каждому товару; на всех старых датах
    # это даёт «разную сумму» у части SKU при равном итоге. Считаем отдельно.
    # SKU только на одной стороне при равном итоге дня — тоже перераспределение
    # (старый API относил сумму операции к другому SKU); материально только
    # расхождение денег дня.
    material = abs(diff) > TOLERANCE
    return {"day": day, "table_rows": len(table), "src_rows": len(src), "sum_table": sum_t, "sum_src": sum_s,
            "diff": diff, "only_table": only_t, "only_src": only_s, "diff_amount": diff_amount,
            "zero_table": zero_t, "material": material}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date-from", default=DEFAULT_FROM)
    parser.add_argument("--date-to", default=DEFAULT_TO)
    parser.add_argument("--refetch", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if in_night_window(now):
        raise SystemExit(f"сейчас {now:%H:%M} UTC — окно ночного пайплайна 00:15…03:15, запуск отложить")

    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

    d0, d1 = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    counters = {"requests": 0, "429": 0}
    results = []
    t0 = time.monotonic()
    print(f"окно {args.date_from} … {args.date_to}, дат {len(days)}; db_writes = 0")
    for day in days:
        accruals, origin = load_or_fetch(day, counters, args.refetch)
        res = compare_day(day, table_rows(sb, day), accruals)
        results.append(res)
        mark = "  ← РАСХОЖДЕНИЕ" if res["material"] else ""
        print(f"{day} {origin:4} таблица {res['sum_table']:>15,.2f} accrual {res['sum_src']:>15,.2f} Δ {res['diff']:>12,.2f}{mark}", flush=True)

    bad = [r for r in results if r["material"]]
    zero_rows = sum(len(r["zero_table"]) for r in results)
    sku_redistributed = sum(len(r["diff_amount"]) for r in results)
    print(f"\nдат сверено: {len(results)}; дат с расхождением по деньгам дня: {len(bad)}; суммарное расхождение (таблица − accrual): "
          f"{sum(r['diff'] for r in results):,.2f}; нулевых строк в таблице (не деньги) {zero_rows}; "
          f"SKU с иным распределением той же суммы {sku_redistributed}; "
          f"обращений {counters['requests']}, 429 — {counters['429']}, {time.monotonic() - t0:.0f} с")
    if bad:
        print("\nдата        Σ таблица         Σ accrual         разница       SKU только табл / только accrual / разная сумма")
        for r in bad:
            print(f"{r['day']}  {r['sum_table']:>15,.2f}  {r['sum_src']:>15,.2f}  {r['diff']:>12,.2f}   "
                  f"{len(r['only_table'])} / {len(r['only_src'])} / {len(r['diff_amount'])}")
    print("db_writes = 0")


if __name__ == "__main__":
    main()
