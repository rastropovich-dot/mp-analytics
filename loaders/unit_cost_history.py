"""Себестоимость по дате продажи (сорок шестая §3) и по курсу 1С (сорок восьмая §2): один читатель снимков 1С для всех отчётов Ozon
(и для WB — по тем же правилам, договор WB-17: WB добавляет только свой variant_type, интерфейс не меняет).

Правило снимка (задача сорок шестая, 2026-09-30):
  себестоимость продажи дня D = снимок с наибольшей snapshot_date ≤ D;
  ключа в этом снимке нет — ближайший БОЛЕЕ ПОЗДНИЙ снимок, где ключ есть (kind «later»: считается строками и ₽ — в примечание и лог);
  позднего тоже нет — ближайший более ранний, где ключ есть («earlier»; товар снят с производства);
  после последнего снимка — последний (пока нет новых: 20.05 → и в сентябре 20.05);
  день не задан — последний снимок («latest»: прежнее поведение читателей на одном снимке 20.05).

Правило курса (решение владельца 2026-10-07, сорок восьмая): СС артикула на дату D = СС снимка (по правилу выше) × (1 + k × Δ),
Δ = курс 1С металла на D / курс 1С металла на дату снимка − 1; металл — по пробе строки снимка (585 и 375 — золото, 925 — серебро,
METAL_OF_FINENESS); k — по пробе и виду изделия из профиля кабинета (COST_RATE_K, считает scripts/cost_gold_rate_check.py --k-profile;
вида нет — «*» пробы). Курс на дату — последняя установка ≤ дате (таблица metal_rates_1c, load_rates; для сухих прогонов —
rates_from_csv). Там, где курс с даты снимка не менялся (Δ = 0), — СС снимка без поправки, источник — дата снимка. Нет курсов, k,
пробы, дня — поправки нет, причина считается (rate_counters) и попадает в note(). Если последняя картинка бота старше D на
STALE_DAYS и больше — источник помечается «курс на <дата последней картинки>, устарел на N дней» (rate_stale_days). Индекс СС 1,150
(COST_INDEX, с 09-01) этим правилом заменён и из профилей снят.

    hist = load_history(sb, norms, rates=load_rates(sb), k_table=CABINET.COST_RATE_K)   # снимки + проба/вид по ключам, страницами
    unit_cost = unit_cost_fn(hist, sku2art)           # unit_cost(sku, day=None, qty=None) → Decimal | None, как раньше (уже по курсу)
    unit_cost.with_source(sku, day, qty)              # (СС, источник) — «<снимок>; по курсу 1С <дата> от снимка <дата>, k=…»
    unit_cost.base(sku, day, qty)                     # СС снимка без поправки (справочные колонки «по снимку»)
    hist.counters / hist.rubles / hist.rate_counters  # для примечаний

Для WB (свой порядок ключей): rate_adjust(cost, snapshot_date, day, fineness, kind) — чистая поправка без lookup; lookup_by_rate(norm, day).
Только чтение; db_writes = 0.
"""
import csv
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

TABLE = "article_unit_costs"
RATES_TABLE = "metal_rates_1c"
KINDS = ("exact", "after", "later", "earlier", "latest", "none")
KIND_TEXT = {"exact": "снимок не позже даты", "after": "день после последнего снимка — взят последний",
             "later": "ключа нет в снимке ≤ даты — взят ближайший более поздний",
             "earlier": "ключ есть только в более раннем снимке", "latest": "дата не задана — последний снимок", "none": "ключа нет ни в одном снимке"}
AFTER_LAST = "после последнего снимка"
C = Decimal("0.01")

