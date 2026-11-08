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

Застрявшие ключи — ТОЛЬКО ОТЧЁТ (вариант без удалений, п. 44 08.11: удаление запрещено). После полного сбора окна ключи окна,
которых в ответе нет, находятся тем же правилом stale_keys.plan, но ничего не удаляется и не изменяется — ни delete, ни update,
ни перезапись строки статусом. Окно — дни строго ВНУТРИ окна сбора: первый день (граница фильтра since у Ozon не по created_at —
в сырье РБХ-2 есть FBO с created_at раньше since) и сегодняшний (отправление могло ещё не попасть в список) не смотрятся. Итог —
число кандидатов и до 5 примеров (posting_number / sku / order_date) в pipeline_runtime_state и строку алерта.

Следствие: кандидаты остаются в таблице. Сумма таблицы по (order_date, sku) может быть больше заказов — родитель разделённого
отправления (cancelled_from_split_pending) ночь не строит, его прежняя строка остаётся рядом со строками детей. Отличить: у живого
ключа окна observed_at = последней полной ночи, у кандидата — старше.
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


BUYER_COLUMN = "buyer_price"


def buyer_groups(rows):
    """(строки с ценой покупателя — все колонки, строки без цены — БЕЗ колонки buyer_price), п. 44а.

    Зачем две группы: postgrest-py (2.29.0, pre_upsert) шлёт у пачки columns = объединение ключей всех строк и при
    merge-duplicates обновляет каждую из этих колонок; ключ, которого у строки нет, уходит как null. Строка «без цены» в общей
    пачке со строками «с ценой» затёрла бы измеренную buyer_price. Поэтому пачка всегда однородна: либо у всех строк есть
    buyer_price (не null), либо ни у одной нет этой колонки — тогда upsert её не трогает (у ключа остаётся прежнее измерение,
    у нового ключа — null, default колонки).
    """
    with_price = [r for r in rows if r.get(BUYER_COLUMN) is not None]
    without = [{k: v for k, v in r.items() if k != BUYER_COLUMN} for r in rows if r.get(BUYER_COLUMN) is None]
    return with_price, without


def upsert(sb, rows):
    """upsert по (posting_number, sku) однородными пачками (buyer_groups). Контракт прежний: возвращает число отправленных строк."""
    n = 0
    for group in buyer_groups(rows):
        for i in range(0, len(group), BATCH):
            batch = group[i:i + BATCH]
            cols = set(batch[0])
            if any(set(r) != cols for r in batch):     # одна пачка — один набор колонок, иначе PostgREST допишет null
                raise ValueError(f"{TABLE}: в пачке разные наборы колонок — не отправляю")
            sb.table(TABLE).upsert(batch, on_conflict=KEY).execute()
            n += len(batch)
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


STALE_EXAMPLES = 5


def report_stale(sb, rows, schema, days_back, now_utc=None, read_fn=read_window):
    """Кандидаты в застрявшие ключи окна одной схемы — ТОЛЬКО отчёт: окно читается, в базе ничего не меняется.

    Правило — stale_keys.plan: ключ есть в таблице за день окна, среди построенных этой ночью его нет, и за этот день ночь построила
    хотя бы одну строку (день без единой построенной строки не считается — пустой ответ неотличим от дыры в API).
    Возвращает {"candidates": N, "examples": [до 5 {posting_number, sku, order_date}], "skipped_days": [...], "day_from", "day_to"}.
    """
    day_from, day_to = stale_window(days_back, now_utc)
    inside = [r for r in rows if day_from.isoformat() <= str(r["order_date"]) <= day_to.isoformat()]
    existing = read_fn(sb, schema, day_from.isoformat(), day_to.isoformat())
    stale, skipped_days = stale_keys.plan(
        existing, {(str(r["posting_number"]), str(r["sku"])) for r in inside}, {str(r["order_date"]) for r in inside},
        key_of=lambda r: (str(r["posting_number"]), str(r["sku"])), day_of=lambda r: str(r["order_date"]))
    examples = [{"posting_number": str(r["posting_number"]), "sku": str(r["sku"]), "order_date": str(r["order_date"])}
                for r in stale[:STALE_EXAMPLES]]
    print(f"Застрявшие ключи {TABLE} ({schema}): окно {day_from} … {day_to}, строк окна в таблице {len(existing)}, "
          f"кандидатов {len(stale)} — не удалены (удаление запрещено)"
          + (f"; дни без построенных строк не смотрю: {', '.join(skipped_days)}" if skipped_days else ""), flush=True)
    for e in examples:
        print(f"    {e['order_date']} {schema} {e['posting_number']} sku {e['sku']}", flush=True)
    return {"candidates": len(stale), "examples": examples, "skipped_days": skipped_days,
            "day_from": day_from.isoformat(), "day_to": day_to.isoformat()}


