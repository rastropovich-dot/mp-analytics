"""Шаг «Ozon: загрузка FBS заказов» + сырой ответ для лога статусов.

То же, что loaders/ozon_fbs_orders_loader.py при запуске
(get_ozon_fbs_postings(days_back=30) → save_ozon_orders), плюс тот же ответ
/v4/posting/fbs/list кладётся в data/postings_raw/fbs_<UTC>.json для
scripts/ozon_posting_status_log.py. Ни одного дополнительного обращения к API,
загрузчик не изменён — вызываются его функции. Окно 30 дней, как у FBO:
отмены дозревают неделями (loaders/ozon_orders_rows.py).

Порядок важен: шаг ФАТАЛЬНЫЙ, сырьё — вспомогательное. Сначала запись заказов,
потом сырьё; отказ сохранения сырья печатает предупреждение, шаг не роняет.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента

from loaders import ozon_fbs_orders_loader as fbs  # noqa: E402
from loaders import ozon_posting_status_log as log  # noqa: E402


def run(fbs_module=fbs, log_module=log, days_back=fbs.DEFAULT_DAYS_BACK, dry_run=False):
    postings = fbs_module.get_ozon_fbs_postings(days_back=days_back)
    if dry_run:   # §5.3 RBH: снять, посчитать, сырьё сохранить; в базу не писать
        print(f"dry-run: FBS отправлений {len(postings)} за {days_back} дн.; db_writes = 0", flush=True)
    else:
        fbs_module.save_ozon_orders(postings)
    try:
        path = log_module.dump_raw(postings, "fbs")
        print(f"Сырой ответ FBS сохранён: {path} ({len(postings)} отправлений)")
    except Exception as exc:  # лог статусов — вспомогательный; заказы уже записаны
        print(f"⚠️  сырой ответ FBS НЕ сохранён, лог статусов за эту ночь без FBS: {type(exc).__name__}: {exc}", flush=True)
    return postings


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Ozon: загрузка FBS заказов (/v4/posting/fbs/list) + сырьё для лога статусов")
    ap.add_argument("--days-back", type=int, default=fbs.DEFAULT_DAYS_BACK)
    ap.add_argument("--dry-run", action="store_true", help="снять окно, сохранить сырьё, в базу не писать; db_writes = 0")
    args = ap.parse_args(argv)
    run(days_back=args.days_back, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
