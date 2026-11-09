"""Штуки выкупов: quantity из /v1/finance/accrual/postings, сведённая построчно с by-day.

В by-day у товарной строки три поля — sku, delivery, commission; количества нет,
поэтому marketplace_buyouts.buyouts_qty считает ПОЗИЦИИ. Штуки есть в
accrual/postings: строка типа 69 SaleCommission несёт seller_price и quantity.
Сводим построчно, а не по суммам: строка by-day (отправление, sku, день) находит
свою строку типа 69 по равенствам

    seller_price × quantity = sale_amount     и     accrued = sale_commission

со знаком. Не нашла — строка НЕ угадывается: день и sku попадают в «несведённые»,
и штуки по этому ключу не пишутся вовсе (null = «не измерено», не ноль и не
позиции). Проверено 2026-09-16 на 46 510 строках: промахов 0.

Знак штук — как у позиций: продажа +, возврат − (по знаку sale_amount).
"""
import os
import time
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

try:
    from loaders import http_retry
    from loaders import ozon_finance_accrual as accrual
except ImportError:  # пайплайн зовёт как скрипт
    import http_retry
    import ozon_finance_accrual as accrual

SALE_TYPE = 69
CHUNK = 200                      # номеров отправлений за вызов accrual/postings (maxItems спеки — 200)
# Пауза перед каждым обращением к accrual/postings. 1,5 с — как было; запасной рычаг (вариант A разбора 08.11) — env, без правки кода:
# сухая история RBH O7 с паузой 12 с — 260 пачек, 0 антиспам-пауз.
PAUSE_SECONDS = float(os.getenv("OZON_BUYOUT_UNITS_PAUSE_SECONDS", "1.5"))
ANTISPAM_PAUSE_SECONDS = 60      # CLAUDE.md §2: не суточный лимит → пауза 60 с, не больше трёх попыток
ANTISPAM_MAX_ATTEMPTS = 3
BATCH = 500
# Вариант C (08.11): по accrual/postings спрашиваем только отправления ключей (день, sku), где штуки ещё не измерены
# (строки нет или buyouts_units null), и ключей последних RECENT_DAYS дней окна — начисления доезжают (правило двух суток).
# Измеренные ключи старше этих дней не запрашиваются и не перезаписываются. Замер rbh1 08.11: 24 549 отправлений → 123 обращения
# за окно 30 дней каждую ночь, хотя почти все ключи уже измерены.
RECENT_DAYS = int(os.getenv("OZON_BUYOUT_UNITS_RECENT_DAYS", "3"))
# Вариант D (08.11): бюджет шага. Ожидания по 429 (антиспам-паузы) — не больше WAIT_BUDGET_SECONDS за шаг, весь шаг — не дольше
# TIME_BUDGET_SECONDS; кончился — BudgetExhausted до следующего ожидания / обращения: стоп с причиной, ничего не пишется (null = «не измерено»).
WAIT_BUDGET_SECONDS = float(os.getenv("OZON_BUYOUT_UNITS_WAIT_BUDGET_SECONDS", "600"))
TIME_BUDGET_SECONDS = float(os.getenv("OZON_BUYOUT_UNITS_TIME_BUDGET_SECONDS", "1800"))


class BudgetExhausted(Exception):
    """Бюджет шага кончился. Не RuntimeError: шаг останавливается с причиной и ничего не пишет, а не падает."""


class Budget:
    """Бюджет ожиданий и времени шага; clock подставляется в тестах."""

    def __init__(self, wait_seconds=None, time_seconds=None, clock=time.monotonic, started=None):
        self.wait_seconds = WAIT_BUDGET_SECONDS if wait_seconds is None else float(wait_seconds)
        self.time_seconds = TIME_BUDGET_SECONDS if time_seconds is None else float(time_seconds)
        self.clock = clock
        self.started = clock() if started is None else started
        self.waited = 0.0

    def elapsed(self):
        return self.clock() - self.started

    def check_time(self, upcoming=0.0, what="обращение"):
        if self.elapsed() + upcoming > self.time_seconds:
            raise BudgetExhausted(f"время шага {self.elapsed():.0f} с + {upcoming:g} с на {what} > бюджета {self.time_seconds:.0f} с")

    def wait(self, seconds, sleep_fn=None):
        if self.waited + seconds > self.wait_seconds:
            raise BudgetExhausted(f"ожидания по 429 уже {self.waited:.0f} с + {seconds:g} с > бюджета {self.wait_seconds:.0f} с")
        self.check_time(seconds, "паузу по 429")
        (sleep_fn or time.sleep)(seconds)
        self.waited += seconds

    def line(self):
        return (f"бюджет шага: ожиданий по 429 {self.waited:.0f} из {self.wait_seconds:.0f} с, "
                f"время {self.elapsed():.0f} из {self.time_seconds:.0f} с")


def D(v):
    return Decimal(str(v))


