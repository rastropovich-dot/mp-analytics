#!/usr/bin/env python3
"""Себестоимость по курсу 1С — приёмка «было → стало» по месяцам (сорок восьмая §3). Только чтение; db_writes = 0; Ozon / WB — 0.

    venv/bin/python3 scripts/cost_by_rate_check.py [--date-from 2026-04-01 --date-to 2026-10-07] \\
        [--rates-csv data/ll_rates/ll_rates_1c_history.csv | таблица metal_rates_1c] \\
        [--book data/reports/finrez_2026-04_2026-10.xlsx] [--manual data/manual_report_september_20260922_v2.xlsx] [--index 1.150 --index-from 2026-09-01]

Было — правило читателей до сорок восьмой: СС снимка по дате продажи (loaders/unit_cost_history.lookup) × индекс COST_INDEX с даты
действия (книга «Фин рез» 10-07: 1,150 с 09-01). Стало — та же СС снимка × (1 + k × Δ курса 1С металла с даты снимка до дня продажи)
(unit_cost_history.rate_adjust, k — COST_RATE_K профиля). Себестоимость = штуки (buyouts_units, иначе позиции) × СС. Фин. рез. «стало» =
фин. рез. книги − Δ себестоимости (остальные статьи от СС не зависят). Против ручного отчёта владельца — его «Итого» листов «Ozon - <месяц>».
Разрез «снимок 20.05 / другие снимки» — чтобы сверить с сорок седьмой §3, где «стало» считалось только от файла 20.05.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import cabinet  # noqa: E402

_PROFILE = cabinet.profile()   # каталоги кабинета; guard assert_env — в main перед клиентом

D, Z = Decimal, Decimal(0)
RU = {"04": "апрель", "05": "май", "06": "июнь", "07": "июль", "08": "август", "09": "сентябрь", "10": "октябрь"}


def index_for(day, index, index_from):
    return index if (index is not None and index_from and day >= index_from) else D(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date-from", default="2026-04-01")
    ap.add_argument("--date-to", default=None, help="по умолчанию — вчера")
    ap.add_argument("--rates-csv", default=None, help="курсы из csv (с ll_rates.csv рядом) вместо таблицы metal_rates_1c")
    ap.add_argument("--book", default=cabinet.data_path("reports", "finrez_2026-04_2026-10.xlsx", prof=_PROFILE))
    ap.add_argument("--manual", default=cabinet.data_path("manual_report_september_20260922_v2.xlsx", prof=_PROFILE))
    ap.add_argument("--index", default="1.150", help="индекс СС книги «было» (пусто — без индекса)")
    ap.add_argument("--index-from", default="2026-09-01")
    args = ap.parse_args(argv)
    from datetime import date, timedelta
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(os.path.join(ROOT, ".env"))
    cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
    import report_ozon_month as rep
    import cost_by_date_check as cbc
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    date_to = args.date_to or (date.today() - timedelta(days=1)).isoformat()
    index = D(args.index) if args.index else None
    rates = rep.rates_from_arg(args.rates_csv)
    sku2art, unit_cost, found, asked, _orders = rep.load_costs(sb, rep.SNAP, rates)
    hist = unit_cost.history
    print(f"кабинет {_PROFILE.DISPLAY_NAME}; снимки {hist.dates}; артикулов с СС {found} из {asked}; k: пробы {sorted(hist.k_table)}")
    print(f"курс 1С: {hist.rates.describe() if hist.rates else 'НЕТ'}" + (" (из csv)" if args.rates_csv else " (таблица metal_rates_1c)"))
    buyouts = rep.fetch(sb, "marketplace_buyouts", "id,buyout_date,marketplace_sku,buyouts_qty,buyouts_units",
                        [("eq", "marketplace_code", "ozon"), ("gte", "buyout_date", args.date_from), ("lte", "buyout_date", date_to)],
                        ["buyout_date", "marketplace_sku"])
    print(f"выкупов Ozon {args.date_from} … {date_to}: строк {len(buyouts)}")
    last = hist.dates[-1] if hist.dates else None
    mon = defaultdict(lambda: defaultdict(Decimal))
    cnt = defaultdict(Counter)
    for r in buyouts:
        day = str(r["buyout_date"]); mm = day[:7]
        periods = [mm] + (["2026-09 (1–21)"] if "2026-09-01" <= day <= "2026-09-21" else [])
        units = D(str(r["buyouts_units"])) if r.get("buyouts_units") is not None else D(str(r["buyouts_qty"]))
        for P in periods:
            cnt[P]["rows"] += 1
        art = sku2art.get(str(r["marketplace_sku"]))
        if not art:
            for P in periods:
                cnt[P]["no_art"] += 1
            continue
        adjusted, base, snap, kind, info = hist.lookup_by_rate(art.lower(), day)
        if adjusted is None:
            for P in periods:
                cnt[P]["no_cost"] += 1
            continue
        was = base * index_for(day, index, args.index_from)
        bucket = "snap_last" if snap == last else "snap_other"
        variant = "F" if art[:1].upper() == "F" else "ST"      # сорок седьмая §3 знала составляющие только у основного варианта F
        for P in periods:
            m = mon[P]
            m["base"] += units * base; m["was"] += units * was; m["new"] += units * adjusted
            m[f"{bucket}_base"] += units * base; m[f"{bucket}_new"] += units * adjusted
            m[f"{bucket}_{variant}_base"] += units * base; m[f"{bucket}_{variant}_new"] += units * adjusted
            cnt[P][bucket] += 1; cnt[P][f"{bucket}_{variant}"] += 1; cnt[P][f"reason_{info['reason']}"] += 1; cnt[P][f"kind_{kind}"] += 1
    book = cbc.book_by_month(os.path.expanduser(args.book)) if os.path.exists(os.path.expanduser(args.book)) else {}
    manual = cbc.manual_by_month(os.path.expanduser(args.manual)) if os.path.exists(os.path.expanduser(args.manual)) else {}
    print(f"\nкнига: {args.book if book else 'нет'} ({len(book)} мес.); ручной отчёт: {args.manual if manual else 'нет'} ({len(manual)} мес.); индекс «было» {index} с {args.index_from}")
    print("\nпериод | строк | СС снимка (без индекса) | СС было (книга: снимок × индекс) | СС стало (по курсу) | Δ стало − было | Δ % к снимку | "
          "фин. рез. книги → стало | СС владельца: расх. было → стало | фин. рез. владельца: расх. было → стало")
    for P in sorted(mon):
        m = mon[P]; m2 = P[5:7]
        b_cogs, b_fin = book.get(m2, (None, None)) if "(" not in P else (None, None)
        m_cogs, m_fin = manual.get(m2, (None, None)) if (m2 != "09" or "(" in P) else (None, None)
        fin_new = (b_fin + (b_cogs - m["new"])) if b_fin is not None else None
        line = (f"{P} | {cnt[P]['rows']} | {m['base']:,.2f} | {m['was']:,.2f} | {m['new']:,.2f} | {m['new'] - m['was']:+,.2f} | "
                f"{(m['new'] / m['base'] - 1) * 100 if m['base'] else Z:+.2f} %")
        line += f" | {b_fin:,.2f} → {fin_new:,.2f}" if b_fin is not None else " | —"
        line += f" | {m_cogs:,.2f}: {m['was'] - m_cogs:,.2f} → {m['new'] - m_cogs:,.2f}" if m_cogs is not None else " | —"
        line += f" | {m_fin:,.2f}: {b_fin - m_fin:,.2f} → {fin_new - m_fin:,.2f}" if (m_fin is not None and b_fin is not None) else " | —"
        print(line)
        if b_cogs is not None and abs(b_cogs - m["was"]) > D("0.5"):
            print(f"      книга: СС {b_cogs:,.2f} ≠ «было» {m['was']:,.2f} (разница {b_cogs - m['was']:+,.2f}) — объяснить: окно / данные книги")
        print(f"      снимок {last}: строк {cnt[P]['snap_last']}, СС снимка {m['snap_last_base']:,.2f} → по курсу {m['snap_last_new']:,.2f}; "
              f"другие снимки: строк {cnt[P]['snap_other']}, {m['snap_other_base']:,.2f} → {m['snap_other_new']:,.2f}")
        print(f"      в т. ч. снимок {last} по вариантам: F — строк {cnt[P]['snap_last_F']}, {m['snap_last_F_base']:,.2f} → {m['snap_last_F_new']:,.2f}; "
              f"S/T — строк {cnt[P]['snap_last_ST']}, {m['snap_last_ST_base']:,.2f} → {m['snap_last_ST_new']:,.2f}")
        print("      причины: " + ", ".join(f"{k[7:]} {v}" for k, v in sorted(cnt[P].items()) if k.startswith("reason_"))
              + "; подбор: " + ", ".join(f"{k[5:]} {v}" for k, v in sorted(cnt[P].items()) if k.startswith("kind_"))
              + f"; без артикула {cnt[P]['no_art']}, без СС {cnt[P]['no_cost']}")
    print("\n" + hist.rate_note())
    print("db_writes = 0; обращений к Ozon / WB — 0")


if __name__ == "__main__":
    main()
