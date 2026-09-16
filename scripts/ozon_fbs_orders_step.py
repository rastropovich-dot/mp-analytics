"""Шаг «Ozon: загрузка FBS заказов» + сырой ответ для лога статусов.

То же, что loaders/ozon_fbs_orders_loader.py при запуске
(get_ozon_fbs_postings(days_back=14) → save_ozon_orders), плюс тот же ответ
/v3/posting/fbs/list кладётся в data/postings_raw/fbs_<UTC>.json для
scripts/ozon_posting_status_log.py. Ни одного дополнительного обращения к API,
загрузчик не изменён — вызываются его функции. Переход на /v4 — отдельно.

Порядок важен: шаг ФАТАЛЬНЫЙ, сырьё — вспомогательное. Сначала запись заказов,
потом сырьё; отказ сохранения сырья печатает предупреждение, шаг не роняет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders import ozon_fbs_orders_loader as fbs  # noqa: E402
from loaders import ozon_posting_status_log as log  # noqa: E402


def run(fbs_module=fbs, log_module=log, days_back=14):
    postings = fbs_module.get_ozon_fbs_postings(days_back=days_back)
    fbs_module.save_ozon_orders(postings)
    try:
        path = log_module.dump_raw(postings, "fbs")
        print(f"Сырой ответ FBS сохранён: {path} ({len(postings)} отправлений)")
    except Exception as exc:  # лог статусов — вспомогательный; заказы уже записаны
        print(f"⚠️  сырой ответ FBS НЕ сохранён, лог статусов за эту ночь без FBS: {type(exc).__name__}: {exc}", flush=True)
    return postings


if __name__ == "__main__":
    run()
