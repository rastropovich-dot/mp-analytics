#!/usr/bin/env python3
"""Соинвест Ozon — проба формулы владельца по сырью на диске. Только чтение; в API не ходит (файлы уже сняты).

    venv/bin/python3 scripts/ozon_coinvest_probe.py --date-from 2026-09-01 --date-to 2026-09-22 [--article-day 2026-09-10]

Владелец (справочник его BI, письмо 09-23): «СПП заказы, %» Ozon = (Σ «Ваша цена» − Σ «Оплачено покупателем») /
Σ «Ваша цена» по двум отчётам ЛК — FBS «Заказы с моих складов» и FBO «Заказы со складов Ozon», период по столбцу
«Принят в обработку» (ЛК отдаёт его по Гринвичу). Его колонка G листа «Заказы» (data/manual_report_september_20260922_v2.xlsx)
— по UTC-суткам заказа.

Источники (все на диске):
  data/ozon_report_postings/<схема>_<от>_<до>.csv   отчёты ЛК через /v1/report/postings/create (сняты 2026-09-23 20:36 UTC): колонки
                                                     «Принят в обработку», «Статус», «SKU», «Артикул», «Предельная цена»,
                                                     «Оплачено покупателем», «Код валюты покупателя», «Количество», «Сумма отправления»
  data/postings_raw/history_fbs_<от>_<до>.json       ночной список FBS (/v4) — financial_data.products[].customer_price, для сверки с CSV
  data/accrual_history/<день>.json                   by-day: posting.products[].commission — seller_price / sale_price / bonus / coinvestment
                                                     по продажам (дню начисления), для сверки трёх определений на отправлениях

Что печатает: таблица по UTC-дню «Принят в обработку» — FBS + FBO вместе и по схемам, все созданные и без отменённых,
(ΣS − ΣP) / ΣS против G; та же таблица по артикулу за один день; сверка FBS customer_price = CSV «Оплачено покупателем»
по номеру отправления; 20 отправлений с продажей — sale_price by-day / CSV / customer_price поимённо; валюты покупателя.
Ничего не пишет.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "data", "ozon_report_postings")
RAW_DIR = os.path.join(ROOT, "data", "postings_raw")
ACCRUAL_DIR = os.path.join(ROOT, "data", "accrual_history")

OWNER_G = {f"2026-09-{d:02d}": Decimal(g) for d, g in enumerate(
    ["0.52", "0.54", "0.53", "0.55", "0.54", "0.53", "0.52", "0.53", "0.57", "0.54", "0.53", "0.56", "0.56", "0.56", "0.56", "0.57",
     "0.56", "0.57", "0.55", "0.56", "0.55", "0.53"], start=1)}
Z = Decimal(0)
CANCELLED_STATUS = "Отменён"


def money(v):
    if isinstance(v, dict):
        v = v.get("amount")
    if v in (None, ""):
        return Z
    try:
        return Decimal(str(v).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return Z


def read_report(path):
    """Строки отчёта ЛК (CSV с BOM, разделитель ;). Возвращает список словарей по заголовку."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def load_reports(date_from, date_to):
    rows = []
    for scheme in ("fbs", "fbo"):
        path = os.path.join(REPORTS_DIR, f"{scheme}_{date_from}_{date_to}.csv")
        if not os.path.exists(path):
            raise SystemExit(f"нет файла {path} — снять отчётом /v1/report/postings/create (см. docstring)")
        for r in read_report(path):
            rows.append({"scheme": scheme, "posting": r.get("Номер отправления", ""), "processed_at": r.get("Принят в обработку", ""),
                         "status": r.get("Статус", ""), "sku": r.get("SKU", ""), "article": r.get("Артикул", ""),
                         "price": money(r.get("Предельная цена")), "paid": money(r.get("Оплачено покупателем")),
                         "currency": r.get("Код валюты покупателя", ""), "qty": money(r.get("Количество")) or Decimal(1),
                         "posting_sum": money(r.get("Сумма отправления")), "buyout": r.get("Выкуп товара", "")})
    return rows


def utc_day(processed_at):
    return processed_at[:10]


def share_table(rows, days, keep):
    """{день: (ΣS, ΣP, n)} — S = Предельная цена × количество, P = Оплачено покупателем × количество."""
    out = defaultdict(lambda: [Z, Z, 0])
    for r in rows:
        if not keep(r):
            continue
        d = utc_day(r["processed_at"])
        if d not in days:
            continue
        a = out[d]
        a[0] += r["price"] * r["qty"]
        a[1] += r["paid"] * r["qty"]
        a[2] += 1
    return out


def ratio(s, p):
    return (s - p) / s if s else None


def fmt(x):
    return "   —  " if x is None else f"{x:.4f}"


def is_standard(r):
    """Основная площадка — артикул на F (как листы площадок генератора: F / S / T)."""
    return r["article"].strip().upper().startswith("F")


