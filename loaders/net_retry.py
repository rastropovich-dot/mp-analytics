"""Повтор транзиентных сетевых отказов для читателей книги «Фин рез» и ночного скрипта (пятьдесят четвёртая §1а).

Ночная книга на Mac ходит через прокси VPN-клиента, и тот в 06:00 МСК отдаёт 503 / рвёт соединения (21.10: `RemoteProtocolError: Server
disconnected`, `ReadError: Connection reset by peer`; 22.10: `ProxyError: 503 Service Unavailable` за 17 с). Такой отказ — транзиентный:
повторить 3 раза с паузой 20 с, и только потом считать шаг не выполненным. Справочные читатели (бонусы, курс, аванс) книгу не роняют —
строка «не прочитано: …» и дальше. Путь сети (прокси) здесь не меняется — только по слову владельца.
"""
import re
import time

TRIES = 3
PAUSE_SECONDS = 20
TRANSIENT_STATUSES = (502, 503, 504)
# Текстовые маркеры — без голых чисел: «503» само по себе ловило суммы вроде «1 503,00» в хвосте лога (довесок 54-й).
TRANSIENT_MARKERS = ("Service Unavailable", "Bad Gateway", "Gateway Timeout", "Gateway Time-out", "Server disconnected", "Connection reset",
                     "reset by peer", "ProxyError", "RemoteProtocolError", "ReadError", "ConnectError", "ReadTimeout", "ConnectTimeout",
                     "timed out", "Temporary failure", "EOF occurred", "Connection aborted", "RemoteDisconnected", "statement timeout",
                     "canceling statement")
# Код 502 / 503 / 504 — только в форме HTTP-статуса: «HTTP 503», «status 503», «status_code=503», «code 503», «503 Service Unavailable»;
# «1 503,00», «rows=2504», «−2 504,17» — не статус (границы: перед кодом не цифра, после — не цифра / запятая / точка с цифрой).
TRANSIENT_STATUS_RE = re.compile(
    r"(?:\bHTTP[/ ]?(?:[12]\.[01] )?|\bstatus(?:_code)?\s*[:= ]\s*|\bcode\s*[:= ]\s*|\bHTTP status\s*)50[234](?![0-9]|[,.][0-9])"
    r"|(?<![0-9])50[234]\s+(?:Service Unavailable|Bad Gateway|Gateway Time-?out)", re.I)
# код 57014 — только как код ошибки Postgres (statement timeout), не как сумма
TRANSIENT_PG_RE = re.compile(r"(?<![0-9])57014(?![0-9]|[,.][0-9])")


def status_code_of(exc):
    """HTTP-статус ответа из httpx.HTTPStatusError / requests.HTTPError; нет — None."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return int(code) if isinstance(code, int) else None


def text_is_transient(text):
    """Текст (строка лога, stderr, текст исключения) говорит о сетевом / прокси / обрыве / таймауте?"""
    text = text or ""
    return any(m in text for m in TRANSIENT_MARKERS) or bool(TRANSIENT_STATUS_RE.search(text)) or bool(TRANSIENT_PG_RE.search(text))


def is_transient(exc):
    """Сетевой / прокси / таймаут / обрыв / 502-503-504 / statement timeout — да; остальное (ключи, схема, логика) — нет.
    У HTTP-ошибок с ответом решает status_code, не текст."""
    try:
        import httpx
        if isinstance(exc, httpx.TransportError):   # ProxyError, ConnectError, ReadError, RemoteProtocolError, TimeoutException …
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return status_code_of(exc) in TRANSIENT_STATUSES
    except ImportError:
        pass
    try:
        import requests
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError):
            return status_code_of(exc) in TRANSIENT_STATUSES
    except ImportError:
        pass
    return text_is_transient(f"{type(exc).__name__}: {exc}")


def call(fn, label, tries=TRIES, pause=None, sleep_fn=None, log=print):
    """fn() с повтором транзиентных отказов: tries попыток, pause секунд между ними; не транзиентное — сразу наружу."""
    pause = PAUSE_SECONDS if pause is None else pause
    sleep_fn = sleep_fn or time.sleep     # берётся при вызове, чтобы тесты могли подменить time.sleep модуля
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc) or attempt == tries:
                raise
            last = exc
            log(f"⚠️ {label}: {type(exc).__name__}: {str(exc)[:160]} — повтор {attempt} из {tries - 1} через {pause} с")
            sleep_fn(pause)
    raise last  # pragma: no cover


def execute(query, label, **kw):
    """query.execute() с повтором (postgrest-запрос Supabase)."""
    return call(query.execute, label, **kw)