def write_state(sb, schema, payload):
    sb.table("pipeline_runtime_state").upsert(
        {"state_key": STATE_KEY_PREFIX + schema, "state_type": STATE_TYPE, "account_signature": None, "payload": payload,
         "updated_at": payload["finished_at"]}, on_conflict="state_key").execute()


def write(sb, postings, schema, order_rows, days_back=30, buyer_prices=None, keep_buyer=False, observed_at=None, dry_run=False,
          now_utc=None, upsert_fn=upsert, report_fn=report_stale, state_fn=write_state):
    """Нефатальная запись отправлений × товар одной схемы. Возвращает итог (dict); исключений наружу не бросает.

    keep_buyer — отчёт ЛК FBO этой ночью не пришёл (сорок пятая §3): строки пишутся БЕЗ buyer_price, upsert его не трогает —
    у ключа остаётся прежнее измерение, у нового ключа — null. И без keep_buyer строка, у которой цены этой ночью нет (частичный
    отчёт ЛК, FBS без customer_price, чужая валюта), уходит без колонки buyer_price — upsert делит пачки (buyer_groups, п. 44а).
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
            to_write = [{k: v for k, v in r.items() if k != BUYER_COLUMN} for r in rows] if keep_buyer else rows
            with_price, without = buyer_groups(to_write)
            info["written"] = upsert_fn(sb, to_write)
            info["buyer_price_kept"] = bool(keep_buyer)
            info["buyer_written"], info["buyer_untouched"] = len(with_price), len(without)
            print(f"✅ {label}: записано {info['written']} строк в {TABLE} — с ценой покупателя {len(with_price)}, "
                  f"без колонки цены (прежнее измерение не трогается) {len(without)}", flush=True)
        info["ok"] = True
    except Exception as exc:  # noqa: BLE001 — справочная таблица: заказы уже записаны, шаг не роняем
        info["error"] = f"{type(exc).__name__}: {exc}"[:500]
        print(f"⚠️  {label} НЕ записаны: {info['error']} — заказы записаны, шаг продолжен", flush=True)
    if info["ok"]:
        try:   # застрявшие ключи — только отчёт, после записи; ничего не удаляет и не меняет; отказ строки ночи не отменяет
            rep = report_fn(sb, rows, schema, days_back, now_utc=now_utc)
            info["stale_candidates"] = int(rep["candidates"])
            info["stale_examples"] = [{k: str(e[k]) for k in ("posting_number", "sku", "order_date")}
                                      for e in rep["examples"][:STALE_EXAMPLES]]
            # 44б: окно и пропущенные дни — в итог, чтобы «кандидатов 0» при пропущенном дне не читалось как «чисто»
            info["stale_window"] = [str(rep.get("day_from")), str(rep.get("day_to"))]
            info["stale_skipped_days"] = [str(d) for d in rep.get("skipped_days") or []]
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
    """Строка утреннего алерта — при отказе, расхождении тождества или кандидатах в застрявшие ключи этой ночью; иначе пусто."""
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
            continue
        if p.get("identity_diffs"):
            bad.append(f"{schema.upper()} расходятся с заказами на {p['identity_diffs']} ключах из {p.get('identity_keys', '?')}")
        if p.get("stale_error"):
            bad.append(f"{schema.upper()} записаны, застрявшие ключи не проверены ({p['stale_error']})")
        elif p.get("stale_candidates"):
            bad.append(f"{schema.upper()} застрявшие ключи: кандидатов {p['stale_candidates']}, не удалены (удаление запрещено)")
        if not p.get("stale_error") and p.get("stale_skipped_days"):
            bad.append(f"{schema.upper()} проверка застрявших ключей неполная: пропущено дней {len(p['stale_skipped_days'])}")
    return ("⚠️ отправления × товар Ozon: " + "; ".join(bad)) if bad else ""