def print_day_table(rows, days, title):
    print(f"\n{title}")
    variants = (("все, FBS+FBO", lambda r: True), ("без отменённых", lambda r: r["status"] != CANCELLED_STATUS),
                ("только FBS", lambda r: r["scheme"] == "fbs"), ("только FBO", lambda r: r["scheme"] == "fbo"),
                ("только Standard (F)", is_standard))
    tables = [share_table(rows, days, keep) for _n, keep in variants]
    print(f"{'день':11}{'n':>6} " + "".join(f"{n:>17}" for n, _k in variants) + f"{'G его':>8}  Δ(все)  Δ(без отм.)")
    hits_all = hits_noc = 0
    for d in days:
        vals = [ratio(t[d][0], t[d][1]) if d in t else None for t in tables]
        g = OWNER_G.get(d)
        d_all = (vals[0] - g) if (vals[0] is not None and g is not None) else None
        d_noc = (vals[1] - g) if (vals[1] is not None and g is not None) else None
        hits_all += 1 if d_all is not None and abs(d_all) <= Decimal("0.01") else 0
        hits_noc += 1 if d_noc is not None and abs(d_noc) <= Decimal("0.01") else 0
        print(f"{d:11}{tables[0][d][2] if d in tables[0] else 0:>6} " + "".join(f"{fmt(v):>17}" for v in vals)
              + f"{('%.2f' % g) if g is not None else '—':>8}  {fmt(d_all):>6}  {fmt(d_noc):>6}")
    S = sum(t[0] for t in tables[0].values()); P = sum(t[1] for t in tables[0].values())
    S2 = sum(t[0] for t in tables[1].values()); P2 = sum(t[1] for t in tables[1].values())
    std = tables[4]
    std_hits = sum(1 for d in days if d in std and d in OWNER_G and std[d][0] and abs(ratio(std[d][0], std[d][1]) - OWNER_G[d]) <= Decimal("0.01"))
    print(f"итого: все {fmt(ratio(S, P))} (ΣS {S:,.2f}, ΣP {P:,.2f}); без отменённых {fmt(ratio(S2, P2))}; "
          f"дней с |Δ| ≤ 0,01: все {hits_all} из {len(days)}, без отменённых {hits_noc} из {len(days)}, только Standard (F) {std_hits} из {len(days)}")
    print("НАЙДЕНО 2026-09-23: G владельца = только Standard (артикулы на F), все созданные заказы вместе с отменёнными, «Предельная цена» × количество "
          "и «Оплачено покупателем» × количество по UTC-дню «Принят в обработку» — 20 дней из 22 в пределах 0,01 (09-03 +0,011, 09-22 +0,018), "
          "смещение +0,0009; граница UTC и МСК на этих числах неразличимы (обе 20 из 22). Перебор 60 кандидатов "
          "(граница × множество × знаменатель × числитель) — ближайшие иные: «F без отменённых» 19 из 22, «все» 7 из 22.")


def print_article_day(rows, day):
    print(f"\nпо артикулу за {day} (UTC-сутки «Принят в обработку», все созданные): (ΣS − ΣP) / ΣS")
    by = defaultdict(lambda: [Z, Z, 0])
    for r in rows:
        if utc_day(r["processed_at"]) != day:
            continue
        a = by[r["article"]]
        a[0] += r["price"] * r["qty"]; a[1] += r["paid"] * r["qty"]; a[2] += 1
    print(f"{'артикул':22}{'n':>4}{'ΣS':>14}{'ΣP':>14}  доля")
    for art, (s, p, n) in sorted(by.items(), key=lambda kv: -kv[1][0])[:25]:
        print(f"{art:22}{n:>4}{s:>14,.2f}{p:>14,.2f}  {fmt(ratio(s, p))}")
    print(f"… всего артикулов за день: {len(by)}")


def load_fbs_raw(date_from, date_to):
    path = os.path.join(RAW_DIR, f"history_fbs_{date_from}_{date_to}.json")
    if not os.path.exists(path):
        return None
    data = json.load(open(path))
    postings = data["postings"] if isinstance(data, dict) else data
    out = {}
    for p in postings:
        fin = {str(q.get("product_id")): q for q in ((p.get("financial_data") or {}).get("products") or [])}
        for pr in p.get("products") or []:
            fp = fin.get(str(pr.get("sku"))) or {}
            out[(p["posting_number"], str(pr.get("sku")))] = (money(fp.get("customer_price")), money(pr.get("price")), p.get("in_process_at", ""))
    return out


