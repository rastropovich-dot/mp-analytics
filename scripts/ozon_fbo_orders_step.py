"""Шаг «Ozon: загрузка FBO заказов» + сырой ответ для лога статусов.

Ровно то же, что делает loaders/ozon_fbo_orders_loader.py при запуске
(get_fbo_postings → build_order_rows → save_orders), плюс одна вещь: тот же
ответ /v3/posting/fbo/list кладётся в data/postings_raw/fbo_<UTC>.json, откуда
его читает scripts/ozon_posting_status_log.py. Ни одного дополнительного
обращения к API, загрузчик не изменён — вызываются его функции.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders import ozon_fbo_orders_loader as fbo  # noqa: E402
from loaders import ozon_posting_status_log as log  # noqa: E402

if __name__ == "__main__":
    postings = fbo.get_fbo_postings(days_back=30)
    path = log.dump_raw(postings, "fbo")
    print(f"Сырой ответ FBO сохранён: {path} ({len(postings)} отправлений)")
    rows = fbo.build_order_rows(postings)
    fbo.save_orders(rows)
