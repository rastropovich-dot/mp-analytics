"""«Отзывы» Ozon по SKU (слово владельца 2026-10-26): типы 95 «Отзывы за баллы» (у товара) и 96 «Ускоренный сбор отзывов» (без товара).

Ozon не отдаёт сумму 96 по SKU: в начислениях by-day она NON_ITEM без SKU, в accrual/postings её нет, представитель Ozon в чате
разработчиков 11.06 — «пока никак, есть задача на новые методы». Зато /v3/product/info/list отдаёт у карточки promotions[] с
type = REVIEWS_PROMO и is_enabled — какие SKU подключены к услуге. Правило витрины по профилю кабинета (REVIEWS_96_RULE):

    no_sku         96 остаётся строкой без SKU (KARATOV, вариант (а))
    participants   96 дня — по SKU, подключённым к услуге на эту дату (снимок ≤ дате, раньше первого снимка — первый), пропорционально
                   их заказам дня (orders_amount_seller); у подключённых заказов нет — по доле SKU в 95 того же дня (вариант (б));
                   нет и 95 — строкой без SKU (РБХ, вариант (в) → (б))

Чистые функции: суммы — Decimal, разнесение до копейки, остаток округления — на SKU с наибольшей долей, Σ частей = сумме дня.
Чтение снимков участников — read_participants (страницы по ключу)."""
from bisect import bisect_right
from decimal import ROUND_HALF_UP, Decimal

C = Decimal("0.01")
RULES = ("no_sku", "participants")
TABLE = "ozon_reviews_promo_daily"


def D(v):
    return v if isinstance(v, Decimal) else Decimal(str(v or 0))


def split(amount, weights):
    """amount по весам {sku: вес > 0} до копейки; остаток округления — SKU с наибольшим весом (при равенстве — меньший sku). Пусто — {}."""
    amount = D(amount)
    weights = {k: D(w) for k, w in weights.items() if D(w) > 0}
    total = sum(weights.values(), Decimal(0))
    if not weights or total <= 0:
        return {}
    out = {k: (amount * w / total).quantize(C, rounding=ROUND_HALF_UP) for k, w in weights.items()}
    rest = amount - sum(out.values(), Decimal(0))
    if rest:
        top = sorted(weights, key=lambda k: (-weights[k], str(k)))[0]
        out[top] += rest
    return out


def participants_on(day, snapshots):
    """Подключённые SKU на дату: снимок с наибольшей датой ≤ day; раньше первого снимка — первый (истории до него нет). snapshots —
    {дата снимка: множество SKU с is_enabled}; пусто — пустое множество."""
    if not snapshots:
        return set()
    dates = sorted(snapshots)
    i = bisect_right(dates, str(day)[:10])
    return set(snapshots[dates[i - 1] if i else dates[0]])


def allocate_96(amount, participants, orders_by_sku, reviews95_by_sku):
    """96 дня по правилу «participants»: ({sku: часть}, правило). Правило — participants / by_95_share / no_sku (ничего не разнесено)."""
    w = {sku: orders_by_sku.get(sku, 0) for sku in participants}
    parts = split(amount, w)
    if parts:
        return parts, "participants"
    parts = split(amount, reviews95_by_sku)
    if parts:
        return parts, "by_95_share"
    return {}, "no_sku"


def read_participants(sb, table=TABLE):
    """{дата снимка: {sku строкой с is_enabled}} — вся таблица индексными страницами по ключу (observed_date, sku), loaders.keyset."""
    try:
        from loaders import keyset
    except ImportError:  # запуск из каталога loaders/
        import keyset
    out = {}
    for r in keyset.read_keyset(sb, table, "observed_date,sku,is_enabled", "2000-01-01", "2100-01-01", day_col="observed_date", id_col="sku"):
        day = str(r["observed_date"])[:10]
        out.setdefault(day, set())
        if r.get("is_enabled"):
            out[day].add(str(r["sku"]))
    return out


def promo_rows(items, observed_date, observed_at):
    """Строки ozon_reviews_promo_daily из ответа /v3/product/info/list: SKU карточки и её sources[].sku, is_enabled по REVIEWS_PROMO
    (нет блока — False). Один SKU — одна строка."""
    out = {}
    for it in items:
        enabled = any(p.get("type") == "REVIEWS_PROMO" and p.get("is_enabled") for p in (it.get("promotions") or []))
        skus = {str(it.get("sku") or "")} | {str(x.get("sku") or "") for x in (it.get("sources") or [])}
        for s in skus:
            if s.isdigit() and s != "0":
                prev = out.get(s)
                out[s] = {"observed_date": observed_date, "sku": int(s), "offer_id": it.get("offer_id"),
                          "is_enabled": bool(enabled or (prev and prev["is_enabled"])), "observed_at": observed_at}
    return [out[k] for k in sorted(out, key=int)]