def check_fbs_customer_price(rows, raw):
    print("\nFBS: CSV «Оплачено покупателем» против customer_price ночного списка (/v4), по (отправление, SKU):")
    both = same = diff = 0
    examples = []
    tz_same = tz_total = 0
    for r in rows:
        if r["scheme"] != "fbs":
            continue
        key = (r["posting"], r["sku"])
        if key not in raw:
            continue
        both += 1
        cp, price, ipa = raw[key]
        if cp == r["paid"]:
            same += 1
        else:
            diff += 1
            if len(examples) < 5:
                examples.append((r["posting"], r["sku"], str(r["paid"]), str(cp)))
        tz_total += 1
        tz_same += 1 if ipa.replace("T", " ").replace("Z", "")[:19] == r["processed_at"][:19] else 0
    print(f"  пар найдено {both}; совпали до копейки {same}; расходятся {diff}" + (f"; примеры {examples}" if examples else ""))
    print(f"  «Принят в обработку» CSV = in_process_at списка (UTC) посекундно на {tz_same} из {tz_total} — граница суток отчёта {'UTC' if tz_same == tz_total else 'НЕ UTC?'}")


def load_sales_by_posting(date_from, date_to):
    """(отправление, sku) → (seller_price, sale_price, bonus, coinvestment) из by-day за окно + 10 дней дозревания."""
    from datetime import date, timedelta
    out = {}
    d = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to) + timedelta(days=10)
    while d <= end:
        path = os.path.join(ACCRUAL_DIR, f"{d.isoformat()}.json")
        d += timedelta(days=1)
        if not os.path.exists(path):
            continue
        data = json.load(open(path))
        for a in (data["accruals"] if isinstance(data, dict) else data):
            if a.get("accrued_category") != "POSTING":
                continue
            for pr in (a.get("posting") or {}).get("products") or []:
                c = pr.get("commission") or {}
                if not c or money(c.get("sale_amount")) <= 0:
                    continue
                out[(a.get("unit_number"), str(pr.get("sku")))] = (money(c.get("seller_price")), money(c.get("sale_price")), money(c.get("bonus")), money(c.get("coinvestment")))
    return out


def three_definitions(rows, raw, sales, n=20):
    print(f"\nтри определения на {n} отправлениях с продажей: sale_price by-day / CSV «Оплачено покупателем» / customer_price списка FBS")
    print(f"{'отправление':22}{'sku':>12}{'схема':>6}{'seller':>10}{'sale_price':>11}{'CSV paid':>10}{'cust_price':>11}  = ?")
    shown = ok = 0
    for r in rows:
        key = (r["posting"], r["sku"])
        if key not in sales:
            continue
        sp, sale, bonus, coinv = sales[key]
        cp = raw.get(key, (None,))[0] if raw else None
        agree = sale == r["paid"] and (cp is None or cp == r["paid"])
        ok += 1 if agree else 0
        shown += 1
        if shown <= n:
            print(f"{r['posting']:22}{r['sku']:>12}{r['scheme']:>6}{sp:>10,.2f}{sale:>11,.2f}{r['paid']:>10,.2f}{(cp if cp is not None else Z):>11,.2f}  {'=' if agree else '≠'}")
    print(f"  отправлений с продажей в by-day и в CSV: {shown}; sale_price = CSV (= customer_price, где есть): {ok}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default="2026-09-01")
    ap.add_argument("--date-to", default="2026-09-22")
    ap.add_argument("--article-day", default="2026-09-10")
    args = ap.parse_args(argv)
    rows = load_reports(args.date_from, args.date_to)
    from datetime import date, timedelta
    days = []
    d = date.fromisoformat(args.date_from)
    while d <= date.fromisoformat(args.date_to):
        days.append(d.isoformat()); d += timedelta(days=1)
    by_scheme = defaultdict(int)
    for r in rows:
        by_scheme[r["scheme"]] += 1
    cur = defaultdict(int)
    for r in rows:
        cur[r["currency"] or "(пусто)"] += 1
    out_of_window = sum(1 for r in rows if utc_day(r["processed_at"]) not in days)
    print(f"отчёты ЛК: строк {len(rows)} ({dict(by_scheme)}); валюта покупателя: {dict(cur)}; строк вне окна по «Принят в обработку»: {out_of_window}")
    statuses = defaultdict(int)
    for r in rows:
        statuses[r["status"]] += 1
    print(f"статусы: {dict(statuses)}")
    print_day_table(rows, days, "по UTC-дню «Принят в обработку»: (Σ «Предельная цена» × кол-во − Σ «Оплачено покупателем» × кол-во) / Σ «Предельная цена» × кол-во")
    print_article_day(rows, args.article_day)
    raw = load_fbs_raw(args.date_from, args.date_to)
    if raw:
        check_fbs_customer_price(rows, raw)
    else:
        print("\nсписка FBS за окно на диске нет — сверку customer_price пропускаю")
    sales = load_sales_by_posting(args.date_from, args.date_to)
    three_definitions(rows, raw or {}, sales)
    print("\ndb_writes = 0; обращений к API — 0 (всё с диска)")


if __name__ == "__main__":
    main()