# Проба строки снимка → металл курса 1С (бот «LL Курсы» печатает золото 585 и серебро 925; 375 в 1С = 585 × 375/585 ровно, сорок седьмая §2б).
METAL_OF_FINENESS = {"585": "gold585", "375": "gold585", "925": "silver925"}
METALS = ("gold585", "silver925", "usd")                   # что лежит в metal_rates_1c
CSV_RATE_TO_METAL = {"gold": "gold585", "silver": "silver925", "usd": "usd"}   # ll_rates_1c_history.csv → таблица (kzt, scrap не грузятся)
METAL_TEXT = {"gold585": "золото 585", "silver925": "серебро 925", "usd": "доллар"}
STALE_DAYS = 7
RATE_REASONS = ("by_rate", "rate_same", "no_day", "no_rates", "no_k_table", "no_meta", "no_fineness", "unknown_fineness", "no_rate", "no_k")
RATE_REASON_TEXT = {"by_rate": "по курсу 1С", "rate_same": "курс с даты снимка не менялся — СС снимка", "no_day": "дата не задана — без поправки",
                    "no_rates": "курсов 1С нет — без поправки", "no_k_table": "k кабинета не заданы — без поправки",
                    "no_meta": "у строки снимка нет пробы и вида — без поправки", "no_fineness": "без пробы — без поправки",
                    "unknown_fineness": "проба вне правила (не 585 / 375 / 925) — без поправки",
                    "no_rate": "нет курса на дату — без поправки", "no_k": "k для пробы не задан — без поправки"}


def source_label(snapshot_date, kind):
    """Источник себестоимости для строки книги: дата снимка, либо «после последнего снимка <дата>» (сорок шестая §3, слово владельца 10-06).
    Для later / earlier / latest — дата снимка с пометкой, почему взят не снимок ≤ дате; без снимка — «нет снимка»."""
    if snapshot_date is None:
        return "нет снимка"
    if kind == "after":
        return f"{AFTER_LAST} {snapshot_date}"
    if kind == "later":
        return f"{snapshot_date} (ключа нет в снимке ≤ даты, взят более поздний)"
    if kind == "earlier":
        return f"{snapshot_date} (ключ только в более раннем снимке)"
    if kind == "latest":
        return f"{snapshot_date} (дата не задана)"
    return str(snapshot_date)


def rate_source_label(info):
    """Суффикс источника по курсу (сорок восьмая): «; по курсу 1С <дата установки> от снимка <дата>, k=0,946»; при устаревшем курсе —
    « (курс на <дата последней картинки>, устарел на N дней)». Без поправки — пустая строка (источник = снимок)."""
    if not info or not info.get("applied"):
        return ""
    text = f"; по курсу 1С {info['rate_set_date']} от снимка {info['snapshot_date']}, k={str(info['k']).replace('.', ',')}"
    if info.get("stale_days"):
        text += f" (курс на {info['last_picture']}, устарел на {info['stale_days']} дней)"
    return text


def _days_between(a, b):
    return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days


class MetalRates:
    """История курса 1С по металлам: {metal: ([set_date …], [rate …])} по возрастанию set_date; last_picture — дата последней картинки бота
    (max seen_to), по ней считается «устарел»."""

    def __init__(self, rows):
        """rows — итерируемое dict: metal, set_date ('YYYY-MM-DD'), rate (Decimal | str), seen_to (дата картинки, до которой курс виден; может быть None)."""
        by = defaultdict(dict)
        self.last_picture = None
        for r in rows:
            metal, d = str(r["metal"]), str(r["set_date"])[:10]
            by[metal][d] = Decimal(str(r["rate"]))
            seen = r.get("seen_to") or r.get("seen_from")
            if seen and (self.last_picture is None or str(seen)[:10] > self.last_picture):
                self.last_picture = str(seen)[:10]
        self.series = {m: (sorted(v), [v[d] for d in sorted(v)]) for m, v in by.items()}

    def __bool__(self):
        return bool(self.series)

    def metals(self):
        return sorted(self.series)

    def rate_on(self, metal, day):
        """(курс, дата установки) — последняя установка ≤ day; раньше первой установки — (None, None)."""
        if metal not in self.series or not day:
            return None, None
        ds, vs = self.series[metal]
        i = bisect_right(ds, str(day)[:10]) - 1
        return (vs[i], ds[i]) if i >= 0 else (None, None)

    def stale_days(self, day, limit=STALE_DAYS):
        """На сколько дней последняя картинка старше day, если на limit и больше; иначе 0 (курс не устарел или картинок нет)."""
        if not self.last_picture or not day:
            return 0
        n = _days_between(self.last_picture, day)
        return n if n >= limit else 0

    def describe(self):
        parts = []
        for m in self.metals():
            ds, vs = self.series[m]
            parts.append(f"{METAL_TEXT.get(m, m)} {len(ds)} установок {ds[0]} … {ds[-1]}, последний {vs[-1]:,.2f}")
        return "; ".join(parts) + (f"; последняя картинка {self.last_picture}" if self.last_picture else "")


