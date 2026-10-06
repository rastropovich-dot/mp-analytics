#!/usr/bin/env python3
"""Расходы Ozon по группам финансового отчёта против наших статей (сорок пятая §4). Только чтение; db_writes = 0.

    venv/bin/python3 scripts/ozon_balance_groups_check.py [--balance data/snapshots/finance_balance_2026-04_2026-08.json]
                                                          [--book data/reports/finrez_2026-04_2026-09.xlsx]

Источник групп. В API у типа начисления группы нет (accrual/types, accrual/by-day — только type_id; в чате 2026-07-08 просят
«добавьте Группу начисления, как в ручной выгрузке»). Экран ЛК «Финансы → Баланс» отдаёт POST /v1/finance/balance (период ≤ 30 дней):
продажи и возвраты (сумма, вознаграждение Ozon) и services[] по СИСТЕМНОМУ имени услуги (acquiring, logistics, …), тоже без группы.
Группы («Услуги доставки», «Услуги агентов» …) — разметка экрана; по type_id она есть только в выгрузке ЛК владельца за ноябрь 2025
(OZON_GROUP в scripts/accrual_types_owner_check.py, суммы сошлись с сырьём до копейки). Поэтому здесь: услуга баланса ↔ type_id леджера
(ozon_accrual_daily_types) по равенству сумм месяца до копейки, type_id → группа ноябрьской выгрузки, группа → статья по правилу
аналитика (Даниил, 09.04.2026), и против неё — наша статья (TYPE_TO_EXPENSE, 41 + 54 — реклама, тип 1 — эквайринг, 25 + 10 — доход).
"""
import argparse
import json
import os
import sys
import warnings
from collections import defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from accrual_types_owner_check import OZON_GROUP  # noqa: E402
from loaders.ozon_finance_accrual import AD_TYPE_IDS, TYPE_TO_EXPENSE, UNCLASSIFIED_TYPE_IDS  # noqa: E402

D, Z, VAT = Decimal, Decimal(0), Decimal("1.22")
MONTHS = ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08")
# правило аналитика (группа финансового отчёта → статья формы); «Услуги агентов» ноябрьской выгрузки = «Услуги партнёров» его списка
ANALYST = {"Вознаграждение Ozon": "комиссия", "Продвижение и реклама": "реклама", "Услуги доставки": "логистика", "Услуги FBO": "прочее",
           "Услуги агентов": "эквайринг", "Прочие начисления": "прочее", "Другие услуги": "прочее (в его списке нет — гипотеза)",
           "Компенсации и декомпенсации": "доход (в его списке нет)"}
NO_GROUP = "(нет в ноябрьской выгрузке)"


def our_article(type_id):
    """Статья, куда тип кладём мы (форма «Фин рез» / «Ozon - месяц»): реклама 41 + 54 и подписка (у владельца — в «Рекламе»)."""
    if type_id in AD_TYPE_IDS:
        return "реклама"
    if type_id in UNCLASSIFIED_TYPE_IDS:
        return "доход (компенсации)"
    if type_id == 1:
        return "эквайринг"
    art = TYPE_TO_EXPENSE.get(type_id)
    return {"logistics": "логистика", "other": "прочее", "subscription": "реклама (подписка)", "external_promo": "реклама (внешнее)"}.get(art, f"? {art}")


def balance_by_month(path):
    snap = json.load(open(path, encoding="utf-8"))
    out = {}
    for key, b in snap["chunks"].items():
        m = key[:7]
        a = out.setdefault(m, {"sales": Z, "sales_fee": Z, "returns": Z, "returns_fee": Z, "services": defaultdict(Decimal)})
        cf = b.get("cashflows") or {}
        a["sales"] += D(str(cf["sales"]["amount"]["value"])); a["sales_fee"] += D(str(cf["sales"]["fee"]["value"]))
        a["returns"] += D(str(cf["returns"]["amount"]["value"])); a["returns_fee"] += D(str(cf["returns"]["fee"]["value"]))
        for s in cf.get("services") or []:
            a["services"][s["name"]] += D(str(s["amount"]["value"]))
    return out


def ledger_by_month(sb):
    """{месяц: {type_id: (имя, сумма со знаком Ozon)}} — чтение с сортировкой по полному ключу, страницами по 1 000."""
    out, start = defaultdict(dict), 0
    while True:
        page = (sb.table("ozon_accrual_daily_types").select("accrual_date,type_id,type_name,amount")
                .gte("accrual_date", "2026-04-01").lte("accrual_date", "2026-08-31").order("accrual_date").order("type_id")
                .range(start, start + 999).execute().data or [])
        for r in page:
            m, t = str(r["accrual_date"])[:7], int(r["type_id"])
            name, s = out[m].get(t, (r["type_name"], Z))
            out[m][t] = (name, s + D(str(r["amount"])))
        if len(page) < 1000:
            return out
        start += 1000


