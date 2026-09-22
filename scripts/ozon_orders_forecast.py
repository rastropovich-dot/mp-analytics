#!/usr/bin/env python3
"""Кривая дозревания заказов Ozon и прогноз подтверждённых. Чистые функции: ни БД, ни API, ни файлов.

Источник кривой — ozon_posting_status_log. Состояние отправления на ночь N — его последняя запись не позже N
(лог сверен с marketplace_orders до рубля на 29 датах, 2026-09-21). Ночью считается только сбор до 06:00 UTC:
дневной сдвигает возраст на полсуток. Посевы 09-14 и 09-15 сняты днём — ночами не считаются, но состояние
несут. FBS в ночь 09-17 собирался старым окном 14 дней — у FBS ночи с 09-18.

Что измеряется. Возраст = дата ночного сбора (МСК) − день заказа. По каждой паре соседних ночей (N, N+1):
    h(k) = отменённые к ночи N+1 / живые (не отменённые) возраста k на ночь N
— по отправлениям («шт»: штук товара в логе нет) и по их суммам («₽»). Меры разные по смыслу и по числам:
дорогие заказы отменяют реже, рублёвая доля ниже штучной; к рублям применяется рублёвая, к штукам — штучная.
Возрасты сложены в вёдра (AGE_BUCKETS) — внутри ведра доля одна.

Прогноз — лестница развития, ни одной зашитой доли:
    доля подтверждённых сейчас, которая ещё отменится   r(a) = 1 − Π (1 − h(k)),  k = a … 20;  для a ≥ 21 — факт, r = 0
    прогноз подтверждённых                                = подтверждено сейчас × (1 − r(a))
Это то же, что (c∞ − c(a)) / (1 − c(a)) из задачи, но измеренное внутри когорты. Формула среза (c(a) по молодым
заказам, плато c∞ по старым) смешивает когорты с разным уровнем отмен: 2026-09-21 у FBS по ₽ плато старых (31,2 %)
оказалось НИЖЕ уровня молодых на 6-й день, и срез обещал «отмен больше не будет» с шестого дня, хотя переходы
показывают ещё 16,5 %. Срез считается и показывается справочно (slice_*), в прогноз не идёт.

Лог копится каждую ночь, кривая каждое утро строится заново по последним CURVE_NIGHTS ночам и шумит всё меньше.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

Z = Decimal(0)
SCHEMAS = ("fbo", "fbs")
MATURE_AGE = 21                    # с этого возраста день считается дозревшим: прогноза нет, только факт
PLATEAU_AGES = range(21, 30)       # 30-й день у окон до 09-20 собирался не с полуночи — в плато не берём
NIGHT_HOUR_UTC = 6                 # сбор позже — не ночной: возраст сдвинут, в срезы не идёт
FIRST_NIGHT = {"fbo": "2026-09-17", "fbs": "2026-09-18"}
# Вёдра возрастов — те же, что у среза в reports_model.md §5. Одна дорогая отмена на 20-й день (FBS, три пары ночей)
# давала h ₽ = 9,4 % в одном возрасте при 0,0–0,9 % у соседей; ведро размазывает её по своим возрастам.
AGE_BUCKETS = ((0,), (1,), (2,), (3, 4), (5, 6), (7, 8, 9), (10, 11, 12, 13), (14, 15, 16, 17, 18, 19, 20))
CURVE_NIGHTS = 45                  # сколько последних ночей складывать: старше — другая когорта, другой сезон

# Константы ручного листа владельца (reports_model.md §1, июльская книга). Справочные колонки считаются на них,
# чтобы разница с измеренным была видна. Они дрейфуют: в сентябрьской книге на листе «Заказы» уже 0,58,
# на «Заказы Standard» — 0,73.
OWNER = {"buyout_rate": Decimal("0.65"), "after_commission": Decimal("0.59"), "other_rate": Decimal("0.024")}


def D(v):
    return Decimal(str(v or 0))


def parse_ts(value):
    """Метка PostgREST → datetime UTC. Дробная часть бывает любой длины (…52.40194+00:00) — python 3.9 такую не читает."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?$", str(value).strip())
    if not m:
        raise ValueError(f"не разобрана метка времени: {value!r}")
    day, clock, frac, tz = m.groups()
    tz = "+00:00" if tz in (None, "Z") else (tz if ":" in tz else (tz + ":00" if len(tz) == 3 else tz[:3] + ":" + tz[3:]))
    return datetime.fromisoformat(f"{day}T{clock}.{(frac or '0')[:6].ljust(6, '0')}{tz}").astimezone(timezone.utc)