def rates_from_csv(path, pictures_csv=None):
    """MetalRates из data/ll_rates/ll_rates_1c_history.csv (сорок седьмая §1) — для сухих прогонов до посева таблицы; pictures_csv (ll_rates.csv)
    даёт дату последней картинки, без него «устарел» считается от первой картинки последней установки."""
    return MetalRates(rate_rows_from_csv(path, pictures_csv))


def rate_rows_from_csv(path, pictures_csv=None):
    """Строки для metal_rates_1c из csv истории (rate, set_date, value_1c, first_seen_img): metal, set_date, rate, source, seen_from, seen_to,
    replaced. Две строки одной даты установки (правка задним числом: доллар 20.05 75 → 74, виден с 18.06) — берётся последняя по картинке,
    прежняя — в replaced (в примечание плана). seen_to — последняя картинка, где курс ещё виден (из pictures_csv = ll_rates.csv);
    без него — день перед первой картинкой следующей установки, у последней установки — её первая картинка."""
    by = defaultdict(dict)
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            metal = CSV_RATE_TO_METAL.get(r["rate"])
            if metal is None:
                continue
            key = str(r["set_date"])[:10]
            prev = by[metal].get(key)
            cur = {"metal": metal, "set_date": key, "rate": Decimal(str(r["value_1c"])), "source": "ll_bot",
                   "seen_from": str(r["first_seen_img"])[:10], "seen_to": None, "replaced": []}
            if prev is not None:
                if cur["seen_from"] < prev["seen_from"]:
                    prev["replaced"].append((cur["rate"], cur["seen_from"]))
                    continue
                cur["replaced"] = prev["replaced"] + [(prev["rate"], prev["seen_from"])]
            by[metal][key] = cur
    last_seen = _last_seen_by_rate(pictures_csv) if pictures_csv else {}
    out = []
    for metal in sorted(by):
        rows = [by[metal][d] for d in sorted(by[metal])]
        for i, r in enumerate(rows):
            seen = last_seen.get((metal, r["set_date"], r["rate"]))
            if seen is None:
                if i + 1 < len(rows):
                    seen = date.fromordinal(date.fromisoformat(rows[i + 1]["seen_from"]).toordinal() - 1).isoformat()
                else:
                    seen = r["seen_from"]
            r["seen_to"] = max(seen, r["seen_from"])
            out.append(r)
    return out


def _last_seen_by_rate(pictures_csv):
    """{(metal, set_date, rate): последняя img_date, где картинка показывает эту пару «дата 1С, курс 1С»} из ll_rates.csv."""
    out = {}
    cols = {"gold585": ("gold_date_1c", "gold_c1"), "silver925": ("silver_date_1c", "silver_c1"), "usd": ("usd_date_1c", "usd_c1")}
    with open(pictures_csv, newline="") as fh:
        for r in csv.DictReader(fh):
            img = str(r.get("img_date") or "")[:10]
            if not img:
                continue
            for metal, (dcol, vcol) in cols.items():
                d, v = (r.get(dcol) or "").strip(), (r.get(vcol) or "").strip()
                if not d or not v or d.count(".") != 2:
                    continue
                dd, mm, yy = d.split(".")
                key = (metal, f"{yy}-{mm}-{dd}", Decimal(v))
                if img > out.get(key, ""):
                    out[key] = img
    return out


