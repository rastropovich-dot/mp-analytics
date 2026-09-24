"""Отчёт ЛК «Заказы со склада Ozon / с моих складов» через /v1/report/postings/create — цена покупателя для FBO.

В ночном списке /v3/posting/fbo/list нет customer_price (есть в /v4 FBS). Формула владельца («Соинвест, %» листа «Заказы»,
тридцать пятая задача §4) требует «Оплачено покупателем» по обеим схемам — отчёт ЛК отдаёт его колонкой за единицу.

    prices, stats = fetch_buyer_prices("fbo", since_utc, to_utc)   # {(posting_number, sku): Decimal за единицу}

Порядок: create (filter.delivery_schema одно значение, processed_at_from/to — UTC, для fbo `with` пустой — иначе 400) →
/v1/report/info раз в POLL_SECONDS, не дольше MAX_WAIT_SECONDS (иначе RuntimeError с именем) → скачивание по ссылке
(CSV с BOM и `;`, хотя спека обещает XLSX — 2026-09-23 отдавался CSV) → разбор. Файл кладётся в data/ozon_report_postings/
(вне git; на Render диска нет — живёт до конца прогона). Обращений: 1 create + N info + 1 download, 429 — через http_retry.
"""
import csv
import io
import os
import time
from decimal import Decimal, InvalidOperation

import requests

from . import http_retry

BASE = "https://api-seller.ozon.ru"
POLL_SECONDS = 5
MAX_WAIT_SECONDS = 180
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ozon_report_postings")


def headers():
    return {"Client-Id": os.getenv("OZON_CLIENT_ID", ""), "Api-Key": os.getenv("OZON_API_KEY", ""), "Content-Type": "application/json"}


def money(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def parse_report(text):
    """CSV отчёта → {(posting_number, sku): цена покупателя за единицу}. Строка без цены — пропускается и считается."""
    prices, skipped = {}, 0
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"):
        paid = money(r.get("Оплачено покупателем"))
        posting, sku = str(r.get("Номер отправления") or "").strip(), str(r.get("SKU") or "").strip()
        if paid is None or not posting or not sku:
            skipped += 1
            continue
        prices[(posting, sku)] = paid
    return prices, skipped


def fetch_buyer_prices(scheme, since_utc, to_utc, sleep_fn=time.sleep, post=None, get=None, save_dir=OUT_DIR, clock=time.monotonic):
    """Отчёт ЛК за окно → (цены, статистика). Отказ — RuntimeError с причиной; вызывающий решает, что делать без цен."""
    post = post or (lambda path, body: http_retry.post(f"{BASE}{path}", label=f"report{path}", headers=headers(), json=body, timeout=120))
    get = get or (lambda url: requests.get(url, timeout=300))
    stats = {"create": 0, "info": 0, "download": 0, "rows": 0, "skipped": 0, "seconds": 0.0}
    body = {"filter": {"delivery_schema": [scheme], "processed_at_from": since_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "processed_at_to": to_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}, "language": "RU"}
    body["with"] = {"additional_data": True} if scheme == "fbs" else {}   # у fbo additional_data → 400 (проба 2026-09-23)
    started = clock()
    r = post("/v1/report/postings/create", body)
    stats["create"] += 1
    if r.status_code != 200:
        raise RuntimeError(f"report/postings/create {scheme}: HTTP {r.status_code} {r.text[:160]}")
    code = ((r.json() or {}).get("result") or {}).get("code")
    if not code:
        raise RuntimeError(f"report/postings/create {scheme}: в ответе нет code")
    status, info = None, {}
    while clock() - started < MAX_WAIT_SECONDS:
        sleep_fn(POLL_SECONDS)
        ri = post("/v1/report/info", {"code": code})
        stats["info"] += 1
        if ri.status_code != 200:
            raise RuntimeError(f"report/info {scheme}: HTTP {ri.status_code} {ri.text[:160]}")
        info = (ri.json() or {}).get("result") or {}
        status = info.get("status")
        if status == "success":
            break
        if status in ("failed", "error"):
            raise RuntimeError(f"report/info {scheme}: status={status} error={info.get('error')!r}")
    if status != "success":
        raise RuntimeError(f"report/info {scheme}: отчёт не готов за {MAX_WAIT_SECONDS} с (status={status})")
    link = info.get("file")
    if not link:
        raise RuntimeError(f"report/info {scheme}: success без ссылки на файл")
    data = get(link)
    stats["download"] += 1
    if data.status_code != 200:
        raise RuntimeError(f"скачивание отчёта {scheme}: HTTP {data.status_code}")
    text = data.content.decode("utf-8")
    try:
        os.makedirs(save_dir, exist_ok=True)
        with open(os.path.join(save_dir, f"nightly_{scheme}_{to_utc.strftime('%Y%m%dT%H%M%SZ')}.csv"), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    prices, skipped = parse_report(text)
    stats.update({"rows": len(prices) + skipped, "skipped": skipped, "seconds": round(clock() - started, 1)})
    return prices, stats
