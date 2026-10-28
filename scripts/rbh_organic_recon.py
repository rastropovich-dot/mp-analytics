#!/usr/bin/env python3
"""Органика РБХ «до / после» включения «ОЗЗ — все товары» (инбокс RBH 28.10): разведка с выгрузкой в файлы, без записи в базы.

    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py campaigns
    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py accrual --date-from 2026-05-01 --date-to 2026-08-22
    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py orders --date-from … --date-to …
    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py analytics --date-from … --date-to …
    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py perf-daily --date-from … --date-to …
    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py cpo --date-from … --date-to …        (+1 выгрузка Performance)
    MP_CABINET=rbh1 venv/bin/python3 scripts/rbh_organic_recon.py table --enable-date … --date-from … --date-to …

Все ответы — в data/<кабинет>/organic_recon/ (вне git); файл есть — в API не идёт. Сырьё начислений кладётся отдельно от
data/<кабинет>/accrual_history (его читает пересборка расходов — история до 23.08 туда попадать не должна). Отправления — через
rebuild_ozon_orders_history.fetch_history (тот же сбор, что у пересборки истории; файлы history_*/part_* в postings_raw).
Счётчик обращений — по каждому шагу, в calls.json. db_writes = 0 во всех шагах. Не стартует в окне ночного прогона.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
_PROFILE = cabinet.assert_env()
import requests  # noqa: E402
from loaders.pipeline_window import in_nightly_run_window, window_text  # noqa: E402

OUT = cabinet.data_path("organic_recon", prof=_PROFILE)
PERF = "https://api-performance.ozon.ru"
D = lambda v: Decimal(str(v or 0))  # noqa: E731
CALLS = Counter()


def guard_night():
    if in_nightly_run_window(datetime.now(timezone.utc)):
        raise SystemExit(f"окно ночного прогона {window_text()} — не стартую")


def path(*parts):
    p = os.path.join(OUT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def save_calls(step):
    p = path("calls.json")
    data = json.load(open(p)) if os.path.exists(p) else {}
    prev = data.get(step, {})
    for k, v in CALLS.items():
        prev[k] = prev.get(k, 0) + v
    data[step] = prev
    json.dump(data, open(p, "w"), ensure_ascii=False, indent=1)
    print(f"обращений за запуск: {dict(CALLS)}; db_writes = 0")


def days(d0, d1):
    d, out = date.fromisoformat(d0), []
    while d <= date.fromisoformat(d1):
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------- Performance ----------

_TOKEN = {}


def perf_token():
    if _TOKEN.get("exp", 0) > time.time() + 60:
        return _TOKEN["t"]
    r = requests.post(f"{PERF}/api/client/token", json={
        "client_id": os.environ["OZON_PERFORMANCE_CLIENT_ID"], "client_secret": os.environ["OZON_PERFORMANCE_CLIENT_SECRET"],
        "grant_type": "client_credentials"}, headers={"Accept": "application/json"}, timeout=60)
    CALLS["perf_token"] += 1
    r.raise_for_status()
    _TOKEN.update(t=r.json()["access_token"], exp=time.time() + int(r.json().get("expires_in") or 1800))
    return _TOKEN["t"]


def perf_get(endpoint, params, kind):
    for attempt in range(1, 4):
        r = requests.get(f"{PERF}{endpoint}", params=params, headers={"Authorization": f"Bearer {perf_token()}"}, timeout=120)
        CALLS[kind] += 1
        if r.status_code == 429 and attempt < 3:
            CALLS[kind + "_429"] += 1
            print(f"  429 {endpoint}: {r.text[:200]} — пауза 60 с")
            time.sleep(60)
            continue
        if r.status_code != 200:
            raise SystemExit(f"{endpoint} {params}: HTTP {r.status_code} {r.text[:300]}")
        return r
    raise RuntimeError("недостижимо")


def cmd_campaigns(args):
    guard_night()
    # фильтр advObjectType=ALL_SKU_PROMO Ozon отдаёт 500 (28.10, оба кабинета) — полный список одним запросом, фильтр у себя
    r = perf_get("/api/client/campaign", {}, "perf_campaign_list")
    every = r.json().get("list") or []
    out = defaultdict(list)
    for c in every:
        out[c.get("advObjectType") or "?"].append(c)
    out = dict(out)
    out.setdefault("ALL_SKU_PROMO", []); out.setdefault("SEARCH_PROMO", []); out.setdefault("SKU", [])
    print(f"всего кампаний {len(every)}: {dict(Counter(c.get('advObjectType') for c in every))}")
    json.dump(out, open(path("campaigns.json"), "w"), ensure_ascii=False, indent=1)
    for t in ("ALL_SKU_PROMO", "SEARCH_PROMO"):
        lst = out[t]
        print(f"{t}: {len(lst)} кампаний, состояния {dict(Counter(c.get('state') for c in lst))}")
        for c in sorted(lst, key=lambda c: c.get("createdAt") or "")[:3] if t == "SEARCH_PROMO" else lst:
            print("  ", {k: c.get(k) for k in ("id", "title", "state", "createdAt", "updatedAt", "fromDate", "toDate", "paymentType", "productAutopilotStrategy")})
        if t == "SEARCH_PROMO" and lst:
            created = sorted((c.get("createdAt") or "")[:10] for c in lst)
            print(f"   создание SEARCH_PROMO: первая {created[0]}, последняя {created[-1]}; по месяцам {dict(Counter(x[:7] for x in created))}")
    print(f"SKU: {len(out['SKU'])} кампаний")
    save_calls("campaigns")


def cmd_perf_daily(args):
    """GET statistics/daily/json без campaignIds — по спеке «статистика по всем кампаниям в промежутке», заказы шт / ₽ и расход
    по кампании × день, синхронно (не выгрузка-отчёт). Один запрос на окно."""
    guard_night()
    p = path("perf_daily", f"{args.date_from}_{args.date_to}.json")
    if os.path.exists(p):
        print(f"файл есть: {p}")
        return
    r = perf_get("/api/client/statistics/daily/json", {"dateFrom": args.date_from, "dateTo": args.date_to}, "perf_statistics_daily")
    data = r.json()
    json.dump({"date_from": args.date_from, "date_to": args.date_to, "response": data}, open(p, "w"), ensure_ascii=False)
    rows = data.get("rows") or []
    print(f"строк кампания × день {len(rows)}, кампаний {len({x.get('id') for x in rows})} → {p}")
    if rows:
        print("пример строки:", rows[0])
    save_calls("perf_daily")


def cmd_cpo(args):
    """CPO «Все» за окно ≤ 62 дней одним отчётом — +1 выгрузка Performance (тем же клиентом, что ночью)."""
    guard_night()
    p = path("cpo", f"{args.date_from}_{args.date_to}.csv")
    if os.path.exists(p):
        print(f"файл есть: {p}")
        return
    if (date.fromisoformat(args.date_to) - date.fromisoformat(args.date_from)).days > 61:
        raise SystemExit("окно CPO — не больше 62 дней")
    from loaders import ozon_performance_ads_loader as ads
    client = ads.OzonPerformanceClient()
    uuid, status, csv_text = client.fetch_all_sku_promo_csv("orders", args.date_from, args.date_to)
    open(p, "w", encoding="utf-8").write(csv_text)
    CALLS["perf_cpo_generate"] += 1
    print(f"CPO «Все» uuid {uuid}, state {status.get('state')}, байт {len(csv_text)} → {p}")
    save_calls("cpo")


# ---------- Seller ----------

def cmd_accrual(args):
    guard_night()
    from loaders import ozon_finance_accrual as accrual
    total = Counter()
    for d in days(args.date_from, args.date_to):
        p = path("accrual_by_day", f"{d}.json")
        if os.path.exists(p):
            continue
        guard_night()
        stats = {}
        rows = accrual.fetch_day(d, stats)
        json.dump({"date": d, "fetched_at": datetime.now(timezone.utc).isoformat(), "accruals": rows}, open(p, "w"), ensure_ascii=False)
        total["requests"] += stats.get("requests", stats.get("pages", 0))
        total["retries"] += stats.get("retries", 0)
        print(f"{d}: начислений {len(rows)}, страниц {stats.get('pages')}, повторов {stats.get('retries', 0)}", flush=True)
        time.sleep(1.5)
    CALLS["seller_accrual_by_day"] += total["requests"]
    CALLS["seller_accrual_by_day_retries"] += total["retries"]
    save_calls("accrual")


def accrual_day_types(d):
    p = path("accrual_by_day", f"{d}.json")
    if not os.path.exists(p):
        return None
    from loaders import ozon_finance_accrual as accrual
    out = defaultdict(Decimal)
    for a in json.load(open(p))["accruals"]:
        for tid, _sku, amount in accrual._service_lines(a):     # то же разложение по услугам, что у загрузчика расходов
            out[int(tid)] += D(amount)
    return out


def cmd_accrual_summary(args):
    print("день | тип 41 клики | тип 54 ОЗЗ | прочие рекламные (51 / 52 / 74 / 95 / 96)")
    for d in days(args.date_from, args.date_to):
        t = accrual_day_types(d)
        if t is None:
            print(f"{d} | нет файла")
            continue
        print(f"{d} | {-t.get(41, 0):,.2f} | {-t.get(54, 0):,.2f} | " + " / ".join(f"{-t.get(k, 0):,.2f}" for k in (51, 52, 74, 95, 96)))


def cmd_orders(args):
    import scripts.rebuild_ozon_orders_history as hist  # noqa: E402 — сбор с кусками и счётчиками, файлы в postings_raw
    from loaders import ozon_orders_rows as rows_mod
    per_day = defaultdict(lambda: Counter())
    for scheme in ("fbo", "fbs"):
        postings = hist.fetch_history(scheme, args.date_from, args.date_to)
        rows, counters = rows_mod.build_order_rows(postings, scheme)
        print(f"{scheme}: строк (день, SKU) {len(rows)}, счётчики {counters}")
        for r in rows:
            c = per_day[r["order_date"]]
            c[f"{scheme}_qty"] += r["orders_qty"]; c[f"{scheme}_amt"] += r["orders_amount_seller"]
            c[f"{scheme}_cqty"] += r["cancelled_orders_qty"]; c[f"{scheme}_camt"] += r["cancelled_orders_amount_seller"]
    out = {d: dict(per_day[d]) for d in sorted(per_day)}
    json.dump(out, open(path("orders_by_day", f"{args.date_from}_{args.date_to}.json"), "w"), ensure_ascii=False, indent=1)
    print(f"дней {len(out)} → orders_by_day/{args.date_from}_{args.date_to}.json")


def cmd_analytics(args):
    """analytics/data по дню (без SKU): ordered_units, revenue — сверка, не база."""
    guard_night()
    p = path("analytics", f"{args.date_from}_{args.date_to}.json")
    if os.path.exists(p):
        print(f"файл есть: {p}")
        return
    hdr = {"Client-Id": os.environ["OZON_CLIENT_ID"], "Api-Key": os.environ["OZON_API_KEY"]}
    body = {"date_from": args.date_from, "date_to": args.date_to, "metrics": ["ordered_units", "revenue"], "dimension": ["day"],
            "limit": 1000, "offset": 0}
    for attempt in range(1, 7):
        r = requests.post("https://api-seller.ozon.ru/v1/analytics/data", json=body, headers=hdr, timeout=120)
        CALLS["seller_analytics_data"] += 1
        if r.status_code == 429:
            CALLS["seller_analytics_data_429"] += 1
            wait = int(r.headers.get("Retry-After") or 0) or 30
            print(f"  429 analytics/data, пауза {min(wait, 60)} с")
            time.sleep(min(wait, 60))
            continue
        r.raise_for_status()
        break
    data = r.json()
    json.dump(data, open(p, "w"), ensure_ascii=False)
    print(f"строк {len((data.get('result') or {}).get('data') or [])} → {p}")
    save_calls("analytics")


def num(v):
    return D(str(v or 0).replace("\xa0", "").replace(" ", "").replace(",", "."))


def enable_date():
    """Дата создания кампании «Оплата за заказ — все товары» (ALL_SKU_PROMO) по Москве — из списка кампаний."""
    camps = json.load(open(path("campaigns.json")))["ALL_SKU_PROMO"]
    ts = datetime.fromisoformat(camps[0]["createdAt"].replace("Z", "+00:00")) + timedelta(hours=3)
    return ts.date()


def perf_by_day():
    """statistics/daily: по дню — клики (SKU-кампании) шт / ₽ / расход, ОЗЗ выбранные (SEARCH_PROMO) шт / ₽ / расход, расход ALL_SKU_PROMO."""
    camps = json.load(open(path("campaigns.json")))
    typ = {str(c["id"]): t for t, lst in camps.items() for c in lst}
    out = defaultdict(lambda: defaultdict(Decimal))
    import glob
    for f in sorted(glob.glob(path("perf_daily", "*.json"))):
        for r in json.load(open(f))["response"].get("rows") or []:
            cls = {"SKU": "cpc", "SEARCH_PROMO": "ozz_sel", "ALL_SKU_PROMO": "ozz_all"}.get(typ.get(str(r["id"])), "other")
            o = out[r["date"]]
            o[f"{cls}_qty"] += num(r.get("orders")); o[f"{cls}_amt"] += num(r.get("ordersMoney")); o[f"{cls}_spend"] += num(r.get("moneySpent"))
    return out


def cpo_by_day():
    """CPO «Все» (отчёт all_sku_promo/orders): по дате заказа — штуки, ₽ продажи, расход, строк (заказ × SKU), перетоков (SKU ≠ продвигаемого)."""
    import glob
    out = defaultdict(lambda: defaultdict(Decimal))
    for f in sorted(glob.glob(path("cpo", "*.csv"))):
        lines = open(f, encoding="utf-8-sig").read().splitlines()
        hdr_i = next(i for i, ln in enumerate(lines) if ln.startswith("Дата;"))
        for r in csv.DictReader(io.StringIO("\n".join(lines[hdr_i:])), delimiter=";"):
            try:
                d = datetime.strptime(r["Дата"], "%d.%m.%Y").date().isoformat()
            except (ValueError, TypeError):
                continue      # строка «Всего» и пустые
            o = out[d]
            o["qty"] += num(r["Количество"]); o["amt"] += num(r["Стоимость продажи, ₽"]); o["spend"] += num(r["Расход, ₽"]); o["lines"] += 1
            if r["SKU"] != r["SKU продвигаемого товара"]:
                o["cross_qty"] += num(r["Количество"])
    return out


def cmd_table(args):
    en = date.fromisoformat(args.enable_date) if args.enable_date else enable_date()
    orders = json.load(open(path("orders_by_day", f"{args.date_from}_{args.date_to}.json")))
    an = {x["dimensions"][0]["id"]: x["metrics"] for x in
          json.load(open(path("analytics", f"{args.date_from}_{args.date_to}.json")))["result"]["data"]}
    perf, cpo = perf_by_day(), cpo_by_day()
    import glob
    cpo_days = set()
    for f in glob.glob(path("cpo", "*.csv")):
        a, b = os.path.basename(f)[:-4].split("_")
        cpo_days |= set(days(a, b))
    weeks = defaultdict(lambda: defaultdict(Decimal))
    daily = {}
    for d in days(args.date_from, args.date_to):
        dd = date.fromisoformat(d)
        monday = (dd - timedelta(days=dd.weekday())).isoformat()
        o, p, c, t = orders.get(d, {}), perf.get(d, {}), cpo.get(d, {}), accrual_day_types(d) or {}
        row = {
            "all_qty": D(o.get("fbo_qty", 0)) + D(o.get("fbs_qty", 0)) + D(o.get("fbo_cqty", 0)) + D(o.get("fbs_cqty", 0)),
            "all_amt": D(o.get("fbo_amt", 0)) + D(o.get("fbs_amt", 0)) + D(o.get("fbo_camt", 0)) + D(o.get("fbs_camt", 0)),
            "conf_qty": D(o.get("fbo_qty", 0)) + D(o.get("fbs_qty", 0)),
            "conf_amt": D(o.get("fbo_amt", 0)) + D(o.get("fbs_amt", 0)),
            "an_qty": D(an.get(d, [0, 0])[0]), "an_amt": D(str(an.get(d, [0, 0])[1])),
            "cpc_qty": p.get("cpc_qty", D(0)), "cpc_amt": p.get("cpc_amt", D(0)), "cpc_spend_perf": p.get("cpc_spend", D(0)),
            "sel_qty": p.get("ozz_sel_qty", D(0)), "sel_amt": p.get("ozz_sel_amt", D(0)), "sel_spend_perf": p.get("ozz_sel_spend", D(0)),
            "all_ozz_qty": c.get("qty", D(0)), "all_ozz_amt": c.get("amt", D(0)), "all_ozz_spend_cpo": c.get("spend", D(0)),
            "all_ozz_spend_perf": p.get("ozz_all_spend", D(0)), "all_ozz_cross_qty": c.get("cross_qty", D(0)),
            "cpo_covered": D(1 if d in cpo_days else 0),
            "t41": -D(t.get(41, 0)), "t54": -D(t.get(54, 0)), "accrual_file": D(1 if t else 0),
        }
        row["ozz_qty"] = row["sel_qty"] + row["all_ozz_qty"]
        row["ozz_amt"] = row["sel_amt"] + row["all_ozz_amt"]
        row["ad_sum_qty"] = row["cpc_qty"] + row["ozz_qty"]
        row["ad_max_qty"] = max(row["cpc_qty"], row["ozz_qty"])
        row["ad_sum_amt"] = row["cpc_amt"] + row["ozz_amt"]
        row["ad_max_amt"] = max(row["cpc_amt"], row["ozz_amt"])
        row["days"] = D(1)
        daily[d] = {k: str(v) for k, v in row.items()}
        for k, v in row.items():
            weeks[monday][k] += v
    json.dump(daily, open(path("table_by_day.json"), "w"), ensure_ascii=False, indent=1)
    en_monday = en - timedelta(days=en.weekday())

    def phase(m):
        m = date.fromisoformat(m)
        return "до" if m < en_monday else ("переход" if m == en_monday else "после")

    def f0(v):
        return f"{v:,.0f}".replace(",", " ")

    def pct(a, b):
        return f"{a / b * 100:.1f} %" if b else "—"

    print(f"включение «все товары»: {en} (неделя с {en_monday})")
    print("\n| неделя | фаза | дней | все заказы шт | все ₽ | подтв. ₽ | analytics шт | клики шт | ОЗЗ выбр. шт | ОЗЗ все шт | реклама шт: сумма … max |"
          " органика шт: все − сумма … все − max | доля органики | клики ₽ (тип 41) | ОЗЗ ₽ (тип 54) | ДРР к «все ₽» |")
    print("|" + "---|" * 16)
    for m in sorted(weeks):
        w = weeks[m]
        end = (date.fromisoformat(m) + timedelta(days=6)).isoformat()
        flag = "" if w["cpo_covered"] == w["days"] or phase(m) == "до" else f" ⚠ CPO {int(w['cpo_covered'])}/{int(w['days'])} дн."
        if w["accrual_file"] != w["days"]:
            flag += f" ⚠ начисления {int(w['accrual_file'])}/{int(w['days'])}"
        org_hi, org_lo = w["all_qty"] - w["ad_max_qty"], w["all_qty"] - w["ad_sum_qty"]
        print(f"| {m[5:]} … {end[5:]} | {phase(m)}{flag} | {int(w['days'])} | {f0(w['all_qty'])} | {f0(w['all_amt'])} | {f0(w['conf_amt'])} | {f0(w['an_qty'])} |"
              f" {f0(w['cpc_qty'])} | {f0(w['sel_qty'])} | {f0(w['all_ozz_qty'])} | {f0(w['ad_sum_qty'])} … {f0(w['ad_max_qty'])} |"
              f" {f0(org_lo)} … {f0(org_hi)} | {pct(org_lo, w['all_qty'])} … {pct(org_hi, w['all_qty'])} | {f0(w['t41'])} | {f0(w['t54'])} |"
              f" {pct(w['t41'] + w['t54'], w['all_amt'])} |")
    json.dump({m: {k: str(v) for k, v in w.items()} for m, w in weeks.items()}, open(path("table_by_week.json"), "w"), ensure_ascii=False, indent=1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["campaigns", "accrual", "accrual-summary", "orders", "analytics", "perf-daily", "cpo", "table"])
    ap.add_argument("--date-from"); ap.add_argument("--date-to"); ap.add_argument("--enable-date")
    args = ap.parse_args(argv)
    print(f"кабинет: {getattr(_PROFILE, 'DISPLAY_NAME', cabinet.cabinet_code())}")
    fn = {"campaigns": cmd_campaigns, "accrual": cmd_accrual, "accrual-summary": cmd_accrual_summary, "orders": cmd_orders,
          "analytics": cmd_analytics, "perf-daily": cmd_perf_daily, "cpo": cmd_cpo, "table": cmd_table}[args.step]
    fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