def sold_postings(accruals):
    """Номера отправлений с продажей или возвратом — те же товарные строки, что берёт build_buyout_rows."""
    out, seen = [], set()
    for a in accruals:
        if a.get("accrued_category") != "POSTING" or not a.get("posting"):
            continue
        for product in a["posting"].get("products") or []:
            commission = product.get("commission") or {}
            if not commission:
                continue
            if accrual.money(commission.get("sale_amount")) == 0 and accrual.money(commission.get("sale_commission")) == 0:
                continue
            number = a.get("unit_number")
            if number and number not in seen:
                seen.add(number)
                out.append(number)
            break
    return out


def _product_keys(a):
    """Ключи (день, sku) товарных строк начисления — тот же отбор, что у units_by_key (строки с продажей / возвратом)."""
    day = str(a.get("date") or "")[:10]
    for product in (a.get("posting") or {}).get("products") or []:
        commission = product.get("commission") or {}
        if not commission:
            continue
        if accrual.money(commission.get("sale_amount")) == 0 and accrual.money(commission.get("sale_commission")) == 0:
            continue
        yield (day, str(product.get("sku") or ""))


def select_postings(accruals, existing, last_day, recent_days=None):
    """Вариант C: какие отправления спрашивать у accrual/postings.

    Ключ (день, sku) нужно измерять, если штук нет (строки выкупа нет или buyouts_units null) или день — в последних
    recent_days днях окна (последний день — last_day). Спрашиваем все отправления, у которых есть хоть один такой ключ:
    штуки ключа складываются из всех его отправлений. Возвращает (номера в порядке sold_postings, ключи к измерению, счётчики).
    """
    recent_days = RECENT_DAYS if recent_days is None else recent_days
    recent_from = (date.fromisoformat(last_day) - timedelta(days=max(recent_days, 1) - 1)).isoformat()
    keys_by_posting = defaultdict(set)
    for a in accruals:
        if a.get("accrued_category") == "POSTING" and a.get("posting") and a.get("unit_number"):
            keys_by_posting[a["unit_number"]].update(_product_keys(a))
    all_keys = {k for ks in keys_by_posting.values() for k in ks}
    need = {k for k in all_keys if existing.get(k) is None or k[0] >= recent_from}
    every = sold_postings(accruals)
    numbers = [n for n in every if keys_by_posting.get(n, set()) & need]
    counters = {"postings_total": len(every), "postings_need": len(numbers), "keys_total": len(all_keys), "keys_need": len(need),
                "keys_measured_skipped": len(all_keys - need), "recent_from": recent_from}
    return numbers, need, counters


def absorb_retry_stats(counters, stats):
    """Счётчики одного вызова http_retry → счётчики шага. Повтор с причиной 429 — тоже 429."""
    counters["http"] += stats.get("requests", 0)
    counters["retries"] += stats.get("retries", 0)
    for reason, n in (stats.get("reasons") or {}).items():
        counters["reasons"][reason] = counters["reasons"].get(reason, 0) + n
        if http_retry.is_429_reason(reason):
            counters["429"] += n


def ratelimit_line(counters):
    return (f"  ratelimit-remaining: в ответах {counters.get('ratelimit_seen', 0)}, min {counters.get('ratelimit_remaining_min', '—')}, "
            f"нулей {counters.get('ratelimit_zero', 0)}; ожиданий до запроса {counters.get('ratelimit_waits', 0)} "
            f"({counters.get('ratelimit_wait_seconds', 0)} с) — отдельно от 429")


def fetch_postings(numbers, counters, budget=None):
    """accrual/postings пачками по CHUNK. counters — печатает вызывающий:

        requests        обращений шага (пачка × попытка антиспама)
        http            HTTP-попыток всего, включая повторы внутри http_retry
        retries         повторов http_retry; reasons — их причины (rate_limit_per_second и т. п.)
        antispam_429    429, дошедших до шага после повторов http_retry, — пауза ANTISPAM_PAUSE_SECONDS
        429             ВСЕ 429 шага: antispam_429 + повторы http_retry с причиной 429

    Прежде шаг считал только antispam_429 и печатал «пауз 429 — 0» при десяти 429, которые http_retry
    повторил сам (ручной прогон 2026-09-23): ложный ноль — тот же класс, что тихий ноль.
    """
    for key in ("requests", "http", "retries", "antispam_429", "429"):
        counters.setdefault(key, 0)
    counters.setdefault("reasons", {})
    budget = budget or Budget()
    out = []
    try:
        for i in range(0, len(numbers), CHUNK):
            chunk = numbers[i:i + CHUNK]
            for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
                budget.check_time(PAUSE_SECONDS, "паузу и обращение")
                time.sleep(PAUSE_SECONDS)
                counters["requests"] += 1
                stats = {}
                r = http_retry.post(f"{accrual.BASE}/v1/finance/accrual/postings", label="accrual/postings",
                                    headers=accrual.headers(), json={"posting_numbers": chunk}, timeout=180, stats=stats)
                absorb_retry_stats(counters, stats)
                http_retry.absorb_ratelimit(counters, stats)
                if r.status_code == 429:
                    counters["antispam_429"] += 1
                    counters["429"] += 1
                    print(f"accrual/postings: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS}", flush=True)
                    budget.wait(ANTISPAM_PAUSE_SECONDS)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"accrual/postings: HTTP {r.status_code} {r.text[:200]}")
                out.extend((r.json() or {}).get("posting_accruals") or [])
                break
            else:
                raise RuntimeError(f"accrual/postings: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")
    finally:
        # до выхода при любом исходе: замер 08.11 оборвался исключением, и строка remaining из run не напечаталась
        print(ratelimit_line(counters) + "; " + budget.line(), flush=True)
    return out


