#!/usr/bin/env python3
"""Справочник типов владельца («Тип начисления → Вид») против нашей свёртки TYPE_TO_EXPENSE. Только чтение.

    venv/bin/python3 scripts/accrual_types_owner_check.py --date-from 2026-09-01 --date-to 2026-09-21 \
        [--canvas-pairs <json пар «Группа услуг||Тип» → [строк, сумма] из полотна владельца>] [--raw-from 2025-11-01 --raw-to 2025-11-30]

Ключ между мирами — русское название типа: /v1/finance/accrual/types отдаёт по каждому type_id `name` (английское) и
`description` — то самое название, что ЛК пишет в колонке «Тип начисления» (проверено 2026-09-23: 1 → «Эквайринг»,
41 → «Оплата за клик», 69 → «Вознаграждение за продажу»). Справочник владельца (docs/owner_manual_report_instruction.md,
таблица «Тип начисления | Вид») ведётся по названиям ЛК, иногда с уточнением в скобках или хвостом «- отмена
начисления» — сопоставление по самому длинному совпавшему префиксу, плюс явные соответствия ниже, где ЛК и API
называют одно разными словами.

Печатает: type_id · описание · наша статья ↔ его Вид · сумма за окно по леджеру (ozon_accrual_daily_types, знак Ozon);
строки «совпало / статья другая / у нас есть, у него нет / у него есть, у нас нет». С --canvas-pairs и --raw-*:
суммы по type_id из сырья by-day за тот же период, что его полотно, против его сумм по типу — проверка ключа числами.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
from loaders import ozon_finance_accrual as accrual  # noqa: E402

DOC = os.path.join(ROOT, "docs", "owner_manual_report_instruction.md")
RAW_DIR = os.path.join(ROOT, "data", "accrual_history")
Z = Decimal(0)
D = lambda v: Decimal(str(v or 0))  # noqa: E731

# ЛК и API называют одно разными словами — сопоставление руками (проверять полотном по суммам).
ALIASES = {
    64: "Услуги Партнёров Ozon на схеме realFBS",                                         # RfbsDomesticDelivery — «Доставка Партнёром Ozon»
    65: "Услуги доставки Партнёрами Ozon на схеме realFBS: Лёгкий возврат Почтой России",  # RfbsEasyReturn — «Лёгкий возврат»
    71: "Вывоз товара со склада силами Ozon: Доставка до СЦ",                                # SellerReturns
    78: "Временное размещение товара в СЦ/ПВЗ",                                             # TemporaryPlacement — «Краткосрочное размещение возврата FBS»
    52: "Подписка Premium Plus",                                                              # PremiumSubscription — «Подписка Premium»
    54: "Продвижение с оплатой за заказ",                                                     # Promotion — «Продвижение товара»
    15: "Утилизация товара: Вы не забрали в срок",                                            # Disposal — «Утилизация»
    9:  "Обработка возвратов Ozon",                                                           # ClientReturn — «Обработка возвратов»
    10: "Прочие компенсации",                                                                 # Compensation — «Компенсация»
    25: "Потеря по вине Ozon в логистике",                                                    # ItemCompensation — «Товарная компенсация»
}
OUR_ARTICLE = {**accrual.TYPE_TO_EXPENSE, **{t: "advertising (41 + 54, Performance)" for t in accrual.AD_TYPE_IDS},
               **{t: "вне классификации (доход)" for t in accrual.UNCLASSIFIED_TYPE_IDS}, 69: "commission (SaleCommission)"}
# Наша статья → его Вид, если он совпадает по смыслу
ARTICLE_TO_KIND = {"commission": "Комиссия", "logistics": "Логистика", "subscription": "Реклама", "external_promo": "Реклама",
                   "other": "Прочее", "advertising (41 + 54, Performance)": "Реклама", "commission (SaleCommission)": "Комиссия",
                   "вне классификации (доход)": "От Ozon"}
ACQUIRING = 1


def owner_directory(path=DOC):
    """[(название ЛК, Вид)] из таблицы «Тип начисления | Вид»."""
    out = []
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if not m or m.group(1) in ("Тип начисления", "---"):
            continue
        kind = re.sub(r"\s*\(в файле выделено красным\)", "", m.group(2)).strip()
        out.append((m.group(1).strip(), kind))
    return out


def match_owner(description, directory, type_id):
    """Строки справочника владельца для типа: по псевдониму или по самому длинному общему префиксу описания."""
    if type_id in ALIASES:
        base = ALIASES[type_id]
        rows = [(n, k) for n, k in directory if n == base or n.startswith(base)]
        if rows:
            return rows
    desc = description.lower()
    rows = [(n, k) for n, k in directory if n.lower() == desc or n.lower().startswith(desc + " ") or n.lower().startswith(desc + ":") or n.lower().startswith(desc + " -")]
    if rows:
        return rows
    return [(n, k) for n, k in directory if n.lower() == desc]


def ledger_sums(date_from, date_to):
    import loaders.ozon_fbo_orders_loader as fbo
    out, start = defaultdict(Decimal), 0
    while True:
        page = (fbo.supabase.table("ozon_accrual_daily_types").select("accrual_date,type_id,amount")
                .gte("accrual_date", date_from).lte("accrual_date", date_to).order("accrual_date").order("type_id")
                .range(start, start + 999).execute().data or [])
        for r in page:
            out[int(r["type_id"])] += D(r["amount"])
        if len(page) < 1000:
            return out
        start += 1000


def raw_sums(date_from, date_to):
    """{type_id: сумма} из data/accrual_history за окно, включая 69 (комиссию) отдельной строкой — по строкам услуг и продаж."""
    from datetime import date, timedelta
    out, days = defaultdict(Decimal), 0
    d = date.fromisoformat(date_from)
    while d <= date.fromisoformat(date_to):
        path = os.path.join(RAW_DIR, f"{d.isoformat()}.json")
        d += timedelta(days=1)
        if not os.path.exists(path):
            continue
        days += 1
        data = json.load(open(path))
        for a in (data["accruals"] if isinstance(data, dict) else data):
            for type_id, _sku, amount in accrual._service_lines(a):
                out[type_id] += D(amount)
            for pr in ((a.get("posting") or {}).get("products") or []):
                c = pr.get("commission") or {}
                if c:
                    out["sale_amount"] += D((c.get("sale_amount") or {}).get("amount") if isinstance(c.get("sale_amount"), dict) else c.get("sale_amount"))
                    out[69] += D((c.get("sale_commission") or {}).get("amount") if isinstance(c.get("sale_commission"), dict) else c.get("sale_commission"))
                    for k in ("bonus", "coinvestment"):
                        out[k] += D((c.get(k) or {}).get("amount") if isinstance(c.get(k), dict) else c.get(k))
    return out, days


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default="2026-09-01")
    ap.add_argument("--date-to", default="2026-09-21")
    ap.add_argument("--types-json", help="сохранённый ответ /v1/finance/accrual/types; нет — один вызов")
    ap.add_argument("--canvas-pairs", help="json {«Группа||Тип»: [строк, сумма]} из полотна владельца")
    ap.add_argument("--raw-from"); ap.add_argument("--raw-to")
    args = ap.parse_args(argv)
    if args.types_json:
        types = {t["id"]: t for t in json.load(open(args.types_json))}
        calls = 0
    else:
        import requests
        r = requests.post(f"{accrual.BASE}/v1/finance/accrual/types", headers=accrual.headers(), json={}, timeout=60)
        types = {t["id"]: t for t in (r.json() or {}).get("accrual_types", [])}
        calls = 1
    directory = owner_directory()
    sums = ledger_sums(args.date_from, args.date_to)
    print(f"справочник владельца: {len(directory)} строк; типов у Ozon: {len(types)}; в леджере за {args.date_from} … {args.date_to}: {len(sums)} типов; обращений к API {calls}")
    ours = sorted(set(OUR_ARTICLE) | set(sums))
    print(f"\n{'type':>4} {'описание (ЛК)':44} {'наша статья':36} {'его Вид':30} {'итог':22} {'Σ окна (знак Ozon)':>18}")
    verdicts = defaultdict(list)
    for t in ours:
        desc = (types.get(t) or {}).get("description") or (types.get(t) or {}).get("name") or "?"
        our = OUR_ARTICLE.get(t, "НЕТ У НАС (unknown_)")
        rows = match_owner(desc, directory, t)
        kinds = sorted({k for _n, k in rows})
        if not rows:
            verdict = "у нас есть, у него нет"
        elif t == ACQUIRING:
            verdict = "совпало (эквайринг — своя колонка)" if kinds == ["Эквайринг"] else "статья другая"
        elif t not in OUR_ARTICLE:
            verdict = "у него есть, у нас нет"
        elif len(kinds) == 1 and kinds[0] == ARTICLE_TO_KIND.get(our):
            verdict = "совпало"
        elif len(kinds) > 1:
            verdict = f"у него по-разному ({' / '.join(kinds)})"
        else:
            verdict = "статья другая"
        verdicts[verdict.split(" (")[0]].append(t)
        print(f"{t:>4} {desc[:44]:44} {our[:36]:36} {' / '.join(kinds)[:30] or '—':30} {verdict[:22]:22} {sums.get(t, Z):>18,.2f}")
    matched_names = {n for t in ours for n, _k in match_owner((types.get(t) or {}).get('description') or '', directory, t)}
    theirs_only = [(n, k) for n, k in directory if n not in matched_names]
    print(f"\nитоги: " + "; ".join(f"{v} — {len(ts)} ({', '.join(map(str, ts))})" for v, ts in verdicts.items()))
    print(f"у него есть, у нас нет соответствия по типу ({len(theirs_only)} строк справочника): " + "; ".join(f"{n} → {k}" for n, k in theirs_only))
    if args.canvas_pairs and args.raw_from and args.raw_to:
        pairs = json.load(open(args.canvas_pairs))
        by_type_name = defaultdict(Decimal)
        for key, (n, s) in pairs.items():
            by_type_name[key.split("||", 1)[1]] += D(s)
        raw, days = raw_sums(args.raw_from, args.raw_to)
        print(f"\nполотно владельца против сырья by-day за {args.raw_from} … {args.raw_to} (дней в файлах {days}):")
        print(f"{'type':>4} {'описание (ЛК)':46} {'его название':48} {'его Σ':>16} {'наша Σ':>16} {'разница':>12}")
        used = set()
        for t in sorted(k for k in raw if isinstance(k, int)):
            desc = (types.get(t) or {}).get("description") or "?"
            rows = match_owner(desc, directory, t)
            names = [n for n, _k in rows] or [desc]
            his = sum((by_type_name.get(n, Z) for n in names), Z)
            used.update(n for n in names if n in by_type_name)
            print(f"{t:>4} {desc[:46]:46} {(' / '.join(names))[:48]:48} {his:>16,.2f} {raw[t]:>16,.2f} {his - raw[t]:>12,.2f}")
        print(f"продажи: его «Выручка» = наш sale_amount? {by_type_name.get('Выручка', Z):,.2f} против {raw.get('sale_amount', Z):,.2f}; "
              f"«Баллы за скидки» {by_type_name.get('Баллы за скидки', Z):,.2f} против bonus {raw.get('bonus', Z):,.2f}; "
              f"«Программы партнёров» {by_type_name.get('Программы партнёров', Z):,.2f} против coinvestment {raw.get('coinvestment', Z):,.2f}")
        rest = {n: s for n, s in by_type_name.items() if n not in used and n not in ("Выручка", "Баллы за скидки", "Программы партнёров", "Возврат выручки")}
        print("его типы, не сведённые ни с одним нашим type_id: " + "; ".join(f"{n} {s:,.2f}" for n, s in sorted(rest.items())))
    print("\ndb_writes = 0")


if __name__ == "__main__":
    main()
