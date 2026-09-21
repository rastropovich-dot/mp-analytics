"""Этап 1 миграции заказов: сверка старого и нового методов. В БД НЕ ПИШЕТ.

/v2/posting/fbo/list и /v3/posting/fbs/list помечены на отключение 31.08.2026.
Замены — /v3/posting/fbo/list и /v4/posting/fbs/list — меняют постраничность
со смещения на курсор, поэтому сравнивать надо МНОЖЕСТВА posting_number, а не
их количество: при потерянной странице и лишних дублях количество совпадёт.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

try:
    from loaders import http_retry
except ImportError:
    sys.path.insert(0, "loaders")
    import http_retry

BASE = "https://api-seller.ozon.ru"
# Предел итераций: страниц по 1000 на сутки заказов заведомо меньше сотни.
# Достигли предела — значит цикл не сходится, и частичный результат вреднее
# ошибки: он выглядит как полный.
MAX_PAGES = 400   # страница новых методов 100 записей, запас на сутки заказов
# Пауза между обращениями: методы заказов упираются в частотный лимит
# («request rate limit per second», код 8) при вызовах подряд, и ретраев
# http_retry не хватает. Сверка не срочная, платить секундой за страницу дешевле,
# чем ловить отказы.
PAUSE_SECONDS = float(os.getenv("OZON_POSTING_PAUSE_SECONDS", "3"))
# Антиспам Ozon отпускает сам за несколько минут (сообщение поддержки), и
# ретраи http_retry с потолком в 10 секунд его не пересиживают. Правило то же,
# что для Performance: пауза 60 с, не больше трёх попыток, потом ошибка.
ANTISPAM_PAUSE = 60
ANTISPAM_MAX = 3


def call(path, body):
    """Запрос с пересиживанием антиспама. Ошибка вместо частичного результата."""
    for attempt in range(1, ANTISPAM_MAX + 1):
        time.sleep(PAUSE_SECONDS)
        r = http_retry.post(f"{BASE}{path}", label=path, headers=headers(), json=body, timeout=120)
        if r.status_code != 429:
            return r
        print(f"    429 на {path}, пауза {ANTISPAM_PAUSE} с, попытка {attempt}/{ANTISPAM_MAX}",
              flush=True)
        time.sleep(ANTISPAM_PAUSE)
    raise RuntimeError(f"{path}: 429 не прошёл за {ANTISPAM_MAX} попыток")


def headers():
    return {"Client-Id": os.getenv("OZON_CLIENT_ID"),
            "Api-Key": os.getenv("OZON_API_KEY"),
            "Content-Type": "application/json"}


def window(day):
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))


def fetch_offset(path, day, limit=1000):
    """Старый способ: offset. Потолок страницы 1000 (спека)."""
    since, to = window(day)
    out, offset, pages = [], 0, 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise RuntimeError(f"{path}: превышен предел {MAX_PAGES} страниц на {day}")
        body = {"dir": "ASC", "limit": limit, "offset": offset, "translit": True,
                "filter": {"since": since, "to": to, "status": ""},
                "with": {"analytics_data": True, "financial_data": True}}
        r = call(path, body)
        if r.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        data = r.json() or {}
        batch = data.get("result")
        if isinstance(batch, dict):          # v3 FBS кладёт список в result.postings
            batch = batch.get("postings") or []
        batch = batch or []
        out.extend(batch)
        if len(batch) < limit:
            return out, pages
        offset += limit


def fetch_cursor(path, day, limit=100):
    """Новый способ: cursor. Выход по has_next, а не по размеру страницы.

    ВНИМАНИЕ: потолок страницы у новых методов 100, а не 1000 — спека,
    `maximum: 100` у posting.v3.PostingFboListRequest.limit и
    posting.v4.PostingFbsListRequest.limit. Страниц станет вдесятеро больше,
    и это надо учитывать вместе с частотным лимитом.
    """
    since, to = window(day)
    out, cursor, pages = [], "", 0
    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise RuntimeError(f"{path}: превышен предел {MAX_PAGES} страниц на {day}")
        body = {"sort_dir": "ASC", "limit": limit, "translit": True,
                "filter": {"since": since, "to": to, "status": ""},
                "with": {"analytics_data": True, "financial_data": True}}
        if cursor:
            body["cursor"] = cursor
        r = call(path, body)
        if r.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        data = r.json() or {}
        out.extend(data.get("postings") or [])
        cursor = data.get("cursor") or ""
        if not data.get("has_next"):
            return out, pages
        if not cursor:
            raise RuntimeError(f"{path}: has_next=true, но cursor пуст на {day}")


READ_FIELDS = ("posting_number", "status", "created_at", "in_process_at", "products")
PRODUCT_FIELDS = ("sku", "offer_id", "name", "quantity", "price")

# Значения, а не только наличие: совпадение множеств posting_number ничего не
# говорит о том, что внутри. Цена и статус — те поля, расхождение в которых
# означает стоп: на них стоят выручка и признак состоявшегося заказа.
VALUE_FIELDS = ("status", "in_process_at", "shipment_date")
VALUE_PRODUCT_FIELDS = ("sku", "offer_id", "quantity", "price")
CRITICAL = ("status", "price")


def product_key(prod):
    return str(prod.get("sku") or prod.get("product_id") or prod.get("offer_id") or "")


def diff_values(old_list, new_list):
    """Список отличий значений по общим posting_number."""
    old_by = {str(p.get("posting_number")): p for p in old_list}
    new_by = {str(p.get("posting_number")): p for p in new_list}
    out = []
    for pn in sorted(set(old_by) & set(new_by)):
        o, n = old_by[pn], new_by[pn]
        for f in VALUE_FIELDS:
            ov, nv = o.get(f), n.get(f)
            if ov != nv:
                out.append({"posting_number": pn, "field": f, "old": ov, "new": nv,
                            "critical": f in CRITICAL})
        op = {product_key(x): x for x in (o.get("products") or [])}
        np_ = {product_key(x): x for x in (n.get("products") or [])}
        for sku in sorted(set(op) | set(np_)):
            if sku not in op or sku not in np_:
                out.append({"posting_number": pn, "field": f"products[{sku}]",
                            "old": "есть" if sku in op else "нет",
                            "new": "есть" if sku in np_ else "нет", "critical": True})
                continue
            for f in VALUE_PRODUCT_FIELDS:
                ov, nv = op[sku].get(f), np_[sku].get(f)
                if f in ("quantity", "price"):
                    try:
                        same = abs(float(ov or 0) - float(nv or 0)) < 0.005
                    except (TypeError, ValueError):
                        same = ov == nv
                else:
                    same = str(ov or "") == str(nv or "")
                if not same:
                    out.append({"posting_number": pn, "field": f"products.{f}", "sku": sku,
                                "old": ov, "new": nv, "critical": f in CRITICAL})
    return out


def compare(day, old_path, new_path):
    old, old_pages = fetch_offset(old_path, day)
    new, new_pages = fetch_cursor(new_path, day)
    so = {str(p.get("posting_number")) for p in old}
    sn = {str(p.get("posting_number")) for p in new}
    missing_new = sorted(so - sn)
    missing_old = sorted(sn - so)
    # поля, которые читает наш код
    miss_fields = set()
    for p in new:
        for f in READ_FIELDS:
            if f not in p:
                miss_fields.add(f)
        for prod in (p.get("products") or []):
            for f in PRODUCT_FIELDS:
                if f not in prod:
                    miss_fields.add(f"products.{f}")
    value_diffs = diff_values(old, new)
    return {"day": day, "old": len(old), "new": len(new), "value_diffs": value_diffs,
            "old_unique": len(so), "new_unique": len(sn),
            "old_pages": old_pages, "new_pages": new_pages,
            "missing_in_new": missing_new, "missing_in_old": missing_old,
            "missing_fields": sorted(miss_fields)}


def main():
    ap = argparse.ArgumentParser(description="Сверка старого и нового методов заказов. В БД не пишет.")
    ap.add_argument("--scheme", choices=["fbo", "fbs"], required=True)
    ap.add_argument("--dates", required=True, help="через запятую")
    args = ap.parse_args()
    old_path, new_path = (("/v2/posting/fbo/list", "/v3/posting/fbo/list") if args.scheme == "fbo"
                          else ("/v3/posting/fbs/list", "/v4/posting/fbs/list"))
    print(f"{old_path}  ->  {new_path}")
    print(f"db_writes = 0\n")
    print(f"{'дата':<12}{'старый':>8}{'новый':>8}{'уник ст':>9}{'уник нов':>10}"
          f"{'стр ст':>8}{'стр нов':>9}  вердикт")
    bad = 0
    for day in [d.strip() for d in args.dates.split(",") if d.strip()]:
        r = compare(day, old_path, new_path)
        ok = (not r["missing_in_new"] and not r["missing_in_old"]
              and not r["missing_fields"] and not r["value_diffs"])
        bad += 0 if ok else 1
        print(f"{r['day']:<12}{r['old']:>8}{r['new']:>8}{r['old_unique']:>9}{r['new_unique']:>10}"
              f"{r['old_pages']:>8}{r['new_pages']:>9}  {'✅' if ok else '❌'}")
        if r["missing_in_new"]:
            print(f"    нет в новом ({len(r['missing_in_new'])}): {r['missing_in_new'][:5]}")
        if r["missing_in_old"]:
            print(f"    нет в старом ({len(r['missing_in_old'])}): {r['missing_in_old'][:5]}")
        if r["missing_fields"]:
            print(f"    отсутствуют поля: {r['missing_fields']}")
        if r["value_diffs"]:
            crit = [d for d in r["value_diffs"] if d.get("critical")]
            print(f"    ОТЛИЧАЮТСЯ ЗНАЧЕНИЯ: {len(r['value_diffs'])}, "
                  f"из них критичных (status/price): {len(crit)}")
            for d in r["value_diffs"][:10]:
                sku = f" sku={d['sku']}" if d.get("sku") else ""
                mark = " ← КРИТИЧНО" if d.get("critical") else ""
                print(f"      {d['posting_number']}{sku} {d['field']}: "
                      f"{d['old']!r} -> {d['new']!r}{mark}")
    print(f"\nдат с расхождением: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