def pava(values, weights):
    """Ближайшая неубывающая последовательность (взвешенные наименьшие квадраты): нарушители порядка сливаются в среднее."""
    blocks = []                                   # [сумма v·w, сумма w, число точек]
    for v, w in zip(values, weights):
        if not w:
            continue                              # точка без наблюдений порядка не задаёт — значение возьмёт у соседа
        blocks.append([v * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, w2, n = blocks.pop()
            blocks[-1][0] += s; blocks[-1][1] += w2; blocks[-1][2] += n
    fitted = []
    for s, w, n in blocks:
        fitted.extend([s / w] * n)
    out, i, last = [], 0, None
    for w in weights:
        if w:
            last = fitted[i]; i += 1
        out.append(last)
    first = next((v for v in out if v is not None), Z)
    return [first if v is None else v for v in out]


def build_curve(log_rows, tz_name="Europe/Moscow", first_night=None, max_nights=CURVE_NIGHTS):
    """Кривая дозревания по схемам из строк лога статусов.

    log_rows  [{posting_number, observed_at, schema, order_date, status, amount}]
    Возвращает {schema: {...}}; схемы, по которой нет ни одной пары соседних ночей, в словаре нет — прогноза по ней нет.
        nights, pairs, postings      ночи-срезы, пары соседних ночей (N, N+1), отправлений в срезах
        h_cnt, h_amt   {k: доля}     доля живых возраста k, отменённых к следующей ночи (по отправлениям / по ₽)
        r_cnt, r_amt   {a: доля}     доля подтверждённых сейчас, которая ещё отменится: 1 − Π (1 − h(k)), k = a … 20
        alive, events  {k: (n, ₽)}   сырьё переходов
        slice_*                      справочно: срез c(a), плато c∞ и r по формуле среза — см. docstring модуля
    """
    first_night = first_night or FIRST_NIGHT
    tz = ZoneInfo(tz_name)
    by_posting = defaultdict(list)
    night_dates = defaultdict(set)
    for r in log_rows:
        schema = str(r["schema"] or "").lower()
        if schema not in SCHEMAS:
            continue
        ts = parse_ts(r["observed_at"])
        seen = ts.astimezone(tz).date()
        by_posting[(schema, r["posting_number"])].append((ts, seen, date.fromisoformat(r["order_date"]), str(r["status"] or ""), D(r["amount"])))
        if ts.hour < NIGHT_HOUR_UTC and seen.isoformat() >= first_night.get(schema, "0001-01-01"):
            night_dates[schema].add(seen)
    out = {}
    for schema in SCHEMAS:
        nights = sorted(night_dates.get(schema, ()))[-max_nights:]
        pairs = [(a, b) for a, b in zip(nights, nights[1:]) if (b - a).days == 1]
        if not pairs:
            continue
        raw = defaultdict(lambda: [0, 0, Z, Z])            # срез: возраст → [создано, отменено, создано ₽, отменено ₽]
        alive = defaultdict(lambda: [0, Z])                # переходы: возраст k → [живых, ₽ живых] на ночь N
        events = defaultdict(lambda: [0, Z])               #           из них отменено к ночи N+1
        postings = set()
        biggest = (None, Z, None)                          # крупнейшая отмена в переходах: рублёвая кривая шумит хвостом цен
        for (s, number), evs in by_posting.items():
            if s != schema:
                continue
            evs.sort(key=lambda e: e[0])
            state = {}
            for night in nights:
                last = None
                for e in evs:                              # последняя запись не позже этой ночи
                    if e[1] <= night:
                        last = e
                    else:
                        break
                state[night] = last                        # None — в ту ночь отправления ещё не видели
                if last is None:
                    continue
                age = (night - last[2]).days
                if 0 <= age <= max(PLATEAU_AGES):
                    cell = raw[age]
                    cell[0] += 1; cell[2] += last[4]
                    if last[3] == "cancelled":
                        cell[1] += 1; cell[3] += last[4]
                    postings.add(number)
            for n1, n2 in pairs:
                before, after = state[n1], state[n2]
                if before is None or before[3] == "cancelled":
                    continue
                k = (n1 - before[2]).days
                if not 0 <= k < MATURE_AGE:
                    continue
                alive[k][0] += 1; alive[k][1] += before[4]
                if after[3] == "cancelled":
                    events[k][0] += 1; events[k][1] += before[4]
                    if before[4] > biggest[1]:
                        biggest = (k, before[4], number)
        ages = list(range(0, MATURE_AGE))
        h_cnt, h_amt = {}, {}
        for bucket in AGE_BUCKETS:                         # внутри ведра доля одна: Σ отмен ведра / Σ живых ведра
            n_alive, a_alive = sum(alive[k][0] for k in bucket), sum((alive[k][1] for k in bucket), Z)
            n_ev, a_ev = sum(events[k][0] for k in bucket), sum((events[k][1] for k in bucket), Z)
            for k in bucket:
                h_cnt[k] = Decimal(n_ev) / Decimal(n_alive) if n_alive else Z
                h_amt[k] = a_ev / a_alive if a_alive else Z
        r_cnt, r_amt, keep_cnt, keep_amt = {}, {}, Decimal(1), Decimal(1)
        for k in reversed(ages):
            keep_cnt *= 1 - h_cnt[k]; keep_amt *= 1 - h_amt[k]
            r_cnt[k], r_amt[k] = 1 - keep_cnt, 1 - keep_amt
        res = {"nights": [n.isoformat() for n in nights], "pairs": len(pairs), "postings": len(postings),
               "h_cnt": h_cnt, "h_amt": h_amt, "r_cnt": r_cnt, "r_amt": r_amt,
               "alive": {k: tuple(v) for k, v in alive.items()}, "events": {k: tuple(v) for k, v in events.items()},
               "biggest_event": biggest, "events_amt_total": sum((v[1] for v in events.values()), Z),
               "empty_ages": [k for k in ages if not alive[k][0]], "raw": {a: tuple(v) for a, v in raw.items()}}
        # справочно — срез: c(a), плато по возрастам 21…29, r = (c∞ − c) / (1 − c). Болеет когортами: плато меряется на
        # старых заказах, c(a) — на молодых; уровень отмен у них разный, и у FBS по ₽ срез даёт r = 0 уже с 6-го дня.
        plateau = [raw[a] for a in PLATEAU_AGES if a in raw]
        n_inf, a_inf = sum(c[0] for c in plateau), sum((c[2] for c in plateau), Z)
        if n_inf and a_inf:
            cnt_inf, amt_inf = Decimal(sum(c[1] for c in plateau)) / Decimal(n_inf), sum((c[3] for c in plateau), Z) / a_inf
            weights = [Decimal(raw[a][0]) for a in ages]
            cnt = [min(v, cnt_inf) for v in pava([Decimal(raw[a][1]) / Decimal(raw[a][0]) if raw[a][0] else Z for a in ages], weights)]
            amt = [min(v, amt_inf) for v in pava([raw[a][3] / raw[a][2] if raw[a][2] else Z for a in ages], weights)]
            res.update({"slice_cnt_inf": cnt_inf, "slice_amt_inf": amt_inf, "slice_cnt": dict(zip(ages, cnt)), "slice_amt": dict(zip(ages, amt)),
                        "slice_r_cnt": {a: ((cnt_inf - c) / (1 - c) if c < 1 else Z) for a, c in zip(ages, cnt)},
                        "slice_r_amt": {a: ((amt_inf - c) / (1 - c) if c < 1 else Z) for a, c in zip(ages, amt)}})
        out[schema] = res
    return out


def remaining_share(curve, schema, age, measure):
    """Доля подтверждённых сейчас, которая ещё отменится. measure: "cnt" | "amt". Дозревший день — 0. Кривой нет — None."""
    if age >= MATURE_AGE:
        return Z
    c = curve.get(schema)
    if c is None:
        return None
    return c["r_" + measure][max(age, 0)]


def shares_by_platform(buyouts, platform_of_sku):
    """Измеренная доля комиссии по площадкам: Σ комиссии / Σ оборота выкупов окна. Плюс «все» — для площадки без оборота."""
    acc = defaultdict(lambda: [Z, Z])
    for r in buyouts:
        for key in (platform_of_sku(r["marketplace_sku"]), "все"):
            acc[key][0] += D(r["commission_amount"]); acc[key][1] += D(r["buyouts_amount_seller"])
    return {k: (c / t if t else None) for k, (c, t) in acc.items()}, {k: t for k, (c, t) in acc.items()}


def build_orders_daily(days, orders, curve, obs_date, commission_share, other_share, ads_by_day, unit_cost, vat_for, platform_of_row,
                       ads_manual_by_day=None):
    """Строки листа «Заказы»: {"fbo": [...], "fbs": [...], "all": [...], "platform:<площадка>": [...]} по дням заказа.

    orders            [{order_date, order_schema, marketplace_sku, article, orders_qty, orders_amount_seller,
                        cancelled_orders_qty, cancelled_orders_amount_seller}]
    obs_date          ночь, на которую снято состояние заказов: "YYYY-MM-DD" или {схема: "YYYY-MM-DD"}
    commission_share  {площадка: доля} с ключом "все" — запасная доля для площадки без выкупов в окне
    other_share       доля прочих расходов (с НДС) в обороте выкупов (с НДС); None — не измерена
    ads_by_day        {день: реклама 41 + 54 без НДС | None} — реклама модели (наш фин. рез. прогноз)
    ads_manual_by_day {день: «реклама по образцу» без НДС | None} — (41 + 54 + 96 + вся подписка), как у владельца; от неё считаются
                      его формулы (ДРР, фин. рез.), чтобы сравниваться с его листом напрямую. Не задана — берётся ads_by_day
    Блоки по площадкам ("platform:Основная" …) — те же колонки без рекламы и фин. реза: леджер начислений без площадки.
    Возвращает ещё и список названных вслух обстоятельств (площадка без своей доли комиссии, схема без кривой).
    """
    ads_manual_by_day = ads_by_day if ads_manual_by_day is None else ads_manual_by_day
    blocks = {k: {d: defaultdict(Decimal) for d in days} for k in SCHEMAS + ("all",)}
    said = set()
    # Состояние схем может быть разной свежести: ночью упал шаг одной из них — её заказы на сутки старше, и возраст у них свой.
    obs = obs_date if isinstance(obs_date, dict) else {k: obs_date for k in SCHEMAS}
    obs = {**obs, "all": min(obs.values())}        # общий лист зовёт день дозревшим, только когда он дозрел у обеих схем
    if len({obs[k] for k in SCHEMAS if k in obs}) > 1:
        said.add("состояние заказов по схемам разной свежести: " + ", ".join(f"{k.upper()} — на ночь {obs[k]}" for k in SCHEMAS if k in obs)
                 + "; возраст дня на общем листе — по более старому состоянию")
    for r in orders:
        d, schema = r["order_date"], str(r.get("order_schema") or "").lower()
        if d not in blocks["all"]:
            continue
        if schema not in SCHEMAS:
            said.add(f"строка заказов со схемой {schema!r} — в лист не вошла")
            continue
        age = (date.fromisoformat(obs.get(schema, obs["all"])) - date.fromisoformat(d)).days
        conf_q, conf_a = D(r["orders_qty"]), D(r["orders_amount_seller"])
        canc_q, canc_a = D(r.get("cancelled_orders_qty")), D(r.get("cancelled_orders_amount_seller"))
        r_cnt, r_amt = remaining_share(curve, schema, age, "cnt"), remaining_share(curve, schema, age, "amt")
        if r_cnt is None:
            said.add(f"для схемы {schema} кривой нет (в логе нет ночных срезов с дозревшими возрастами) — прогноза по ней нет")
        platform = platform_of_row(r)
        pkey = f"platform:{platform}"
        if pkey not in blocks:
            blocks[pkey] = {d: defaultdict(Decimal) for d in days}
        share = commission_share.get(platform)
        if share is None:
            share = commission_share.get("все")
            said.add(f"у площадки «{platform}» нет выкупов в окне комиссии — взята общая доля")
        uc = unit_cost(r["marketplace_sku"])
        vat = vat_for(d)
        for key in (schema, "all", pkey):
            a = blocks[key][d]
            a["created_q"] += conf_q + canc_q; a["created_a"] += conf_a + canc_a
            a["conf_q"] += conf_q; a["conf_a"] += conf_a
            a["canc_q"] += canc_q; a["canc_a"] += canc_a
            a["cogs_created"] += (conf_q + canc_q) * (uc or Z)
            if uc is None:
                a["no_cost_q"] += conf_q
            if r_cnt is None or share is None:
                a["no_forecast"] += 1
                continue
            fc_q, fc_a = conf_q * (1 - r_cnt), conf_a * (1 - r_amt)
            a["fc_q"] += fc_q; a["fc_a"] += fc_a
            a["commission"] += fc_a * share
            a["revenue"] += fc_a * (1 - share) / vat
            a["cogs"] += fc_q * (uc or Z)
    out = {}
    for key, per_day in blocks.items():
        rows = []
        for d in days:
            a, vat = per_day[d], vat_for(d)
            age = (date.fromisoformat(obs.get(key, obs["all"])) - date.fromisoformat(d)).days
            ok = not a["no_forecast"]
            row = {"date": d, "vat": vat, "age": age, "mature": age >= MATURE_AGE,
                   "created_q": a["created_q"], "created_a": a["created_a"], "conf_q": a["conf_q"], "conf_a": a["conf_a"],
                   "canc_q": a["canc_q"], "canc_a": a["canc_a"], "no_cost_q": a["no_cost_q"], "cogs_created": a["cogs_created"]}
            for k in ("fc_q", "fc_a", "commission", "revenue", "cogs"):
                row[k] = a[k] if ok else None
            row["expected_cancels_a"] = (a["conf_a"] - a["fc_a"]) if ok else None
            row["margin"] = (row["revenue"] - row["cogs"]) if ok else None
            row["other"] = (row["fc_a"] * other_share / vat) if ok and other_share is not None else None
            row["ads"] = ads_by_day.get(d) if key == "all" else None
            row["ads_manual"] = ads_manual_by_day.get(d) if key == "all" else None
            row["fin_result"] = None if None in (row["margin"], row["ads"], row["other"]) else row["margin"] - row["ads"] - row["other"]
            # справочно: формулы владельца на его константах и на ЕГО рекламе («по образцу»: 41 + 54 + 96 + вся подписка) —
            # чтобы его лист «Заказы» и эти колонки сравнивались напрямую (2026-09-22, тридцать первая).
            row["owner_revenue"] = a["created_a"] * OWNER["after_commission"] / vat
            row["owner_margin"] = row["owner_revenue"] - a["cogs_created"]
            row["owner_fin_result"] = None if row["ads_manual"] is None else (row["owner_margin"] * OWNER["buyout_rate"] - row["ads_manual"]
                                                                               - row["owner_revenue"] * OWNER["buyout_rate"] * OWNER["other_rate"])
            rows.append(row)
        out[key] = rows
    return out, sorted(said)


ORDER_MONEY = ("created_q", "created_a", "conf_q", "conf_a", "canc_q", "canc_a", "fc_q", "fc_a", "expected_cancels_a", "commission", "revenue",
               "cogs", "margin", "ads", "ads_manual", "other", "fin_result", "owner_revenue", "owner_margin", "owner_fin_result", "no_cost_q", "cogs_created")


def ratio(a, b):
    return (a / b) if (a is not None and b) else None


def add_order_ratios(row):
    """Проценты строки. ДРР — обе базы без НДС, как реклама; ДРР владельца — его формула как есть (реклама без НДС к обороту с НДС)."""
    vat = row["vat"]
    created_net = row.get("_created_net", row["created_a"] / vat)
    fc_net = row.get("_fc_net", None if row["fc_a"] is None else row["fc_a"] / vat)
    row["canc_pct"] = ratio(row["canc_a"], row["created_a"])
    row["fc_canc_pct"] = None if row["fc_a"] is None else ratio(row["created_a"] - row["fc_a"], row["created_a"])
    row["commission_pct"] = ratio(row["commission"], row["fc_a"])
    row["margin_pct"] = ratio(row["margin"], row["revenue"])
    row["drr_created_pct"] = ratio(row["ads"], created_net)
    row["drr_fc_pct"] = ratio(row["ads"], fc_net)
    row["fin_result_pct"] = ratio(row["fin_result"], row["revenue"])
    row["owner_drr_pct"] = ratio(row.get("ads_manual", row["ads"]), row["owner_revenue"] * OWNER["buyout_rate"] * vat / OWNER["after_commission"])
    row["fin_result_minus_owner"] = None if None in (row["fin_result"], row["owner_fin_result"]) else row["fin_result"] - row["owner_fin_result"]
    return row


def orders_total(rows):
    """«Итого»: деньги суммой, проценты — от сумм. Колонка пуста хоть за один день — в итоге она пуста, и это видно."""
    t = {"date": "Итого", "vat": rows[-1]["vat"], "age": None, "mature": all(r["mature"] for r in rows)}
    for k in ORDER_MONEY:
        vals = [r.get(k) for r in rows]
        t[k] = None if any(v is None for v in vals) else sum(vals, Z)
    t["_created_net"] = sum((r["created_a"] / r["vat"] for r in rows), Z)       # НДС может смениться внутри месяца
    t["_fc_net"] = None if t["fc_a"] is None else sum((r["fc_a"] / r["vat"] for r in rows), Z)
    return add_order_ratios(t)


def mature_check(rows):
    """Приёмка: у дозревшего дня прогноз обязан равняться факту. Возвращает (дозревших дней, из них прогноз = факт, нарушители)."""
    mature = [r for r in rows if r["mature"]]
    bad = [r["date"] for r in mature if r["fc_a"] is None or r["fc_a"] != r["conf_a"] or r["fc_q"] != r["conf_q"]]
    return len(mature), len(mature) - len(bad), bad


def window_before(day, length=30):
    """30 дней по день включительно — окно измерения долей комиссии и прочих."""
    last = date.fromisoformat(day)
    return (last - timedelta(days=length - 1)).isoformat(), last.isoformat()
