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
import time
from collections import defaultdict
from decimal import Decimal

try:
    from loaders import http_retry
    from loaders import ozon_finance_accrual as accrual
except ImportError:  # пайплайн зовёт как скрипт
    import http_retry
    import ozon_finance_accrual as accrual

SALE_TYPE = 69
CHUNK = 200                      # номеров отправлений за вызов accrual/postings
PAUSE_SECONDS = 1.5
ANTISPAM_PAUSE_SECONDS = 60      # CLAUDE.md §2: не суточный лимит → пауза 60 с, не больше трёх попыток
ANTISPAM_MAX_ATTEMPTS = 3
BATCH = 500


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


def fetch_postings(numbers, counters):
    """accrual/postings пачками по CHUNK. counters: {'requests', '429'} — число обращений и пауз печатает вызывающий."""
    out = []
    for i in range(0, len(numbers), CHUNK):
        chunk = numbers[i:i + CHUNK]
        for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
            time.sleep(PAUSE_SECONDS)
            counters["requests"] += 1
            r = http_retry.post(f"{accrual.BASE}/v1/finance/accrual/postings", label="accrual/postings",
                                headers=accrual.headers(), json={"posting_numbers": chunk}, timeout=180)
            if r.status_code == 429:
                counters["429"] += 1
                print(f"accrual/postings: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS}", flush=True)
                time.sleep(ANTISPAM_PAUSE_SECONDS)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"accrual/postings: HTTP {r.status_code} {r.text[:200]}")
            out.extend((r.json() or {}).get("posting_accruals") or [])
            break
        else:
            raise RuntimeError(f"accrual/postings: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")
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
