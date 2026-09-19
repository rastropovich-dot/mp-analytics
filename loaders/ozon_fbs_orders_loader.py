import os
import time
import requests

try:
    from loaders import http_retry
    from loaders import ozon_orders_rows as rules
except ImportError:  # пайплайн зовёт как скрипт: python3 loaders/<файл>.py
    import http_retry
    import ozon_orders_rows as rules
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Прежнее имя оставлено: его импортируют другие модули.
to_local_order_date = rules.to_local_order_date


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


# Те же константы и та же механика, что у FBO на /v3 (loaders/ozon_fbo_orders_loader.py):
# страница 100 вместо 1000, пауза между страницами, антиспам 429 — 60 с и три
# попытки, предел страниц с падением вместо частичного результата. Дублируется
# намеренно: тесты обеих схем подменяют http_retry.post, time.sleep и MAX_PAGES
# в пространстве своего модуля.
PAGE_PAUSE_SECONDS = float(os.getenv("OZON_POSTING_PAGE_PAUSE_SECONDS", "1.5"))
ANTISPAM_PAUSE_SECONDS = 60
ANTISPAM_MAX_ATTEMPTS = 3
MAX_PAGES = 400
# Окно сбора 30 дней, как у FBO: отмены дозревают неделями, 14 дней их не ловили.
DEFAULT_DAYS_BACK = 30


def fbs_request(url, payload):
    """Запрос с пересиживанием антиспама."""
    for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
        response = http_retry.post(url, label="Ozon FBS postings",
                                   headers=ozon_headers(), json=payload, timeout=120)
        if response.status_code != 429:
            return response
        print(f"Ozon FBS: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, "
              f"попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS}")
        time.sleep(ANTISPAM_PAUSE_SECONDS)
    raise RuntimeError(f"Ozon FBS: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")


def get_ozon_fbs_postings(days_back=DEFAULT_DAYS_BACK, since=None, to=None):
    """Отправления FBS через /v4/posting/fbs/list.

    /v3/posting/fbs/list помечен на отключение 31.08.2026 (спека, deprecated: true).
    Отличия контракта: offset → cursor, result.postings → postings + cursor +
    has_next, страница 1000 → 100, цена стала объектом {amount, currency},
    created_at исчез (дата берётся из in_process_at, как и раньше).
    Сверка старого и нового на одном окне — scripts/ozon_fbs_v4_parity.py,
    docs/ozon_orders_one_rule.md.

    since/to — границы окна (datetime, UTC); без них — ночное окно: от локальной
    полуночи даты (сегодня − days_back) до «сейчас», чтобы первый день окна был
    целым (rules.nightly_window).
    """
    url = "https://api-seller.ozon.ru/v4/posting/fbs/list"
    night_from, night_to = rules.nightly_window(days_back)
    date_to = to or night_to
    date_from = since or night_from
    print(f"Ozon FBS: окно сбора {date_from.strftime('%Y-%m-%dT%H:%M:%S.000Z')} … {date_to.strftime('%Y-%m-%dT%H:%M:%S.000Z')}")

    postings, cursor, pages = [], "", 0
    limit = 100  # потолок метода, спека: maximum 100
    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise RuntimeError(
                f"Ozon FBS: превышен предел {MAX_PAGES} страниц — цикл не сходится"
            )
        payload = {
            "sort_dir": "ASC",
            "filter": {
                "since": date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to": date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            },
            "limit": limit,
            "with": {
                "analytics_data": True,
                "financial_data": True,
            },
        }
        if cursor:
            payload["cursor"] = cursor

        time.sleep(PAGE_PAUSE_SECONDS)
        response = fbs_request(url, payload)
        print(f"Ozon FBS postings страница {pages} HTTP status: {response.status_code}")
        if response.status_code != 200:
            print(response.text[:3000])
            raise RuntimeError(f"Ozon FBS: HTTP {response.status_code}")

        data = response.json() or {}
        postings.extend(data.get("postings") or [])
        cursor = data.get("cursor") or ""
        if not data.get("has_next"):
            break
        if not cursor:
            raise RuntimeError("Ozon FBS: has_next=true, но cursor пуст")

    print(f"Получено FBS postings: {len(postings)} за {pages} страниц")
    return postings


def build_order_rows(postings, observed_at=None):
    """Строки к записи по общему правилу обеих схем (loaders/ozon_orders_rows.py).

    До 2026-09-16 этот загрузчик статус не смотрел: отменённые лежали в
    orders_* вместе с доставленными (31,6 % по деньгам за 08-17…09-14).
    """
    rows, counters = rules.build_order_rows(postings, "fbs", observed_at=observed_at)
    rules.print_counters("fbs", counters)
    return rows


def save_ozon_orders(postings, observed_at=None):
    rows = build_order_rows(postings, observed_at=observed_at)
    if not rows:
        print("Нет Ozon FBS заказов для записи")
        return
    for i in range(0, len(rows), 500):
        supabase.table("marketplace_orders").upsert(
            rows[i:i + 500],
            on_conflict="order_date,marketplace_code,marketplace_sku,order_schema"
        ).execute()
    print(f"✅ Ozon FBS заказы записаны в marketplace_orders: {len(rows)} строк")


if __name__ == "__main__":
    postings = get_ozon_fbs_postings()
    print(f"Итого отправлений Ozon FBS: {len(postings)}")
    save_ozon_orders(postings)
