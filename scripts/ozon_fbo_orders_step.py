"""Шаг «Ozon: загрузка FBO заказов» + сырой ответ для лога статусов.

Ровно то же, что делает loaders/ozon_fbo_orders_loader.py при запуске
(get_fbo_postings → build_order_rows → save_orders), плюс одна вещь: тот же
ответ /v3/posting/fbo/list кладётся в data/postings_raw/fbo_<UTC>.json, откуда
его читает scripts/ozon_posting_status_log.py. Ни одного дополнительного
обращения к API, загрузчик не изменён — вызываются его функции.

Порядок важен: шаг ФАТАЛЬНЫЙ (заказы — база витрин), а сырьё для лога —
вспомогательное. Поэтому сначала запись заказов, потом сырьё, и отказ
сохранения сырья печатает предупреждение, но шаг не роняет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders import ozon_fbo_orders_loader as fbo  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_posting_status_log as log  # noqa: E402
from loaders import ozon_postings_report as report  # noqa: E402


def fetch_fbo_buyer_prices(days_back, report_module=report, rules_module=rules):
    """Цены покупателя FBO из отчёта ЛК за то же окно, что список. Отказ — {} и строка в лог: заказы пишутся без цены (null)."""
    since, to = rules_module.nightly_window(days_back)
    try:
        prices, stats = report_module.fetch_buyer_prices("fbo", since, to)
        print(f"Ozon FBO: цены покупателя из отчёта ЛК — строк {stats['rows']}, без цены {stats['skipped']}, "
              f"обращений create {stats['create']} / info {stats['info']} / download {stats['download']}, {stats['seconds']} с")
        return prices
    except Exception as exc:  # цена покупателя — справочная колонка; заказы без неё пишутся как есть
        print(f"⚠️  Ozon FBO: отчёт ЛК не получен, orders_amount_buyer за ночь — null: {type(exc).__name__}: {exc}", flush=True)
        return {}


def run(fbo_module=fbo, log_module=log, days_back=30, report_module=report):
    postings = fbo_module.get_fbo_postings(days_back=days_back)
    buyer_prices = fetch_fbo_buyer_prices(days_back, report_module)
    rows = fbo_module.build_order_rows(postings, buyer_prices=buyer_prices)
    fbo_module.save_orders(rows)
    try:
        path = log_module.dump_raw(postings, "fbo")
        print(f"Сырой ответ FBO сохранён: {path} ({len(postings)} отправлений)")
    except Exception as exc:  # лог статусов — вспомогательный; заказы уже записаны
        print(f"⚠️  сырой ответ FBO НЕ сохранён, лог статусов за эту ночь без FBO: {type(exc).__name__}: {exc}", flush=True)
    return postings


if __name__ == "__main__":
    run()