def load_rates(sb, page_cap=1000):
    """MetalRates из таблицы metal_rates_1c (все металлы, страницами по ключу (metal, set_date); таблицы нет — RuntimeError вслух, не пустой курс)."""
    rows, last = [], None
    while True:
        q = sb.table(RATES_TABLE).select("metal,set_date,rate,seen_to").order("metal").order("set_date")
        if last is not None:
            q = q.or_(f"metal.gt.{last[0]},and(metal.eq.{last[0]},set_date.gt.{last[1]})")
        try:
            data = q.limit(page_cap).execute().data or []
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{RATES_TABLE}: таблица курсов 1С не прочиталась ({type(exc).__name__}: {exc}) — миграция и посев по слову, "
                               f"scripts/load_metal_rates_1c.py") from exc
        rows += data
        if len(data) < page_cap:
            break
        last = (data[-1]["metal"], str(data[-1]["set_date"])[:10])
    return MetalRates(rows)


def k_for(k_table, fineness, kind):
    """k по пробе и виду изделия из профиля: вид → «*» пробы → None."""
    if not k_table:
        return None
    by_fin = k_table.get(str(fineness or "").strip())
    if not by_fin:
        return None
    k = by_fin.get(str(kind or "").strip())
    return k if k is not None else by_fin.get("*")


