"""Себестоимость по дате продажи (сорок шестая §3): один читатель снимков 1С для всех отчётов Ozon (и для WB — по тем же правилам).

Правило (задача сорок шестая, 2026-09-30):
  себестоимость продажи дня D = снимок с наибольшей snapshot_date ≤ D;
  ключа в этом снимке нет — ближайший БОЛЕЕ ПОЗДНИЙ снимок, где ключ есть (kind «later»: считается строками и ₽ — в примечание и лог);
  позднего тоже нет — ближайший более ранний, где ключ есть («earlier»; товар снят с производства);
  после последнего снимка — последний (пока нет новых: 20.05 → и в сентябре 20.05);
  день не задан — последний снимок («latest»: прежнее поведение читателей на одном снимке 20.05).
Пока в article_unit_costs один снимок 2026-05-20, все даты до него дают «later» на 20.05 — числа читателей не меняются; с посевом
снимков 30.03 … 12.05 (по слову) апрель и май пересчитываются по дате автоматически.

    hist = load_history(sb, norms)                     # все снимки таблицы по нужным ключам, страницами по ключу
    unit_cost = unit_cost_fn(hist, sku2art)           # unit_cost(sku, day=None) → Decimal | None, как раньше
    hist.counters / hist.rubles                        # {kind: строк} и {kind: Σ ₽ себестоимости} — для примечаний

Только чтение; db_writes = 0.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal

TABLE = "article_unit_costs"
KINDS = ("exact", "later", "earlier", "latest", "none")
KIND_TEXT = {"exact": "снимок не позже даты", "later": "ключа нет в снимке ≤ даты — взят ближайший более поздний",
             "earlier": "ключ есть только в более раннем снимке", "latest": "дата не задана — последний снимок", "none": "ключа нет ни в одном снимке"}


class CostHistory:
    def __init__(self, snapshots):
        """snapshots — {snapshot_date 'YYYY-MM-DD': {offer_id_norm: Decimal}}."""
        self.dates = sorted(str(d) for d in snapshots)
        self.snaps = {str(d): snapshots[d] for d in snapshots}
        self.counters = Counter()
        self.rubles = defaultdict(Decimal)

    def lookup(self, norm, day=None):
        """(себестоимость | None, дата снимка | None, kind) — без учёта в счётчиках."""
        if not self.dates or not norm:
            return None, None, "none"
        if day is None:
            d = self.dates[-1]
            c = self.snaps[d].get(norm)
            return (c, d, "latest") if c is not None else (None, None, "none")
        day = str(day)[:10]
        i = bisect_right(self.dates, day)                      # self.dates[:i] — снимки ≤ day
        if i > 0:
            d = self.dates[i - 1]
            c = self.snaps[d].get(norm)
            if c is not None:
                return c, d, "exact"
        for d in self.dates[i:]:                               # ближайший более поздний
            c = self.snaps[d].get(norm)
            if c is not None:
                return c, d, "later"
        for d in reversed(self.dates[:max(i - 1, 0)]):         # более ранние (кроме уже проверенного)
            c = self.snaps[d].get(norm)
            if c is not None:
                return c, d, "earlier"
        return None, None, "none"

    def cost(self, norm, day=None, qty=None):
        """Себестоимость с учётом в счётчиках: строки по kind и ₽ (cost × qty, если qty задан)."""
        c, _d, kind = self.lookup(norm, day)
        self.counters[kind] += 1
        if c is not None and qty is not None:
            self.rubles[kind] += c * Decimal(str(qty))
        return c

    def note(self):
        """Строка для примечаний и лога: сколько строк и ₽ пришлось на каждый вид подбора."""
        parts = []
        for kind in KINDS:
            if self.counters.get(kind):
                rub = f", {self.rubles[kind]:,.2f} ₽" if self.rubles.get(kind) else ""
                parts.append(f"{KIND_TEXT[kind]}: {self.counters[kind]} строк{rub}")
        return "Себестоимость по дате продажи — снимки " + ", ".join(self.dates) + "; " + ("; ".join(parts) if parts else "обращений не было") + "."


def load_history(sb, norms, marketplace_code="ozon", chunk=120, page_cap=1000):
    """Все снимки таблицы по ключам norms: {snapshot_date: {norm: Decimal}}. Ключи — кусками по chunk (снимков может быть несколько на ключ,
    а PostgREST отдаёт не больше page_cap строк на ответ — ответ ровно в page_cap строк считается обрезанным и роняет чтение вслух)."""
    norms = sorted({str(n).lower() for n in norms if n})
    out = defaultdict(dict)
    for i in range(0, len(norms), chunk):
        res = (sb.table(TABLE).select("offer_id_norm,snapshot_date,unit_cost").eq("marketplace_code", marketplace_code)
               .in_("offer_id_norm", norms[i:i + chunk]).order("offer_id_norm").order("snapshot_date").execute())
        data = res.data or []
        if len(data) >= page_cap:
            raise RuntimeError(f"{TABLE}: ответ на {chunk} ключей упёрся в {page_cap} строк — уменьшить chunk")
        for r in data:
            out[str(r["snapshot_date"])][str(r["offer_id_norm"])] = Decimal(str(r["unit_cost"]))
    return CostHistory(out)


def unit_cost_fn(history, sku2art):
    """unit_cost(sku, day=None, qty=None) для читателей: артикул по карте sku → article (из заказов), ключ — без регистра."""
    def unit_cost(sku, day=None, qty=None):
        art = sku2art.get(str(sku))
        return history.cost(art.lower(), day, qty) if art else None
    unit_cost.history = history
    return unit_cost
