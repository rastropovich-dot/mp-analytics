"""Повтор транзиентных сетевых отказов для читателей книги «Фин рез» и ночного скрипта (пятьдесят четвёртая §1а).

Ночная книга на Mac ходит через прокси VPN-клиента, и тот в 06:00 МСК отдаёт 503 / рвёт соединения (21.10: `RemoteProtocolError: Server
disconnected`, `ReadError: Connection reset by peer`; 22.10: `ProxyError: 503 Service Unavailable` за 17 с). Такой отказ — транзиентный:
повторить 3 раза с паузой 20 с, и только потом считать шаг не выполненным. Справочные читатели (бонусы, курс, аванс) книгу не роняют —
строка «не прочитано: …» и дальше. Путь сети (прокси) здесь не меняется — только по слову владельца.
"""
import time

TRIES = 3
PAUSE_SECONDS = 20
TRANSIENT_MARKERS = ("503", "502", "504", "Service Unavailable", "Server disconnected", "Connection reset", "reset by peer", "ProxyError",
                     "RemoteProtocolError", "ReadError", "ConnectError", "ReadTimeout", "ConnectTimeout", "timed out", "Temporary failure",
                     "EOF occurred", "Connection aborted", "RemoteDisconnected", "57014", "statement timeout")


def is_transient(exc):
    """Сетевой / прокси / таймаут / обрыв / statement timeout — да; остальное (ключи, схема, логика) — нет."""
    try:
        import httpx
        if isinstance(exc, (httpx.TransportError,)):   # ProxyError, ConnectError, ReadError, RemoteProtocolError, TimeoutException …
            return True
    except ImportError:
        pass
    try:
        import requests
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in TRANSIENT_MARKERS)


def text_is_transient(text):
    return any(m in (text or "") for m in TRANSIENT_MARKERS)


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
