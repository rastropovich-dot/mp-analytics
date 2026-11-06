"""Отправление × товар Ozon (шестьдесят первая): строки ozon_posting_items из того же ответа, что ночные шаги заказов.

Схема — sql/20261106_create_ozon_posting_items.sql. Источник — список отправлений FBO /v3/posting/fbo/list и FBS
/v4/posting/fbs/list, который шаги заказов уже получили: ни одного нового обращения к Ozon. Правило отбора товаров —
то же, что у строк marketplace_orders (loaders/ozon_orders_rows.py): родитель разделённого отправления, отправление без
даты, товар без sku и с нулевым количеством не пишутся. Поэтому тождество одно:

    Σ quantity по (order_date, sku, schema)                 = orders_qty + cancelled_orders_qty
    Σ quantity × seller_price по (order_date, sku, schema)  = orders_amount_seller + cancelled_orders_amount_seller

и проверяется каждой ночью на строках этой же ночи (check_identity) — до записи, расхождение называется вслух.

Запись нефатальна: заказы — база витрин, отправления × товар — справочная таблица. write() ничего не бросает наружу:
отказ печатается строкой «⚠️ …», уходит в итог pipeline_runtime_state (ozon_posting_items:<схема>) и в строку алерта.

Застрявшие ключи — правило loaders/stale_keys.py: после полного сбора окна (список либо получен целиком, либо шаг упал
раньше) ключи окна, которых в ответе нет, удаляются. Окно чистки — дни строго ВНУТРИ окна сбора: первый день (граница
фильтра since у Ozon не по created_at — в сырье РБХ-2 есть FBO с created_at раньше since) и сегодняшний (отправление
могло ещё не попасть в список) не трогаются. Порог и печать списка до удаления — как у stale_keys.cleanup.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

try:
    from loaders import ozon_orders_rows as rules
    from loaders import stale_keys
except ImportError:  # вызов как скрипт из корня
    import ozon_orders_rows as rules
    import stale_keys

TABLE = "ozon_posting_items"
KEY = "posting_number,sku"
BATCH = 500
PAGE = 1_000
CENT = Decimal("0.01")
STATE_KEY_PREFIX = "ozon_posting_items:"
STATE_TYPE = "ozon_posting_items"


def item_rows(postings, schema, observed_at, source="nightly", buyer_prices=None):
    """(строки ozon_posting_items, счётчики). buyer_prices — как у rules.build_order_rows (FBO: из отчёта ЛК)."""
    if schema not in ("fbo", "fbs"):
        raise ValueError(f"схема {schema!r}: ожидается fbo или fbs")
    out, counters, seen_postings = {}, Counter(), set()
    for posting in postings:
        number = str(posting.get("posting_number") or "")
        if not number:
            counters["no_posting_number"] += 1
            continue
        if number in seen_postings:            # одно отправление дважды в ответе — второе не считается (и в заказах тоже нет)
            counters["duplicate_postings"] += 1
            continue
        seen_postings.add(number)
        status = str(posting.get("status") or "")
        if status in rules.SPLIT_PARENT_STATUSES:
            counters["split_parent_skipped"] += 1
            continue
        order_date = rules.order_date_of(posting, schema)
        if not order_date:
            counters["no_date"] += 1
            continue
        ordered_at = (posting.get("created_at") or posting.get("in_process_at")) if schema == "fbo" else \
            (posting.get("in_process_at") or posting.get("shipment_date"))
        for product in posting.get("products") or []:
            sku = str(product.get("sku") or "")
            if not sku:
                counters["no_sku"] += 1
                continue
            qty = Decimal(str(product.get("quantity") or 0))
            if qty <= 0:
                counters["zero_qty"] += 1
                continue
            price = rules.product_price(product, schema)
            unit_buyer = rules.buyer_unit_price(posting, product, buyer_prices)
            key = (number, sku)
            row = out.get(key)
            if row is not None:                # sku дважды в одном отправлении: разведка 06.11 — 0 случаев; складываем, не теряем
                counters["duplicate_sku_in_posting"] += 1
                total = row["_amount"] + qty * price
                row["quantity"] += qty
                row["_amount"] = total
                row["seller_price"] = (total / row["quantity"]).quantize(CENT)
                if row["buyer_price"] is not None and unit_buyer is not None:
                    row["_buyer"] += qty * unit_buyer
                    row["buyer_price"] = (row["_buyer"] / row["quantity"]).quantize(CENT)
                else:
                    row["buyer_price"] = None
                continue
            counters["items"] += 1
            counters["buyer_known" if unit_buyer is not None else "buyer_unknown"] += 1
            out[key] = {
                "posting_number": number, "sku": int(sku), "offer_id": str(product.get("offer_id") or "") or None,
                "schema": schema, "order_date": order_date, "ordered_at": ordered_at,
                "quantity": qty, "seller_price": price.quantize(CENT),
                "buyer_price": None if unit_buyer is None else unit_buyer.quantize(CENT),
                "status": status, "observed_at": observed_at, "source": source,
                "_amount": qty * price, "_buyer": (qty * unit_buyer) if unit_buyer is not None else Decimal(0),
            }
    rows = []
    for row in out.values():
        row.pop("_amount"), row.pop("_buyer")
        row["quantity"] = int(row["quantity"])
        row["seller_price"] = float(row["seller_price"])
        row["buyer_price"] = None if row["buyer_price"] is None else float(row["buyer_price"])
        rows.append(row)
    counters["rows"] = len(rows)
    return rows, dict(counters)


def day_sums(rows):
    """{(order_date, sku, schema): (штуки, Σ штуки × цена продавца)} — Decimal."""
    out = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for r in rows:
        a = out[(str(r["order_date"]), str(r["sku"]), r["schema"])]
        q = Decimal(str(r["quantity"]))
        a[0] += q
        a[1] += q * Decimal(str(r["seller_price"]))
    return {k: (v[0], v[1].quantize(CENT)) for k, v in out.items()}


def order_sums(order_rows):
    """Те же суммы по строкам marketplace_orders (созданные = подтверждённые + отменённые)."""
    out = {}
    for r in order_rows:
        key = (str(r["order_date"]), str(r["marketplace_sku"]), r["order_schema"])
        q = Decimal(str(r.get("orders_qty") or 0)) + Decimal(str(r.get("cancelled_orders_qty") or 0))
        a = Decimal(str(r.get("orders_amount_seller") or 0)) + Decimal(str(r.get("cancelled_orders_amount_seller") or 0))
        prev = out.get(key, (Decimal(0), Decimal(0)))
        out[key] = (prev[0] + q, (prev[1] + a).quantize(CENT))
    return out


def check_identity(item_rows_, order_rows):
    """Список расхождений [(ключ, штуки/сумма отправлений, штуки/сумма заказов)] — пусто, если тождество держится."""
    a, b = day_sums(item_rows_), order_sums(order_rows)
    diffs = []
    for key in sorted(set(a) | set(b)):
        x, y = a.get(key, (Decimal(0), Decimal(0))), b.get(key, (Decimal(0), Decimal(0)))
        if x[0] != y[0] or abs(x[1] - y[1]) > CENT:
            diffs.append((key, x, y))
    return diffs


def upsert(sb, rows):
    n = 0
    for i in range(0, len(rows), BATCH):
        sb.table(TABLE).upsert(rows[i:i + BATCH], on_conflict=KEY).execute()
        n += len(rows[i:i + BATCH])
    return n


def stale_window(days_back, now_utc=None):
    """(первый, последний) день чистки: строго внутри ночного окна — без его первого дня и без сегодняшнего."""
    now_utc = now_utc or rules.utc_now()
    today = now_utc.astimezone(ZoneInfo(rules.APP_TIMEZONE)).date()
    return today - timedelta(days=days_back) + timedelta(days=1), today - timedelta(days=1)


def read_window(sb, schema, day_from, day_to):
    """Ключи таблицы за дни окна одной схемы — по дню, с сортировкой по полному ключу (OFFSET только внутри дня)."""
    out, day = [], date.fromisoformat(str(day_from))
    end = date.fromisoformat(str(day_to))
    while day <= end:
        out.extend(stale_keys.read_window_rows(sb, TABLE, "posting_number,sku,order_date,status",
                                               [("eq", "schema", schema), ("eq", "order_date", day.isoformat())],
                                               ["posting_number", "sku"], page=PAGE))
        day += timedelta(days=1)
    return out


def delete_keys(sb, rows):
    n = 0
    for r in rows:
        res = sb.table(TABLE).delete().eq("posting_number", r["posting_number"]).eq("sku", r["sku"]).execute()
        n += len(res.data or [])
    return n


def cleanup_stale(sb, rows, schema, days_back, apply, now_utc=None, read_fn=read_window, delete_fn=delete_keys):
    day_from, day_to = stale_window(days_back, now_utc)
    inside = [r for r in rows if day_from.isoformat() <= str(r["order_date"]) <= day_to.isoformat()]
    existing = read_fn(sb, schema, day_from.isoformat(), day_to.isoformat())
    return stale_keys.cleanup(
        sb, TABLE, {"day_from": day_from.isoformat(), "day_to": day_to.isoformat(), "complete": True}, existing,
        {(str(r["posting_number"]), str(r["sku"])) for r in inside}, {str(r["order_date"]) for r in inside},
        key_of=lambda r: (str(r["posting_number"]), str(r["sku"])), day_of=lambda r: str(r["order_date"]),
        describe=lambda r: f"{r['order_date']} {schema} {r['posting_number']} sku {r['sku']} ({r.get('status')})",
        delete_fn=delete_fn, apply=apply)


def write_state(sb, schema, payload):
    sb.table("pipeline_runtime_state").upsert(
        {"state_key": STATE_KEY_PREFIX + schema, "state_type": STATE_TYPE, "account_signature": None, "payload": payload,
         "updated_at": payload["finished_at"]}, on_conflict="state_key").execute()


def write(sb, postings, schema, order_rows, days_back=30, buyer_prices=None, keep_buyer=False, observed_at=None, dry_run=False,
          now_utc=None, upsert_fn=upsert, cleanup_fn=cleanup_stale, state_fn=write_state):
    """Нефатальная запись отправлений × товар одной схемы. Возвращает итог (dict); исключений наружу не бросает.

    keep_buyer — отчёт ЛК FBO этой ночью не пришёл (сорок пятая §3): строки пишутся БЕЗ buyer_price, upsert его не трогает —
    у ключа остаётся прежнее измерение, у нового ключа — null.
    """
    label = f"Ozon {schema.upper()}: отправления × товар"
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    info = {"schema": schema, "ok": False, "error": None, "dry_run": bool(dry_run), "observed_at": observed_at}
    try:
        rows, counters = item_rows(postings, schema, observed_at, buyer_prices=buyer_prices)
        diffs = check_identity(rows, order_rows)
        qty = sum(r["quantity"] for r in rows)
        info.update({"rows": len(rows), "quantity": qty, "identity_keys": len(day_sums(rows)), "identity_diffs": len(diffs),
                     "buyer_unknown": counters.get("buyer_unknown", 0),
                     "skipped": {k: v for k, v in counters.items() if k not in ("items", "rows", "buyer_known", "buyer_unknown")}})
        print(f"{label}: строк {len(rows)}, штук {qty}, цена покупателя неизвестна у {counters.get('buyer_unknown', 0)}; "
              f"тождество с marketplace_orders: ключей {info['identity_keys']}, расхождений {len(diffs)}"
              + (f"; пропущено {info['skipped']}" if info["skipped"] else ""), flush=True)
        for key, x, y in diffs[:10]:
            print(f"  ⚠️  {key}: отправления {x[0]} шт / {x[1]} ₽, заказы {y[0]} шт / {y[1]} ₽", flush=True)
        if dry_run:
            print(f"{label}: dry-run — в базу не пишу (db_writes = 0)", flush=True)
        else:
            to_write = [{k: v for k, v in r.items() if k != "buyer_price"} for r in rows] if keep_buyer else rows
            info["written"] = upsert_fn(sb, to_write)
            info["buyer_price_kept"] = bool(keep_buyer)
            print(f"✅ {label}: записано {info['written']} строк в {TABLE}", flush=True)
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001 — справочная таблица: заказы уже записаны, шаг не роняем
        info["error"] = f"{type(exc).__name__}: {exc}"[:500]
        print(f"⚠️  {label} НЕ записаны: {info['error']} — заказы записаны, шаг продолжен", flush=True)
    if info["ok"]:
        try:   # чистка — после записи, не до; её отказ строки ночи не отменяет, но называется
            info["stale_deleted"] = cleanup_fn(sb, rows, schema, days_back, apply=not dry_run, now_utc=now_utc)
        except Exception as exc:  # noqa: BLE001
            info["stale_error"] = f"{type(exc).__name__}: {exc}"[:500]
            print(f"⚠️  {label}: застрявшие ключи не проверены: {info['stale_error']}", flush=True)
    info["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not dry_run:
        try:
            state_fn(sb, schema, info)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  итог {label} не записан в pipeline_runtime_state: {type(exc).__name__}: {exc}", flush=True)
    return info


def alert_line(payloads, now=None, max_age_hours=20):
    """Строка утреннего алерта — только при отказе или расхождении тождества этой ночью; иначе пусто."""
    now = now or datetime.now(timezone.utc)
    bad = []
    for schema in ("fbo", "fbs"):
        p = (payloads or {}).get(schema)
        if not p:
            continue
        try:
            finished = datetime.fromisoformat(str(p.get("finished_at")))
        except (TypeError, ValueError):
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if now - finished > timedelta(hours=max_age_hours):
            continue
        if not p.get("ok"):
            bad.append(f"{schema.upper()} не записаны ({p.get('error') or 'причина не записана'})")
        elif p.get("identity_diffs"):
            bad.append(f"{schema.upper()} расходятся с заказами на {p['identity_diffs']} ключах из {p.get('identity_keys', '?')}")
        elif p.get("stale_error"):
            bad.append(f"{schema.upper()} записаны, застрявшие ключи не проверены ({p['stale_error']})")
    return ("⚠️ отправления × товар Ozon: " + "; ".join(bad)) if bad else ""
