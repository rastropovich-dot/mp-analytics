"""Страницы по составному ключу (день, id) через PostgREST — только индексные формы запроса (WB-10 §3).

Форма `or(day.gt.X, and(day.eq.X, id.gt.Y))` (WB-8 §6) не ложится на индекс первичного ключа: сервер идёт по индексу с
начала и фильтрует, глубокие страницы дорожают, как offset, и на 490 тыс. строк под параллельной нагрузкой одна страница
упёрлась в statement_timeout 8 с (57014, 2026-09-25). Здесь каждый запрос — диапазон по индексу (day, id):
  * «диапазон»: day ≥ from AND day ≤ to ORDER BY day, id LIMIT n            — начало и переход к следующему дню;
  * «тот же день»: day = D AND id > Y ORDER BY id LIMIT n                   — продолжение дня, на котором кончилась страница.
Полная страница → продолжаем тот же день; неполная страница в режиме «тот же день» → день исчерпан, переходим к
диапазону day > D; неполная страница в режиме «диапазон» → всё прочитано. Потолок PostgREST — 1 000 строк на ответ,
больше просить бессмысленно (how-we-work).
"""

PAGE = 1_000


def read_keyset(sb, table, select, day_from, day_to, day_col="day", id_col="nm_id", page=PAGE, filters=None):
    """Все строки table с day_from ≤ day_col ≤ day_to, отсортированные по (day_col, id_col). filters — список
    (метод, *аргументы) билдера PostgREST, применяется к каждому запросу (например ("in_", "seller_oper_name", [...]))."""
    def base():
        qb = sb.table(table).select(select)
        for f in filters or ():
            qb = getattr(qb, f[0])(*f[1:])
        return qb

    out, day_lo, same_day = [], day_from, None
    while True:
        if same_day is None:
            qb = base().gte(day_col, day_lo).lte(day_col, day_to).order(day_col).order(id_col).limit(page)
        else:
            qb = base().eq(day_col, same_day[0]).gt(id_col, same_day[1]).order(id_col).limit(page)
        data = qb.execute().data or []
        out.extend(data)
        if len(data) < page:
            if same_day is None:
                return out
            day_lo, same_day = same_day[0], None            # день исчерпан — следующий диапазон начинается после него
            if _after(day_lo) > day_to:
                return out
            day_lo = _after(day_lo)
            continue
        same_day = (str(data[-1][day_col]), data[-1][id_col])


def _after(day):
    """Следующая календарная дата в ISO (границы диапазона — включительные)."""
    from datetime import date, timedelta
    return (date.fromisoformat(str(day)[:10]) + timedelta(days=1)).isoformat()
