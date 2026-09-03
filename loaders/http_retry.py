"""Повтор транзиентных HTTP-отказов для загрузчиков.

Зачем отдельный модуль. Ночь на 2026-09-03: один 429 «request rate limit per
second» на первом же запросе Seller Analytics уронил весь пайплайн и оставил
день без витрины. Ретраев не было ни в одном загрузчике, кроме Performance.

ГРАНИЦА, которую нельзя стирать. 429 бывают двух разных природ:

* **Суточная квота** — Performance API, `/api/client/statistics/json`. Повтор
  бессмыслен: до сброса окна ответ не изменится. Действует правило CLAUDE.md
  «стоп на первом 429, без retry storm». Этот модуль к Performance НЕ
  применяется и применяться не должен: там своя классификация в
  loaders/ozon_performance_ads_loader.py (RateLimitPending, fail_fast_on_429).

* **Частота запросов** — Seller API Ozon (`"code": 8`) и WB. Отказ
  транзиентный: тот же запрос через паузу проходит. Здесь повтор и нужен.

Поэтому политика 429 задаётся вызывающим, а не угадывается: Ozon Seller
повторяет только `code 8`, WB — любой 429.

Помощник НЕ меняет контракт вызывающего: если попытки исчерпаны, он возвращает
последний ответ, и загрузчик поступает с ним ровно так же, как поступал раньше.
Ничего не проглатывается сверх того, что проглатывалось до этого.
"""

import time

import requests

MAX_ATTEMPTS = 4
BASE_SLEEP_SECONDS = 1
CAP_SLEEP_SECONDS = 10
OZON_RATE_LIMIT_PER_SECOND_CODE = 8

RETRY_429_OZON_RATE_LIMIT = "ozon_code_8"
RETRY_429_ANY = "any"
RETRY_429_NEVER = "never"


def _error_code(response):
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    try:
        return int(body.get("code"))
    except (TypeError, ValueError):
        return None


def classify(response, retry_429=RETRY_429_OZON_RATE_LIMIT):
    """(повторять?, причина). Причина идёт в лог, чтобы решение было видно."""
    status = int(response.status_code)
    if status == 200:
        return False, "ok"
    if 500 <= status <= 599:
        return True, f"http_{status}"
    if status == 429:
        if retry_429 == RETRY_429_ANY:
            return True, "rate_limit"
        if retry_429 == RETRY_429_OZON_RATE_LIMIT:
            code = _error_code(response)
            if code == OZON_RATE_LIMIT_PER_SECOND_CODE:
                return True, "rate_limit_per_second"
            return False, f"429_code_{code}"
        return False, "429_retry_disabled"
    return False, f"http_{status}"


def sleep_seconds(response, attempt):
    """Пауза до следующей попытки. Retry-After имеет приоритет над backoff."""
    retry_after = (getattr(response, "headers", None) or {}).get("Retry-After")
    if retry_after is not None:
        try:
            return max(0, min(CAP_SLEEP_SECONDS, int(float(str(retry_after).strip()))))
        except (TypeError, ValueError):
            pass
    return min(CAP_SLEEP_SECONDS, BASE_SLEEP_SECONDS * (2 ** max(attempt - 1, 0)))


def request(method, url, *, label, retry_429=RETRY_429_OZON_RATE_LIMIT,
            max_attempts=MAX_ATTEMPTS, sleep_fn=time.sleep, session=None, **kwargs):
    """Как requests.request, но с повтором транзиентных отказов.

    Возвращает последний ответ. Не бросает по коду ответа — решение о том,
    что делать с не-200, остаётся за вызывающим.
    """
    caller = session or requests
    response = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        response = caller.request(method, url, **kwargs)
        retryable, reason = classify(response, retry_429=retry_429)
        if not retryable or attempt >= max_attempts:
            if reason != "ok" and attempt > 1:
                print(f"{label}: транзиентный отказ не изжит за {attempt} попыток (reason={reason})")
            return response
        pause = sleep_seconds(response, attempt)
        print(f"{label}: транзиентный отказ reason={reason} "
              f"attempt={attempt}/{max_attempts} sleep={pause}s")
        sleep_fn(pause)
    return response


def post(url, **kwargs):
    return request("POST", url, **kwargs)


def get(url, **kwargs):
    return request("GET", url, **kwargs)