def book_turnover(path):
    import openpyxl
    warnings.simplefilter("ignore")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb["Выкупы Ozon"].iter_rows(min_row=1, max_row=200, values_only=True)); wb.close()
    ix = {str(v).strip(): j for j, v in enumerate(rows[2]) if v}
    num = {"апр": "2026-04", "май": "2026-05", "июн": "2026-06", "июл": "2026-07", "авг": "2026-08"}
    return {num[r[0]]: (D(str(r[ix["Оборот (с НДС)"]])), D(str(r[ix["Комиссия (с НДС), руб."]]))) for r in rows[3:] if r and r[0] in num}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", default=cabinet.data_path("snapshots", "finance_balance_2026-04_2026-08.json", prof=cabinet.profile()))
    ap.add_argument("--book", default=cabinet.data_path("reports", "finrez_2026-04_2026-09.xlsx", prof=cabinet.profile()))
    args = ap.parse_args(argv)
    from dotenv import load_dotenv
    from supabase import create_client
    load_dotenv(os.path.join(ROOT, ".env"))
    import cabinet
    cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    bal, led = balance_by_month(args.balance), ledger_by_month(sb)
    book = book_turnover(args.book) if os.path.exists(args.book) else {}

    print("1. Продажи, возвраты, вознаграждение (баланс) против оборота и комиссии «Выкупы Ozon»")
    for m in MONTHS:
        b = bal[m]; turn, fee = b["sales"] + b["returns"], b["sales_fee"] + b["returns_fee"]
        bt, bc = book.get(m, (None, None))
        vs_turn = f"«Выкупы Ozon» {bt:,.2f} → разница {turn - bt:,.2f}" if bt is not None else "книги нет — не сверено"
        vs_fee = f"наша комиссия {-bc:,.2f} → разница {fee + bc:,.2f}" if bc is not None else ""
        print(f"  {m}: продажи {b['sales']:,.2f} + возвраты {b['returns']:,.2f} = {turn:,.2f}; {vs_turn}; вознаграждение {fee:,.2f}; {vs_fee}")

    print("\n2. Услуга баланса ↔ type_id леджера (равенство суммы месяца до копейки)")
    name_to_type, unmatched = defaultdict(set), defaultdict(list)
    for m in MONTHS:
        by_amount = defaultdict(list)
        for t, (_n, s) in led[m].items():
            by_amount[s].append(t)
        for name, s in bal[m]["services"].items():
            cand = by_amount.get(s, [])
            if len(cand) == 1:
                name_to_type[name].add(cand[0])
            else:
                unmatched[name].append((m, s, cand))
    for name in sorted(set(name_to_type) | set(unmatched)):
        types = sorted(name_to_type.get(name, []))
        miss = unmatched.get(name, [])
        print(f"  {name:48} → type {types or '—'}" + (f"; без пары: {[(m, f'{s:,.2f}', c) for m, s, c in miss]}" if miss else ""))

    print("\n3. По группам (type_id → группа ноябрьской выгрузки), с НДС и без; правило аналитика против нашей статьи")
    types_seen = sorted({t for m in MONTHS for t in led[m]})
    for m in MONTHS:
        groups = defaultdict(lambda: defaultdict(Decimal))
        for t, (_n, s) in led[m].items():
            if s == 0:
                continue
            groups[OZON_GROUP.get(t, NO_GROUP)][our_article(t)] += s
        print(f"  {m}:")
        for g in sorted(groups, key=lambda g: (g == NO_GROUP, g)):
            tot = sum(groups[g].values(), Z)
            target = ANALYST.get(g, "?")
            parts = "; ".join(f"{a} {v:,.2f}" for a, v in sorted(groups[g].items()))
            off = sum((v for a, v in groups[g].items() if not a.startswith(target.split(" ")[0])), Z)
            print(f"    {g:30} с НДС {tot:>16,.2f}  без НДС {tot / VAT:>16,.2f}  → аналитик: {target:12} | наши статьи: {parts}"
                  + (f"  | не туда: {off:,.2f}" if off and g != NO_GROUP else ""))

    print("\n4. Типы, которые мы кладём не туда, куда группа по правилу аналитика (Σ апрель … август, со знаком Ozon)")
    for t in types_seen:
        g = OZON_GROUP.get(t)
        if not g:
            continue
        target, ours = ANALYST.get(g, "?"), our_article(t)
        if not ours.startswith(target.split(" ")[0]):
            name = next((led[m][t][0] for m in MONTHS if t in led[m]), "?")
            tot = sum((led[m][t][1] for m in MONTHS if t in led[m]), Z)
            print(f"  {t:4} {name:40} группа «{g}» → аналитик {target}; у нас {ours}; Σ {tot:,.2f}")
    print("  типы без группы в ноябрьской выгрузке:", [(t, next((led[m][t][0] for m in MONTHS if t in led[m]), "?"), our_article(t),
                                                         f"{sum((led[m][t][1] for m in MONTHS if t in led[m]), Z):,.2f}") for t in types_seen if t not in OZON_GROUP])

    print("\n5. Компенсации (типы 25 + 10) по месяцам — в фин. рез. не входят (доход, отдельной строкой)")
    for m in MONTHS:
        print(f"  {m}: 25 {led[m].get(25, ('', Z))[1]:,.2f}; 10 {led[m].get(10, ('', Z))[1]:,.2f}; всего {led[m].get(25, ('', Z))[1] + led[m].get(10, ('', Z))[1]:,.2f}")
    print("\nобращений к API — 0 (баланс из файла), db_writes = 0")


if __name__ == "__main__":
    main()