class CostHistory:
    def __init__(self, snapshots, meta=None, rates=None, k_table=None, stale_days=STALE_DAYS):
        """snapshots — {snapshot_date 'YYYY-MM-DD': {offer_id_norm: Decimal}}; meta — {snapshot_date: {norm: (metal_fineness, product_kind)}};
        rates — MetalRates | None; k_table — COST_RATE_K профиля ({} / None → без поправки)."""
        self.dates = sorted(str(d) for d in snapshots)
        self.snaps = {str(d): snapshots[d] for d in snapshots}
        self.meta = {str(d): meta[d] for d in (meta or {})}
        self.rates = rates
        self.k_table = k_table or {}
        self.stale_limit = stale_days
        self.counters = Counter()
        self.rubles = defaultdict(Decimal)
        self.rate_counters = Counter()
        self.rate_rubles = Decimal(0)        # Σ поправки по курсу (СС по курсу − СС снимка) × qty
        self.base_rubles = Decimal(0)        # Σ СС снимка × qty у строк, где qty задан
        self.stale_days_seen = set()

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
                return c, d, ("after" if day > self.dates[-1] else "exact")   # «after»: день позже последнего снимка — взят последний
        for d in self.dates[i:]:                               # ближайший более поздний
            c = self.snaps[d].get(norm)
            if c is not None:
                return c, d, "later"
        for d in reversed(self.dates[:max(i - 1, 0)]):         # более ранние (кроме уже проверенного)
            c = self.snaps[d].get(norm)
            if c is not None:
                return c, d, "earlier"
        return None, None, "none"

    def rate_adjust(self, cost, snapshot_date, day, fineness, kind):
        """(СС по курсу, info) — поправка СС снимка на Δ курса 1С металла с даты снимка до day; info: applied, reason, metal, rate_day,
        rate_set_date, rate_snap, delta, k, stale_days, last_picture, snapshot_date. Без поправки — (cost, info с причиной)."""
        info = {"applied": False, "reason": None, "snapshot_date": str(snapshot_date)[:10] if snapshot_date else None}
        if cost is None:
            info["reason"] = "none"
            return cost, info
        if day is None:
            info["reason"] = "no_day"
            return cost, info
        if not self.rates:
            info["reason"] = "no_rates"
            return cost, info
        if not self.k_table:
            info["reason"] = "no_k_table"
            return cost, info
        fin = str(fineness or "").strip()
        if not fin:
            info["reason"] = "no_fineness"
            return cost, info
        metal = METAL_OF_FINENESS.get(fin)
        if metal is None:
            info["reason"] = "unknown_fineness"
            return cost, info
        k = k_for(self.k_table, fin, kind)
        if k is None:
            info["reason"] = "no_k"
            return cost, info
        day = str(day)[:10]
        r_day, set_day = self.rates.rate_on(metal, day)
        r_snap, _snap_set = self.rates.rate_on(metal, snapshot_date)
        if r_day is None or r_snap is None or not r_snap:
            info["reason"] = "no_rate"
            return cost, info
        info.update({"metal": metal, "rate_day": r_day, "rate_set_date": set_day, "rate_snap": r_snap, "k": k,
                     "stale_days": self.rates.stale_days(day, self.stale_limit), "last_picture": self.rates.last_picture})
        delta = r_day / r_snap - 1
        info["delta"] = delta
        if delta == 0:
            info["reason"] = "rate_same"
            return cost, info
        info["applied"], info["reason"] = True, "by_rate"
        return (cost * (1 + k * delta)).quantize(C), info

    def lookup_by_rate(self, norm, day=None):
        """(СС по курсу, СС снимка, дата снимка, kind, info) — lookup + проба/вид строки снимка + rate_adjust; без счётчиков."""
        c, d, kind = self.lookup(norm, day)
        if c is None:
            return None, None, None, kind, {"applied": False, "reason": "none", "snapshot_date": None}
        fin, pk = (self.meta.get(d) or {}).get(norm) or (None, None)
        if fin is None and pk is None and self.rates and self.k_table and day is not None:
            return c, c, d, kind, {"applied": False, "reason": "no_meta", "snapshot_date": d}
        adjusted, info = self.rate_adjust(c, d, day, fin, pk)
        return adjusted, c, d, kind, info

    def cost_pair(self, norm, day=None, qty=None):
        """(СС по курсу | None, СС снимка | None, источник) с учётом в счётчиках: строки по kind и ₽ (по курсу × qty), поправка по курсу."""
        adjusted, base, d, kind, info = self.lookup_by_rate(norm, day)
        self.counters[kind] += 1
        if adjusted is not None:
            self.rate_counters[info["reason"]] += 1
            if info.get("stale_days"):
                self.stale_days_seen.add((info["last_picture"], info["stale_days"]))
            if qty is not None:
                q = Decimal(str(qty))
                self.rubles[kind] += adjusted * q
                self.base_rubles += base * q
                self.rate_rubles += (adjusted - base) * q
        return adjusted, base, source_label(d, kind) + rate_source_label(info)

    def cost_source(self, norm, day=None, qty=None):
        """(себестоимость | None, источник-строка) с учётом в счётчиках — СС уже по курсу (сорок восьмая)."""
        adjusted, _base, src = self.cost_pair(norm, day, qty)
        return adjusted, src

    def cost(self, norm, day=None, qty=None):
        """Себестоимость с учётом в счётчиках (как cost_source, без источника)."""
        return self.cost_source(norm, day, qty)[0]

    def rate_stale_days(self, day):
        """На сколько дней курс 1С устарел к дате day (0 — не устарел или курсов нет)."""
        return self.rates.stale_days(day, self.stale_limit) if self.rates else 0

    def rate_note(self):
        """Строка о поправке по курсу: курсы, строки по причинам, Σ поправки; без курсов / k — так и сказано."""
        if not self.rates:
            return "Курсов 1С нет — себестоимость по снимкам без поправки."
        if not self.k_table:
            return f"Курс 1С: {self.rates.describe()}; k кабинета не заданы — себестоимость по снимкам без поправки."
        parts = [f"{RATE_REASON_TEXT[r]}: {self.rate_counters[r]} строк" for r in RATE_REASONS if self.rate_counters.get(r)]
        text = (f"Курс 1С ({self.rates.describe()}): СС = снимок × (1 + k × Δ курса металла с даты снимка); " + ("; ".join(parts) if parts else "обращений не было")
                + (f"; поправка по курсу {self.rate_rubles:+,.2f} ₽ к {self.base_rubles:,.2f} ₽ снимка" if self.base_rubles else "") + ".")
        if self.stale_days_seen:
            worst = max(self.stale_days_seen, key=lambda x: x[1])
            text += f" ВНИМАНИЕ: курс устарел — последняя картинка {worst[0]}, до {worst[1]} дней к датам книги; снять свежий экспорт чата «LL Курсы»."
        return text

    def note(self):
        """Строка для примечаний и лога: сколько строк и ₽ пришлось на каждый вид подбора, и как лёг курс."""
        parts = []
        for kind in KINDS:
            if self.counters.get(kind):
                rub = f", {self.rubles[kind]:,.2f} ₽" if self.rubles.get(kind) else ""
                parts.append(f"{KIND_TEXT[kind]}: {self.counters[kind]} строк{rub}")
        return ("Себестоимость по дате продажи — снимки " + ", ".join(self.dates) + "; " + ("; ".join(parts) if parts else "обращений не было") + ". "
                + self.rate_note())


