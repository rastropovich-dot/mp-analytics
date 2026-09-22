"""Застрявшие ключи: после ПОЛНОГО сбора окна удалить из таблицы ключи окна, которых нет среди построенных строк.

Три таблицы копили ключи, которых у Ozon уже нет, потому что upsert не удаляет: начисление было в ответе by-day,
пока день шёл, Ozon его убрал — строка осталась (`marketplace_expenses`: 4 строки за 9 дней, +1 667,25;
`marketplace_buyouts`: 08-21 sku 4453045714; по построению — `ozon_accrual_daily_types`). Правило одно, как у
заказов FBO с 2026-09-16: свежий полный сбор окна — единственный источник правды о ключах окна.

Защиты, без которых удалять нельзя:
  * только ключи ВНУТРИ окна сбора — читатель фильтрует по датам окна, за окно не заглядывает;
  * только если сбор ПОЛОН — все дни получены, ни одного повтора и отказа (`window["complete"]`): частичный
    ответ с удалением превратил бы дыру в API в дыру в базе (docs/loader_partial_data_contract.md);
  * день, за который свежий сбор не построил НИ ОДНОЙ строки, а в таблице строки есть, не чистится: пустой
    день при коде 200 неотличим от дыры в API — называется вслух и ждёт следующей ночи;
  * порог: больше MAX_STALE_ROWS строк или MAX_STALE_SHARE окна — это не чистка, а поломка; отказ с причиной;
  * список удаляемых ключей печатается целиком ДО удаления; удаление — ПОСЛЕ записи, не до; --dry-run не пишет.
"""
from collections import defaultdict

MAX_STALE_ROWS = 200          # абсолютный потолок за один прогон
MAX_STALE_SHARE = 0.02        # доля окна, выше которой это уже не чистка …
MIN_STALE_ALLOWED = 10        # … но на маленьком окне (леджер ~570 строк) десяток ключей — ещё чистка


def allowed_stale(existing_count):
    return int(min(MAX_STALE_ROWS, max(MIN_STALE_ALLOWED, MAX_STALE_SHARE * existing_count)))


def read_window_rows(sb, table, select, filters, order, page=1000):
    """Постраничное чтение с сортировкой по полному ключу (без order PostgREST отдаёт повторы и пропуски)."""
    out, i = [], 0
    while True:
        qb = sb.table(table).select(select)
        for f in filters:
            qb = getattr(qb, f[0])(*f[1:])
        for col in order:
            qb = qb.order(col)
        res = qb.range(i * page, i * page + page - 1).execute()
        data = res.data or []
        out.extend(data)
        if len(data) < page:
            return out
        i += 1


def plan(existing, built_keys, built_days, key_of, day_of):
    """(застрявшие строки, дни без единой построенной строки). Ключ есть в таблице, среди построенных нет — застрял."""
    stale, skipped_days = [], set()
    for r in existing:
        if key_of(r) in built_keys:
            continue
        if day_of(r) not in built_days:
            skipped_days.add(day_of(r))
            continue
        stale.append(r)
    return stale, sorted(skipped_days)


def cleanup(sb, table, window, existing, built_keys, built_days, key_of, day_of, describe, delete_fn, apply):
    """Печатает план и, если apply и все защиты пройдены, удаляет. Возвращает число удалённых строк."""
    print(f"Застрявшие ключи {table}: окно {window['day_from']} … {window['day_to']}, строк окна в таблице {len(existing)}, "
          f"построено ключей {len(built_keys)}, дней с построенными строками {len(built_days)}")
    if not window.get("complete"):
        print(f"  сбор окна НЕ полон (повторов {window.get('retries')}, отказов {window.get('failures')}) — ничего не удаляю")
        return 0
    stale, skipped_days = plan(existing, built_keys, built_days, key_of, day_of)
    if skipped_days:
        print(f"  дни со строками в таблице, но без единой построенной строки — НЕ трогаю: {', '.join(skipped_days)}")
    if not stale:
        print("  застрявших ключей нет")
        return 0
    print(f"  к удалению {len(stale)}:")
    for r in stale:
        print("    " + describe(r))
    limit = allowed_stale(len(existing))
    if len(stale) > limit:
        print(f"  ОТКАЗ: {len(stale)} строк — больше порога {limit} (≤ {MAX_STALE_ROWS} и ≤ {MAX_STALE_SHARE:.0%} окна, не меньше {MIN_STALE_ALLOWED}): "
              "похоже на поломку сбора, не на чистку. Не удаляю")
        return 0
    if not apply:
        print("  --dry-run: не удаляю")
        return 0
    n = delete_fn(sb, stale)
    print(f"  ✅ удалено {n} из {len(stale)}")
    return n


def delete_by_id(sb, table, rows, batch=200):
    ids = [r["id"] for r in rows]
    n = 0
    for i in range(0, len(ids), batch):
        res = sb.table(table).delete().in_("id", ids[i:i + batch]).execute()
        n += len(res.data or [])
    return n


def delete_by_date_and_column(sb, table, rows, date_col, col):
    by_day = defaultdict(list)
    for r in rows:
        by_day[r[date_col]].append(r[col])
    n = 0
    for day, values in sorted(by_day.items()):
        res = sb.table(table).delete().eq(date_col, day).in_(col, values).execute()
        n += len(res.data or [])
    return n
