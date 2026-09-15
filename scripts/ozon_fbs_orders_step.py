"""Шаг «Ozon: загрузка FBS заказов» + сырой ответ для лога статусов.

То же, что loaders/ozon_fbs_orders_loader.py при запуске
(get_ozon_fbs_postings(days_back=14) → save_ozon_orders), плюс тот же ответ
/v3/posting/fbs/list кладётся в data/postings_raw/fbs_<UTC>.json для
scripts/ozon_posting_status_log.py. Ни одного дополнительного обращения к API,
загрузчик не изменён — вызываются его функции. Переход на /v4 — отдельно.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders import ozon_fbs_orders_loader as fbs  # noqa: E402
from loaders import ozon_posting_status_log as log  # noqa: E402

if __name__ == "__main__":
    postings = fbs.get_ozon_fbs_postings(days_back=14)
    path = log.dump_raw(postings, "fbs")
    print(f"Сырой ответ FBS сохранён: {path} ({len(postings)} отправлений)")
    fbs.save_ozon_orders(postings)
