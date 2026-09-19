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
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")


def ozon_headers():
    return {
        "Client-Id": OZON_CLIENT_ID,
        "Api-Key": OZON_API_KEY,
        "Content-Type": "application/json",
    }


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def to_local_order_date(value):
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(ZoneInfo(APP_TIMEZONE)).date().isoformat()


# Пауза между страницами: новый метод отдаёт по 100 записей вместо 1000, то есть
# обращений становится вдесятеро больше, и вызовы подряд упираются в частотный
# лимит («request rate limit per second», код 8).
PAGE_PAUSE_SECONDS = float(os.getenv("OZON_POSTING_PAGE_PAUSE_SECONDS", "1.5"))
# Антиспам Ozon отпускает сам за несколько минут (сообщение поддержки), ретраи
# http_retry с потолком 10 с его не пересиживают.
ANTISPAM_PAUSE_SECONDS = 60
ANTISPAM_MAX_ATTEMPTS = 3
# Предел страниц: 100 записей на страницу, сутки заказов — заведомо меньше.
# Достигли предела — падаем. Частичный результат вреднее ошибки: он выглядит
# как полный и молча занижает заказы.
MAX_PAGES = 400


def posting_price(product):
    """Цена товара. В /v3 это объект {amount, currency}, в /v2 была строка.

    Округления нет: сверка 981 пары на четырёх датах дала 0 расхождений по
    Decimal. Но валюту проверяем — чужая валюта здесь означает, что мы считаем
    рубли там, где их нет, и молчать об этом нельзя.
    """
    raw = product.get("price")
    if isinstance(raw, dict):
        currency = str(raw.get("currency") or "")
        if currency and currency != "RUB":
            raise RuntimeError(
                f"Ozon FBO: цена не в рублях — currency={currency}, "
                f"sku={product.get('sku')}, price={raw}"
            )
        if raw.get("amount") in (None, ""):
            # Объект без amount — не «бесплатный товар», а сломанный контракт.
            # Молчаливый ноль занизил бы выручку по всем заказам; падаем.
            raise RuntimeError(
                f"Ozon FBO: в цене нет amount — sku={product.get('sku')}, price={raw}"
            )
        raw = raw["amount"]
    return float(raw or 0)


def fbo_request(url, payload):
    """Запрос с пересиживанием антиспама."""
    for attempt in range(1, ANTISPAM_MAX_ATTEMPTS + 1):
        response = http_retry.post(url, label="Ozon FBO postings",
                                   headers=ozon_headers(), json=payload, timeout=120)
        if response.status_code != 429:
            return response
        print(f"Ozon FBO: 429, пауза {ANTISPAM_PAUSE_SECONDS} с, "
              f"попытка {attempt}/{ANTISPAM_MAX_ATTEMPTS}")
        time.sleep(ANTISPAM_PAUSE_SECONDS)
    raise RuntimeError(f"Ozon FBO: 429 не прошёл за {ANTISPAM_MAX_ATTEMPTS} попыток")


def get_fbo_postings(days_back=30, since=None, to=None):
    """Заказы FBO через /v3/posting/fbo/list.

    /v2/posting/fbo/list помечен на отключение 31.08.2026. Отличия контракта:
    offset -> cursor, result -> postings, страница 1000 -> 100, цена стала
    объектом. Сверка старого и нового на четырёх датах: множества
    posting_number совпали в обе стороны, значения полей идентичны.
    См. docs/ozon_postings_migration.md.

    since/to — границы окна (datetime, UTC) для пересборки истории; без них —
    ночное окно: от локальной полуночи даты (сегодня − days_back) до «сейчас»,
    чтобы первый день окна был целым (rules.nightly_window).
    """
    url = "https://api-seller.ozon.ru/v3/posting/fbo/list"

    night_from, night_to = rules.nightly_window(days_back)
    date_to = to or night_to
    date_from = since or night_from
    print(f"Ozon FBO: окно сбора {date_from.strftime('%Y-%m-%dT%H:%M:%S.000Z')} … {date_to.strftime('%Y-%m-%dT%H:%M:%S.000Z')}")

    postings = []
    limit = 100          # потолок метода, спека: maximum 100
    cursor = ""
    pages = 0

    while True:
        pages += 1
        if pages > MAX_PAGES:
            raise RuntimeError(
                f"Ozon FBO: превышен предел {MAX_PAGES} страниц — цикл не сходится"
            )
        payload = {
            "sort_dir": "ASC",
            "filter": {
                "since": date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to": date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "status": "",
            },
            "limit": limit,
            "translit": True,
            "with": {
                "analytics_data": True,
                "financial_data": True,
            },
        }
        if cursor:
            payload["cursor"] = cursor

        time.sleep(PAGE_PAUSE_SECONDS)
        response = fbo_request(url, payload)
        print(f"Ozon FBO postings страница {pages} HTTP status: {response.status_code}")

        if response.status_code != 200:
            print(response.text[:3000])
            raise RuntimeError(f"Ozon FBO: HTTP {response.status_code}")

        data = response.json() or {}
        postings.extend(data.get("postings") or [])
        cursor = data.get("cursor") or ""

        if not data.get("has_next"):
            break
        if not cursor:
            raise RuntimeError("Ozon FBO: has_next=true, но cursor пуст")

    print(f"Получено FBO postings: {len(postings)} за {pages} страниц")
    return postings


def parse_date(value):
    if not value:
        return None
    return to_local_order_date(value)


def build_order_rows(postings, observed_at=None):
    """Строки к записи по общему правилу обеих схем (loaders/ozon_orders_rows.py).

    До 2026-09-16 отменённые здесь выбрасывались, и ключ, у которого отправлений
    не осталось, жил в таблице навсегда. Теперь отменённые идут в
    cancelled_orders_*, подтверждённые — в orders_*, каждый ключ окна
    переписывается целиком.
    """
    rows, counters = rules.build_order_rows(postings, "fbo", observed_at=observed_at)
    rules.print_counters("fbo", counters)
    return rows


def save_orders(rows):
    if not rows:
        print("Нет FBO заказов для записи")
        return

    for batch in chunks(rows, 500):
        supabase.table("marketplace_orders").upsert(
            batch,
            on_conflict="order_date,marketplace_code,marketplace_sku,order_schema"
        ).execute()

    print(f"✅ Ozon FBO orders записаны в marketplace_orders: {len(rows)} строк")


if __name__ == "__main__":
    postings = get_fbo_postings(days_back=30)
    rows = build_order_rows(postings)
    save_orders(rows)
