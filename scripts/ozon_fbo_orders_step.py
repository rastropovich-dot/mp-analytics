"""Шаг «Ozon: загрузка FBO заказов» + сырой ответ для лога статусов.

Ровно то же, что делает loaders/ozon_fbo_orders_loader.py при запуске
(get_fbo_postings → build_order_rows → save_orders), плюс одна вещь: тот же
ответ /v3/posting/fbo/list кладётся в data/postings_raw/fbo_<UTC>.json, откуда
его читает scripts/ozon_posting_status_log.py. Ни одного дополнительного
обращения к API, загрузчик не изменён — вызываются его функции.

Порядок важен: шаг ФАТАЛЬНЫЙ (заказы — база витрин), а сырьё для лога —
вспомогательное. Поэтому сначала запись заказов, потом сырьё, и отказ
сохранения сырья печатает предупреждение, но шаг не роняет.

Цена покупателя FBO — из отчёта ЛК (loaders/ozon_postings_report.py). Отчёт не пришёл (сорок пятая §3, ночь 09-29: «не готов за
180 с» обнулил цену на всём 30-дневном окне — 2 709 строк из 3 892) — строки пишутся БЕЗ колонок цены покупателя: upsert их не
трогает, у ключа остаётся прежнее измерение, у нового ключа — null (default у колонок снят миграцией 2026-09-24). Итог шага
(отчёт пришёл / нет, сколько строк осталось с прежним измерением) — в pipeline_runtime_state (ozon_fbo_buyer_prices:last) для
строки утреннего алерта. Чтение прежних ключей — одно постраничное по id (~4 страницы), только при отказе.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders import ozon_fbo_orders_loader as fbo  # noqa: E402
from loaders import ozon_orders_rows as rules  # noqa: E402
from loaders import ozon_posting_status_log as log  # noqa: E402
from loaders import ozon_postings_report as report  # noqa: E402


STATE_KEY = "ozon_fbo_buyer_prices:last"
STATE_TYPE = "ozon_fbo_buyer_prices"


def fetch_fbo_buyer_prices(days_back, report_module=report, rules_module=rules):
    """Цены покупателя FBO из отчёта ЛК за то же окно, что список: (цены | None при отказе, итог для алерта)."""
    since, to = rules_module.nightly_window(days_back)
    try:
        prices, stats = report_module.fetch_buyer_prices("fbo", since, to)
        print(f"Ozon FBO: цены покупателя из отчёта ЛК — строк {stats['rows']}, без цены {stats['skipped']}"
              + (f" (в чужой валюте {stats['foreign_currency']})" if stats.get("foreign_currency") else "")
              + f", обращений create {stats['create']} / info {stats['info']} / download {stats['download']}, {stats['seconds']} с")
        return prices, {"ok": True, "error": None, "seconds": stats["seconds"], "report_rows": stats["rows"], "skipped": stats["skipped"],
                        "foreign_currency": stats.get("foreign_currency", 0), "info_calls": stats["info"]}
    except Exception as exc:  # цена покупателя — справочная колонка; заказы без неё пишутся, прежнее измерение не трогаем
        print(f"⚠️  Ozon FBO: отчёт ЛК не получен: {type(exc).__name__}: {exc} — цена покупателя FBO этой ночью НЕ переписывается "
              f"(у ключей окна остаётся прежнее измерение, у новых ключей — null)", flush=True)
        return None, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def existing_buyer_keys(sb, since_date, page=1000):
    """{(order_date, sku): измерена ли цена покупателя} для FBO Ozon с since_date — постранично по id, с сортировкой."""
    out, last = {}, None
    while True:
        q = (sb.table("marketplace_orders").select("id,order_date,marketplace_sku,orders_amount_buyer")
             .eq("marketplace_code", "ozon").eq("order_schema", "fbo").gte("order_date", since_date).order("id").limit(page))
        if last is not None:
            q = q.gt("id", last)
        data = q.execute().data or []
        for r in data:
            out[(str(r["order_date"]), str(r["marketplace_sku"]))] = r.get("orders_amount_buyer") is not None
        if len(data) < page:
            return out
        last = data[-1]["id"]


def keep_measured_buyer(rows, existing, rules_module=rules):
    """Строки без колонок цены покупателя (upsert их не трогает) + счётчики: у скольких ключей осталось прежнее измерение."""
    keys = [(str(r["order_date"]), str(r["marketplace_sku"])) for r in rows]
    kept = sum(1 for k in keys if existing.get(k))
    counts = {"rows_kept_measured": kept, "rows_without_price": len(keys) - kept, "rows_new_keys": sum(1 for k in keys if k not in existing)}
    return [{k: v for k, v in r.items() if k not in rules_module.BUYER_COLUMNS} for r in rows], counts


def write_state(sb, payload):
    sb.table("pipeline_runtime_state").upsert(
        {"state_key": STATE_KEY, "state_type": STATE_TYPE, "account_signature": None, "payload": payload, "updated_at": payload["finished_at"]},
        on_conflict="state_key").execute()


def run(fbo_module=fbo, log_module=log, days_back=30, report_module=report, sb=None, now=None, existing_fn=existing_buyer_keys, state_fn=write_state):
    now = now or datetime.now(timezone.utc)
    postings = fbo_module.get_fbo_postings(days_back=days_back)
    buyer_prices, info = fetch_fbo_buyer_prices(days_back, report_module)
    rows = fbo_module.build_order_rows(postings, buyer_prices=buyer_prices or {})
    info.update({"date": now.date().isoformat(), "started_at": now.isoformat(timespec="seconds"), "rows": len(rows)})
    if buyer_prices is None and rows:
        client = sb or getattr(fbo_module, "supabase", None)
        try:
            existing = existing_fn(client, min(str(r["order_date"]) for r in rows))
        except Exception as exc:  # не прочитали прежние ключи — всё равно не затираем, только без счётчиков
            print(f"⚠️  Ozon FBO: прежние ключи окна не прочитаны ({type(exc).__name__}: {exc}) — пишу без цены покупателя, счётчиков нет", flush=True)
            existing, info["count_error"] = None, f"{type(exc).__name__}: {exc}"
        rows, counts = keep_measured_buyer(rows, existing or {})
        if existing is not None:
            info.update(counts)
            print(f"Ozon FBO: строк {len(rows)} пишутся без цены покупателя — у {counts['rows_kept_measured']} остаётся прежнее измерение, "
                  f"{counts['rows_without_price']} без цены (из них новых ключей {counts['rows_new_keys']})", flush=True)
    fbo_module.save_orders(rows)
    info["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        state_fn(sb or getattr(fbo_module, "supabase", None), info)
    except Exception as exc:  # итог для алерта — вспомогательный; заказы уже записаны
        print(f"⚠️  итог цены покупателя FBO не записан в pipeline_runtime_state: {type(exc).__name__}: {exc}", flush=True)
    try:
        path = log_module.dump_raw(postings, "fbo")
        print(f"Сырой ответ FBO сохранён: {path} ({len(postings)} отправлений)")
    except Exception as exc:  # лог статусов — вспомогательный; заказы уже записаны
        print(f"⚠️  сырой ответ FBO НЕ сохранён, лог статусов за эту ночь без FBO: {type(exc).__name__}: {exc}", flush=True)
    return postings


if __name__ == "__main__":
    run()