def load_history(sb, norms, marketplace_code="ozon", chunk=120, page_cap=1000, rates=None, k_table=None):
    """Все снимки таблицы по ключам norms: {snapshot_date: {norm: Decimal}} + проба и вид изделия строки (для поправки по курсу). Ключи — кусками
    по chunk (снимков может быть несколько на ключ, а PostgREST отдаёт не больше page_cap строк на ответ — ответ ровно в page_cap строк
    считается обрезанным и роняет чтение вслух). rates / k_table — курсы 1С и k профиля; без них история считает как сорок шестая."""
    norms = sorted({str(n).lower() for n in norms if n})
    out, meta = defaultdict(dict), defaultdict(dict)
    for i in range(0, len(norms), chunk):
        res = (sb.table(TABLE).select("offer_id_norm,snapshot_date,unit_cost,metal_fineness,product_kind").eq("marketplace_code", marketplace_code)
               .in_("offer_id_norm", norms[i:i + chunk]).order("offer_id_norm").order("snapshot_date").execute())
        data = res.data or []
        if len(data) >= page_cap:
            raise RuntimeError(f"{TABLE}: ответ на {chunk} ключей упёрся в {page_cap} строк — уменьшить chunk")
        for r in data:
            d, n = str(r["snapshot_date"]), str(r["offer_id_norm"])
            out[d][n] = Decimal(str(r["unit_cost"]))
            meta[d][n] = (r.get("metal_fineness"), r.get("product_kind"))
    return CostHistory(out, meta=meta, rates=rates, k_table=k_table)


def unit_cost_fn(history, sku2art):
    """unit_cost(sku, day=None, qty=None) для читателей: артикул по карте sku → article (из заказов), ключ — без регистра.
    unit_cost.with_source(sku, day, qty) → (себестоимость, источник) — для строк книг, которые несут источник (сорок шестая §3);
    unit_cost.with_base(sku, day, qty) → (СС по курсу, СС снимка, источник); unit_cost.base(sku, day, qty) → СС снимка без поправки
    (справочные колонки «по снимку», без учёта в счётчиках)."""
    def unit_cost(sku, day=None, qty=None):
        return unit_cost.with_source(sku, day, qty)[0]

    def _with_base(sku, day=None, qty=None):
        art = sku2art.get(str(sku))
        return history.cost_pair(art.lower(), day, qty) if art else (None, None, "нет артикула")

    def _with_source(sku, day=None, qty=None):
        adjusted, _base, src = _with_base(sku, day, qty)
        return adjusted, src

    def _base(sku, day=None, qty=None):
        art = sku2art.get(str(sku))
        return history.lookup(art.lower(), day)[0] if art else None
    unit_cost.with_source = _with_source
    unit_cost.with_base = _with_base
    unit_cost.base = _base
    unit_cost.history = history
    return unit_cost


def with_source(unit_cost, sku, day=None, qty=None):
    """(себестоимость, источник) от любого читателя: у боевого unit_cost есть .with_source, у заглушек тестов — нет (источник пустой)."""
    fn = getattr(unit_cost, "with_source", None)
    if fn is not None:
        return fn(sku, day, qty)
    return unit_cost(sku, day, qty), ""


def with_base(unit_cost, sku, day=None, qty=None):
    """(СС по курсу, СС снимка, источник) от любого читателя; у заглушек без .with_base СС снимка = СС."""
    fn = getattr(unit_cost, "with_base", None)
    if fn is not None:
        return fn(sku, day, qty)
    c = unit_cost(sku, day, qty)
    return c, c, ""
