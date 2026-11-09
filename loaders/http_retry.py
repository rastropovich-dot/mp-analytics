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

Ожидание по заголовкам (шестьдесят вторая, седьмая задача RBH). С 25.09 Seller API отдаёт `ratelimit-remaining` и, у 429,
`retry-after` в секундах (чат, id 66817, 67133, 67138). Ответ с `remaining = 0` значит, что следующий запрос ТОГО ЖЕ метода
получит 429, — поэтому до него ждём: `retry-after`, если он есть, иначе RATELIMIT_ZERO_WAIT_SECONDS (у 200 Ozon `retry-after`
не шлёт: id 66923 «http=200 remaining=0 retry-after=»), не дольше RATELIMIT_MAX_WAIT_SECONDS. Ожидание считается от момента
ответа: пауза вызывающего между запросами входит в него. Ключ — метод + URL (у `accrual/postings` и `accrual/by-day` квоты
раздельные: id 66920 / 66922). Нет заголовка `ratelimit-remaining` — поведение прежнее, без ожиданий (WB шлёт X-Ratelimit-*,
под это правило не попадает). Ожидания и минимум remaining считаются в stats отдельно от 429.
"""

import os
import threading
import time

import requests

MAX_ATTEMPTS = 4
BASE_SLEEP_SECONDS = 1
CAP_SLEEP_SECONDS = 10
NET_BASE_SLEEP_SECONDS = 5       # пауза после сетевого исключения: 5, 10, 15 … с (потолок 30)
NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError)   # SSLError и ProxyError — подклассы ConnectionError
OZON_RATE_LIMIT_PER_SECOND_CODE = 8

RATELIMIT_REMAINING_HEADER = "ratelimit-remaining"
RETRY_AFTER_HEADER = "retry-after"
RATELIMIT_ZERO_WAIT_SECONDS = float(os.getenv("OZON_RATELIMIT_ZERO_WAIT_SECONDS", "1"))   # remaining = 0 без retry-after
RATELIMIT_MAX_WAIT_SECONDS = 10.0
_gate = {}                        # (метод, url) → момент (monotonic), раньше которого запрос этого метода не шлём
_gate_lock = threading.Lock()

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


def header(response, name):
    """Значение заголовка без учёта регистра (requests — CaseInsensitiveDict, фальшивые ответы тестов — dict)."""
    headers = getattr(response, "headers", None) or {}
    value = headers.get(name)
    if value is None:
        low = name.lower()
        for k, v in headers.items():
            if str(k).lower() == low:
                return v
    return value


def ratelimit_remaining(response):
    """int из ratelimit-remaining или None (заголовка нет / не число)."""
    raw = header(response, RATELIMIT_REMAINING_HEADER)
    try:
        return int(float(str(raw).strip())) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ratelimit_wait(response):
    """Сколько ждать до следующего запроса того же метода: 0 — не ждать; None — заголовка нет (прежнее поведение)."""
    remaining = ratelimit_remaining(response)
    if remaining is None:
        return None
    if remaining > 0:
        return 0.0
    raw = header(response, RETRY_AFTER_HEADER)
    try:
        wait = float(str(raw).strip()) if raw not in (None, "") else RATELIMIT_ZERO_WAIT_SECONDS
    except (TypeError, ValueError):
        wait = RATELIMIT_ZERO_WAIT_SECONDS
    return max(0.0, min(RATELIMIT_MAX_WAIT_SECONDS, wait))


def _note(stats, key, value=1):
    if stats is not None:
        stats[key] = stats.get(key, 0) + value


def wait_for_gate(key, stats=None, sleep_fn=time.sleep, clock=time.monotonic):
    """Ждать, если прошлый ответ этого метода сказал remaining = 0 и срок ещё не вышел. Возвращает секунды ожидания."""
    with _gate_lock:
        until = _gate.get(key)
    if until is None:
        return 0.0
    left = until - clock()
    if left <= 0:
        return 0.0
    _note(stats, "ratelimit_waits")
    _note(stats, "ratelimit_wait_seconds", round(left, 3))
    sleep_fn(left)
    return left


def update_gate(key, response, stats=None, clock=time.monotonic):
    """После ответа: запомнить срок ожидания (remaining = 0) или снять его; remaining — в stats (min и сколько раз 0)."""
    remaining = ratelimit_remaining(response)
    if remaining is not None and stats is not None:
        stats["ratelimit_remaining_min"] = min(stats.get("ratelimit_remaining_min", remaining), remaining)
        _note(stats, "ratelimit_seen")
        if remaining == 0:
            _note(stats, "ratelimit_zero")
    wait = ratelimit_wait(response)
    with _gate_lock:
        if wait:
            _gate[key] = clock() + wait
        else:
            _gate.pop(key, None)


def reset_gate():
    with _gate_lock:
        _gate.clear()


def absorb_ratelimit(counters, stats):
    """Счётчики ожиданий одного вызова → счётчики шага (отдельно от 429)."""
    for k in ("ratelimit_waits", "ratelimit_zero", "ratelimit_seen"):
        counters[k] = counters.get(k, 0) + stats.get(k, 0)
    counters["ratelimit_wait_seconds"] = round(counters.get("ratelimit_wait_seconds", 0) + stats.get("ratelimit_wait_seconds", 0), 3)
    if "ratelimit_remaining_min" in stats:
        counters["ratelimit_remaining_min"] = min(counters.get("ratelimit_remaining_min", stats["ratelimit_remaining_min"]), stats["ratelimit_remaining_min"])


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
            max_attempts=MAX_ATTEMPTS, sleep_fn=time.sleep, session=None, stats=None, clock=time.monotonic, **kwargs):
    """Как requests.request, но с повтором транзиентных отказов.

    Возвращает последний ответ. Не бросает по коду ответа — решение о том,
    что делать с не-200, остаётся за вызывающим.

    stats — словарь вызывающего, если ему важно, был ли сбор гладким: requests (все попытки), retries
    (повторы), failures (последний ответ не 200), reasons (причины повторов). Сбор, в котором был хоть один
    повтор, для удаления застрявших ключей не годится (loaders/stale_keys.py). Ожидания по ratelimit-remaining —
    ratelimit_waits / ratelimit_wait_seconds, минимум remaining — ratelimit_remaining_min (заголовок модуля).
    """
    caller = session or requests
    response = None
    gate_key = (str(method).upper(), str(url))
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        wait_for_gate(gate_key, stats=stats, sleep_fn=sleep_fn, clock=clock)
        try:
            response = caller.request(method, url, **kwargs)
        except NETWORK_ERRORS as exc:
            # Сеть, не ответ сервера: обрыв TLS (SSLEOFError), прокси, таймаут, оборванное тело. 22.10 на РБХ-1 без
            # повтора падали и сбор accrual/postings, и опрос отчётов Performance. Повторяем с нарастающей паузой,
            # последняя попытка — исключение наружу, как раньше.
            if stats is not None:
                stats["requests"] = stats.get("requests", 0) + 1
                stats["net_retries"] = stats.get("net_retries", 0) + 1
            if attempt >= max_attempts:
                raise
            pause = min(CAP_SLEEP_SECONDS * 3, NET_BASE_SLEEP_SECONDS * attempt)
            print(f"{label}: сеть {type(exc).__name__} attempt={attempt}/{max_attempts} sleep={pause}s")
            sleep_fn(pause)
            continue
        if stats is not None:
            stats["requests"] = stats.get("requests", 0) + 1
        update_gate(gate_key, response, stats=stats, clock=clock)
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
        with _gate_lock:      # пауза повтора уже выждала retry-after — второй раз перед той же попыткой не ждём
            _gate.pop(gate_key, None)
    return response


def post(url, **kwargs):
    return request("POST", url, **kwargs)


def get(url, **kwargs):
    return request("GET", url, **kwargs)
