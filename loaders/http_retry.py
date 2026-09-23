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


RETRY_REASONS_429 = ("rate_limit", "rate_limit_per_second")


def is_429_reason(reason):
    """Причина повтора из classify — это 429? Шаги, печатающие «429 — N», считают по ней и повторы http_retry."""
    return reason in RETRY_REASONS_429


def count_429(stats, response=None):
    """Все 429 одного вызова request: повторы с причиной 429 из stats плюс последний ответ, если он 429.

    Обёртка, считающая 429 только по возвращённому ответу, не видит повторов, которые request сделал сам:
    ручной прогон 2026-09-23 — десять 429 в шаге штук и «пауз 429 — 0» в его итоге.
    """
    n = sum(v for k, v in (stats.get("reasons") or {}).items() if is_429_reason(k))
    return n + (1 if response is not None and int(response.status_code) == 429 else 0)


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
            max_attempts=MAX_ATTEMPTS, sleep_fn=time.sleep, session=None, stats=None, **kwargs):
    """Как requests.request, но с повтором транзиентных отказов.

    Возвращает последний ответ. Не бросает по коду ответа — решение о том,
    что делать с не-200, остаётся за вызывающим.

    stats — словарь вызывающего, если ему важно, был ли сбор гладким: requests (все попытки), retries
    (повторы), failures (последний ответ не 200), reasons (причины повторов). Сбор, в котором был хоть один
    повтор, для удаления застрявших ключей не годится (loaders/stale_keys.py).
    """
    caller = session or requests
    response = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        response = caller.request(method, url, **kwargs)
        if stats is not None:
            stats["requests"] = stats.get("requests", 0) + 1
        retryable, reason = classify(response, retry_429=retry_429)
        if not retryable or attempt >= max_attempts:
            if reason != "ok":
                if stats is not None:
                    stats["failures"] = stats.get("failures", 0) + 1
                if attempt > 1:
                    print(f"{label}: транзиентный отказ не изжит за {attempt} попыток (reason={reason})")
            return response
        if stats is not None:
            stats["retries"] = stats.get("retries", 0) + 1
            reasons = stats.setdefault("reasons", {})
            reasons[reason] = reasons.get(reason, 0) + 1
        pause = sleep_seconds(response, attempt)
        print(f"{label}: транзиентный отказ reason={reason} "
              f"attempt={attempt}/{max_attempts} sleep={pause}s")
        sleep_fn(pause)
    return response


def post(url, **kwargs):
    return request("POST", url, **kwargs)


def get(url, **kwargs):
    return request("GET", url, **kwargs)