def sale_index(postings):
    """(отправление, sku, день) → строки типа 69."""
    idx = defaultdict(list)
    for p in postings:
        for a in p.get("accruals") or []:
            if a.get("type_id") == SALE_TYPE:
                idx[(p.get("posting_number"), str(a.get("sku")), str(a.get("accrual_date") or "")[:10])].append(a)
    return idx


def units_by_key(accruals, postings):
    """{(день, sku): штуки нетто} и счётчики. Ключ, где хоть одна строка не сведена, в результат НЕ попадает."""
    idx = sale_index(postings)
    used = set()
    units, positions = defaultdict(int), defaultdict(int)
    unmatched = defaultdict(int)
    c = defaultdict(int)
    for a in accruals:
        if a.get("accrued_category") != "POSTING" or not a.get("posting"):
            continue
        day = str(a.get("date") or "")[:10]
        number = a.get("unit_number")
        for product in a["posting"].get("products") or []:
            commission = product.get("commission") or {}
            if not commission:
                continue
            sale_amount = D((commission.get("sale_amount") or {}).get("amount") or 0)
            sale_commission = D((commission.get("sale_commission") or {}).get("amount") or 0)
            if sale_amount == 0 and sale_commission == 0:
                continue
            sign = 1 if sale_amount >= 0 else -1
            sku = str(product.get("sku") or "")
            key = (day, sku)
            c["rows"] += 1
            positions[key] += sign
            hit = None
            for line in idx.get((number, sku, day), []):
                if id(line) in used:
                    continue
                price, qty = line.get("seller_price"), line.get("quantity") or 0
                if price is not None and D(price.get("amount") or 0) * qty == sale_amount and D((line.get("accrued") or {}).get("amount") or 0) == sale_commission:
                    hit = line
                    break
            if hit is None:
                c["unmatched_rows"] += 1
                unmatched[key] += 1
                continue
            used.add(id(hit))
            c["matched_rows"] += 1
            units[key] += sign * int(hit["quantity"])
            if int(hit["quantity"]) >= 2:
                c["rows_with_2_plus"] += 1
    clean = {k: v for k, v in units.items() if k not in unmatched}
    # ключ, у которого все строки не сведены, в units не появился вовсе — он тоже «не измерен»
    c["keys"] = len(positions)
    c["keys_measured"] = len(clean)
    c["keys_unmeasured"] = len(positions) - len(clean)
    c["units"] = sum(clean.values())
    c["positions_of_measured"] = sum(positions[k] for k in clean)
    return clean, dict(c), dict(unmatched), dict(positions)


def read_existing_keys(supabase, date_from, date_to, with_units=True):
    """{(день, sku): buyouts_units} строк выкупов Ozon за окно. Чтение по ключу id, с сортировкой.

    with_units=False — только ключи (значения None): для плана посева ДО миграции, когда колонки ещё нет.
    """
    out, last = {}, None
    columns = "id,buyout_date,marketplace_sku" + (",buyouts_units" if with_units else "")
    while True:
        q = (supabase.table("marketplace_buyouts").select(columns)
             .eq("marketplace_code", "ozon").gte("buyout_date", date_from).lte("buyout_date", date_to).order("id").limit(1000))
        if last is not None:
            q = q.gt("id", last)
        page = q.execute().data or []
        for r in page:
            out[(r["buyout_date"], str(r["marketplace_sku"]))] = r.get("buyouts_units")
        if len(page) < 1000:
            return out
        last = page[-1]["id"]


def plan_update(units, existing):
    """Что писать: только существующие строки и только там, где значение меняется."""
    to_write = {k: v for k, v in units.items() if k in existing and existing[k] != v}
    same = sum(1 for k, v in units.items() if k in existing and existing[k] == v)
    units_without_row = sorted(k for k in units if k not in existing)
    rows_without_units = sorted(k for k in existing if k not in units)
    return to_write, same, units_without_row, rows_without_units


def write_units(supabase, to_write):
    """Пишем ТОЛЬКО колонку buyouts_units: upsert по ключу с тремя ключевыми полями и штуками.

    PostgREST при конфликте обновляет лишь присланные колонки, остальные не трогает. Ключи
    заранее отобраны среди существующих (plan_update), поэтому новых строк upsert не создаёт.
    """
    rows = [{"buyout_date": d, "marketplace_code": "ozon", "marketplace_sku": s, "buyouts_units": v} for (d, s), v in sorted(to_write.items())]
    for i in range(0, len(rows), BATCH):
        supabase.table("marketplace_buyouts").upsert(rows[i:i + BATCH], on_conflict="buyout_date,marketplace_code,marketplace_sku").execute()
    return len(rows)
