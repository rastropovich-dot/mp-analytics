"""Окно ночного прогона — одно место для всех скриптов, которым нельзя стартовать во время ночи.

Ночь 09-21 шла до 04:04 UTC (очередь отчётов Performance у Ozon, 181 мин ожидания), а прежнее окно
00:15 … 03:15 в четырёх скриптах её уже не накрывало (CLAUDE.md §9-4ж). Верх поднят до 04:30 — полчаса
над самой длинной ночью. Окно — по часам, не по факту: спрашивать Render, идёт ли прогон, — отдельное
решение. Утреннее окно — задача alerts_telegram (07:30 UTC): алерт и сборка книги месяца из базы.
"""
from datetime import datetime, timezone

NIGHTLY_RUN_WINDOW_UTC = ((0, 15), (4, 30))    # cron `15 0 * * *`; самая длинная ночь — 09-21, финиш 04:04
MORNING_ALERT_WINDOW_UTC = ((7, 20), (7, 45))  # cron `30 7 * * *`; алерт ~10 с, книга месяца ~1–2 мин


def _minutes(hm):
    return hm[0] * 60 + hm[1]


def _in(window, now_utc):
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minutes = now_utc.hour * 60 + now_utc.minute
    return _minutes(window[0]) <= minutes <= _minutes(window[1])


def in_nightly_run_window(now_utc=None):
    return _in(NIGHTLY_RUN_WINDOW_UTC, now_utc)


def in_morning_alert_window(now_utc=None):
    return _in(MORNING_ALERT_WINDOW_UTC, now_utc)


def window_text(window=NIGHTLY_RUN_WINDOW_UTC):
    (h1, m1), (h2, m2) = window
    return f"{h1:02d}:{m1:02d}…{h2:02d}:{m2:02d} UTC"
