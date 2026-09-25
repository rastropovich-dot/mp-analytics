#!/usr/bin/env python3
"""Лист «Выкупы WB» книги «Фин рез» (форма второго кабинета) и строки «Данные WB выкупы» — из отчёта реализации.

    venv/bin/python3 scripts/report_finrez_wb.py --month-from 2026-09 --month-to 2026-09 --date-to 2026-09-21 --xlsx data/reports/finrez_wb_2026-09.xlsx --check
    venv/bin/python3 scripts/report_finrez_wb.py --month-from 2026-04 --month-to 2026-09 --xlsx data/reports/finrez_wb_2026-04_09.xlsx

ДОГОВОР С OZON-СБОРКОЙ (scripts/report_finrez.py импортирует этот модуль; сигнатуры не менять без записи в отчёт):
    build_rows(month_from, month_to, date_to=None, sb=None)  -> список строк «Данные WB выкупы» (dict по DATA_COLS)
    build_month_sheet(rows)                                   -> строки листа «Выкупы WB»: месяцы + «Итого» (dict по SHEET_COLS)
    build_split(rows, by='brand'|'category')                  -> те же колонки в разрезе бренд × месяц / категория × месяц (+ итог группы)
    SHEET_COLS, DATA_COLS                                     -> (заголовок, ключ, формат) для записи листов
    load_funnel_products(date_from, date_to, sb=None)         -> строки wb_funnel_products_daily за окно (для общего листа «Заказы»)
    orders_rows_for_finrez(date_from, date_to, sb=None)       -> строки заказов WB по (день, nmId) для общего листа «Заказы»
    buyout_rate_for_finrez(month_from, month_to, sb=None)     -> {ярлык месяца: Decimal, "итого": Decimal} — коэффициент выкупа WB (₽, когорта
                                                                 месяца заказа, только зрелые месяцы) для листа «Коэффициенты» (WB-9 §3)
    Ярлыки бренда и категории — одни с Ozon (WB-9 §2): KARATOV / Топаз / «(без товара)»; кольца · серьги · подвески · цепочки · браслеты ·
    пирсинг · колье · броши · прочее (BRAND_LABELS, CATEGORY_LABELS).

ФОРМА (образец — книга второго кабинета `data/owner_finrez_wb_buyouts.xlsx`, лист «Свод», сводная без формул; тождества
проверены по значениям на четырёх месяцах, WB-8 §2): B Продажи · C Комиссия · D Себес-ть · E Реклама · F Хранение ·
G Логистика · H Ост. расходы и компенсации МП (все с НДС, ₽) и K … Z:
    K = B;  L = C;  M = L / K;  N = (K − L) / НДС;  O = D;  P = N − O;  Q = P / N;
    R = (G + F) / НДС  ← «Логистика без НДС» второго кабинета ВКЛЮЧАЕТ хранение;  S = R / N;
    T = E / НДС;  U = T / N;  V = эквайринг без НДС (у второго кабинета 0 — у нас заполнен);  W = V / N;
    X = H / НДС;  Y = P − R − T − V − X;  Z = Y / N.
Строки образца — месяцы (у свежих месяцев ещё и дни) и «Общий итог»; здесь — месяцы и «Итого».

«ДАННЫЕ WB ВЫКУПЫ» — строка на (дата продажи saleDt МСК, nmId), как на листе «Данные» книги владельца (задача WB-8 §2.1):
    Продажи = Σ retailPriceWithDisc (Продажа +, Возврат −); Комиссия = Σ цена × commissionPercent / 100 (знак тот же);
    Логистика = deliveryService (rebillLogisticCost — «возмещение издержек по перевозке — не берём», инструкция владельца;
    справочной колонкой в «Данных», в форме не участвует); Себес-ть = СС снимка 1С (базовый артикул, как на «WB - месяц») × штуки;
    Хранение = paidStorage; Ост. расходы и компенсации = penalty + deduction − additionalPayment; Эквайринг = acquiringFee
    (возврат минус); Соинвест = Σ retailPriceWithDisc − Σ retailAmount; Штуки со знаком; Реклама — списания дня из
    wb_ad_spend_daily (updSum — истина по деньгам, биллинг), разнесённые по артикулам ДОЛЯМИ из статистики по номенклатурам
    (wb_ad_spend_nm_daily, fullstats: Σ nms.sum по дню выше updSum на ~1,7 % — доли нормируются к updSum дня, Σ по дню =
    списаниям; решение советника WB-8), а где статистики за день нет — пропорционально продажам дня (ads_allocated: «fullstats»
    / «по продажам»). Категория — subject_name строки отчёта (колонки с 09-24), откат — карточка воронки / product_name выкупов. Строки отчёта без nmId (возмещение ПВЗ, хранение, удержания) — строка «(без товара)» за
    день, чтобы Σ = «WB - месяц». Справочно: Возмещение ПВЗ (ppvzReward) — в форму не входит, но нужен мосту к «WB - месяц».

ПРИЁМКА (--check): за период сравнивается с итогом report_wb_month.build_daily по тем же строкам отчёта — оборот,
комиссия, эквайринг, СС — до копейки; логистика, прочее, выручка и фин. рез. — через мост с названными слагаемыми
(хранение внутри R у второго кабинета; штрафы у владельца без НДС, здесь H / НДС; additionalPayment вычтен из H;
НДС за возмещение вычитается из выручки только у владельца), остаток моста обязан быть 0,00.
Только чтение; db_writes = 0.
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from loaders import keyset, stale_keys  # noqa: E402
import loaders.wb_sales_report_loader as report_loader  # noqa: E402
import report_wb_month as wbm  # noqa: E402  — те же строки отчёта, СС, реклама и НДС, что у «WB - месяц»

Z = Decimal(0)
C2 = Decimal("0.01")
PLATFORM = "WB"
SHOP = "KARATOV"                       # кабинет; имя магазина в книге владельца — уточнить (WB-8 §2 вопрос)
DISCOUNTER_LETTER = "t"
# Ярлыки бренда и категории — одни на обе площадки, как у Ozon-скрипта (WB-9 §2): бренд по первой букве артикула
# (t → «Топаз», иначе KARATOV; бренд WB — только когда артикула нет, и тогда нормализуется), категория — словарь владельца
# строчными (= scripts/ozon_product_catalog.OWNER_CATEGORIES, тест на равенство).
BRAND_TOPAZ = "Топаз"
BRAND_BY_LETTER = {DISCOUNTER_LETTER: BRAND_TOPAZ}
BRAND_DEFAULT = "KARATOV"
BRAND_NORMALIZE = {"коюз топаз": BRAND_TOPAZ, "топаз": BRAND_TOPAZ, "karatov": BRAND_DEFAULT}
UNIDENTIFIED = "неопознанный товар"    # артикул и бренд WB у операций без опознанного товара — считаем строками без товара
NO_PRODUCT = "(без товара)"
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
OWNER_CATEGORIES = ("кольца", "серьги", "подвески", "цепочки", "браслеты", "пирсинг", "колье", "броши")   # = ozon_product_catalog.OWNER_CATEGORIES
# Предмет WB → категория владельца; всё остальное — «прочее», считается вслух (в данных на 09-25: иконы, упаковки, запонки).
CATEGORY_BY_SUBJECT = {
    "Ювелирные кольца": "кольца", "Ювелирные серьги": "серьги", "Ювелирные подвески": "подвески", "Ювелирные цепочки": "цепочки",
    "Ювелирные браслеты": "браслеты", "Ювелирный пирсинг": "пирсинг", "Ювелирные колье": "колье", "Ювелирные броши": "броши",
}
CATEGORY_OTHER = "прочее"
CATEGORY_UNKNOWN = "прочее (нет предмета)"
BRAND_LABELS = frozenset({BRAND_DEFAULT, BRAND_TOPAZ, NO_PRODUCT})
CATEGORY_LABELS = frozenset(OWNER_CATEGORIES) | {CATEGORY_OTHER, CATEGORY_UNKNOWN, NO_PRODUCT}
SELECT = wbm.SELECT + ",additional_payment,subject_name,brand_name"   # поля «WB - месяц» (мост зовёт его build_daily) + доплаты, предмет, бренд
# build_rows читает только то, что использует: без doc_type, for_pay, cashback_discount (они нужны мосту через build_daily — main читает SELECT). WB-9 §4.
BUILD_SELECT = ("rrd_id,rr_date,sale_dt,seller_oper_name,vendor_code,tech_size,nm_id,quantity,retail_price_with_disc,retail_amount,commission_percent,"
                "ppvz_reward,rebill_logistic_cost,delivery_service,acquiring_fee,paid_storage,penalty,deduction,additional_payment,subject_name,brand_name")
ADS_NM_TABLE = "wb_ad_spend_nm_daily"
FUNNEL_SELECT = "day,nm_id,vendor_code,title,brand,subject_name,order_count,order_sum,buyout_count,buyout_sum,cancel_count,cancel_sum"

# «Данные WB выкупы» — первые 15 полей ровно как в кэше сводной владельца (WB-8 §6, дополнение владельца 24.09):
# «Статус» = категория (статус у владельца не задействован), «Месяцы» = номер месяца; наши добавки — справа после 15-го.
OWNER_DATA_FIELDS = ("Дата", "Месяц", "Магазин", "Артикул поставщика", "Продажи, ₽", "Реклама, ₽", "Комиссия, ₽", "Логистика, ₽", "Себес-ть, ₽",
                     "Хранение, ₽", "Ост.расходы и компенсации МП, ₽", "Наименование", "Бренд", "Статус", "Месяцы")
DATA_COLS = [
    ("Дата", "date", "date"), ("Месяц", "month", None), ("Магазин", "shop", None), ("Артикул поставщика", "article", None),
    ("Продажи, ₽", "sales", "money"), ("Реклама, ₽", "ads", "money"), ("Комиссия, ₽", "commission", "money"), ("Логистика, ₽", "logistics", "money"),
    ("Себес-ть, ₽", "cogs", "money"), ("Хранение, ₽", "storage", "money"), ("Ост.расходы и компенсации МП, ₽", "other", "money"),
    ("Наименование", "title", None), ("Бренд", "brand", None), ("Статус", "category", None), ("Месяцы", "month_no", "int"),
    ("nmId", "nm_id", "int"), ("Эквайринг, ₽", "acquiring", "money"), ("Соинвест, ₽", "coinvest", "money"), ("Штуки", "qty", "int"),
    ("справочно: Возмещение ПВЗ, ₽ (в форму не входит)", "ppvz_reward", "money"), ("справочно: возмещение издержек по перевозке (rebillLogisticCost), ₽ — не берём", "rebill", "money"),
    ("реклама разнесена по", "ads_allocated", None), ("без СС, шт", "no_cost_qty", "int"),
]
assert tuple(h for h, _k, _f in DATA_COLS[:15]) == OWNER_DATA_FIELDS
SHEET_COLS = [
    ("Названия строк", "label", None),
    ("Продажи, ₽", "sales", "money"), ("Комиссия, ₽", "commission", "money"), ("Себес-ть, ₽", "cogs", "money"), ("Реклама, ₽", "ads", "money"),
    ("Хранение, ₽", "storage", "money"), ("Логистика, ₽", "logistics", "money"), ("Ост.расходы и компенсации МП, ₽", "other", "money"),
    (None, None, None), (None, None, None),
    ("Оборот (с НДС)", "k_turnover", "money"), ("Комиссия (с НДС), руб.", "l_commission", "money"), ("Комиссия, %", "m_commission_pct", "pct"),
    ("Выручка, руб. (без НДС)", "n_revenue", "money"), ("Себестоимость, руб.", "o_cogs", "money"), ("Маржа, руб.", "p_margin", "money"), ("Мар-ть, %", "q_margin_pct", "pct"),
    ("Логистика, руб. (без НДС)", "r_logistics", "money"), ("% Логистики", "s_logistics_pct", "pct"), ("Реклама, руб. (без НДС)", "t_ads", "money"), ("% ДРР", "u_drr_pct", "pct"),
    ("Эквайринг, руб.", "v_acquiring", "money"), ("% Эквайринга", "w_acquiring_pct", "pct"), ("Прочее, руб. (без НДС)", "x_other", "money"),
    ("Фин. рез., руб.", "y_fin", "money"), ("% Фин. рез.", "z_fin_pct", "pct"),
]
SPLIT_COLS = [("Группа", "group", None)] + SHEET_COLS
MONEY_KEYS = ("sales", "ads", "commission", "logistics", "cogs", "storage", "other", "acquiring", "coinvest", "ppvz_reward", "rebill")


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Z


def q(v):
    return Decimal(v).quantize(C2)


def ratio(a, b):
    return (Decimal(a) / Decimal(b)).quantize(Decimal("0.0001")) if b else None


def month_label(day):
    return MONTHS_SHORT[int(str(day)[5:7]) - 1]


def month_bounds(month_from, month_to, date_to=None):
    d1 = f"{month_from}-01"
    y, m = int(month_to[:4]), int(month_to[5:7])
    last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).isoformat()
    if date_to:
        last = min(last, date_to)
    return d1, last


def is_unidentified(vendor_code=None, brand=None):
    """«Неопознанный товар» WB (артикул или бренд) — операции без опознанного товара, как строки без nmId."""
    return str(vendor_code or "").strip().lower() == UNIDENTIFIED or str(brand or "").strip().lower() == UNIDENTIFIED


def brand_of(vendor_code, funnel_brand=None):
    """Ярлык бренда владельца: первая буква артикула первична (t → «Топаз», иначе KARATOV); бренд WB (строка отчёта /
    карточка) — только когда артикула нет, и тогда нормализуется («КОЮЗ Топаз» → «Топаз»), неизвестный бренд без
    артикула — KARATOV; «Неопознанный товар» — «(без товара)»."""
    code = str(vendor_code or "").strip()
    if is_unidentified(code, funnel_brand):
        return NO_PRODUCT
    if code:
        return BRAND_BY_LETTER.get(code[:1].lower(), BRAND_DEFAULT)
    return BRAND_NORMALIZE.get(str(funnel_brand or "").strip().lower(), BRAND_DEFAULT)


def category_of(subject_name):
    if not subject_name:
        return CATEGORY_UNKNOWN
    return CATEGORY_BY_SUBJECT.get(subject_name, CATEGORY_OTHER)


# ---------- источники ----------

def _sb(sb):
    return sb or report_loader._client()


def load_report_rows(sb, d1, d2, select=SELECT):
    """Строки отчёта по rrDate d1 … d2 + запас под дату продажи (как в report_wb_month); страницы по ключу
    (rr_date, rrd_id), без offset — offset на 330 тыс. строк дорожает с глубиной (WB-9 §4)."""
    return read_keyset(sb, report_loader.TABLE, select, d1, wbm.window_end(d2), day_col="rr_date", id_col="rrd_id")


DICT_PAGE = 1_000   # PostgREST отдаёт не больше 1 000 строк на ответ, limit больше — молча урежет (how-we-work); страницы — по ключу


def read_keyset(sb, table, select, d1, d2, page=None, day_col="day", id_col="nm_id"):
    """Страницы по первичному ключу (day, id) только индексными запросами — loaders.keyset (WB-10 §3: прежняя форма
    or(day.gt, and(day.eq, id.gt)) упиралась в statement_timeout на глубоких страницах). Только окно дней d1 … d2;
    page — по умолчанию DICT_PAGE."""
    return keyset.read_keyset(sb, table, select, d1, d2, day_col=day_col, id_col=id_col, page=page or DICT_PAGE)


def fetch_subjects_for(sb, nm_ids, batch=200):
    """Предмет и бренд для nmId, которых нет ни в строках окна, ни в воронке: адресно из wb_sales_report_rows
    (любая строка с subject_name — колонки с WB-8), пачками по batch id. {nmId: {subject, brand}}."""
    out = {}
    ids = sorted({int(n) for n in nm_ids})
    for i in range(0, len(ids), batch):
        try:
            data = (sb.table(report_loader.TABLE).select("nm_id,subject_name,brand_name").in_("nm_id", ids[i:i + batch])
                    .not_.is_("subject_name", "null").order("nm_id").order("rr_date", desc=True).limit(DICT_PAGE).execute().data or [])
        except Exception as error:
            print(f"предмет по nmId из {report_loader.TABLE}: не прочитать — {str(error)[:120]}", flush=True)
            return out
        for r in data:
            nm = int(r["nm_id"])
            if nm not in out:
                out[nm] = {"subject": r.get("subject_name"), "brand": r.get("brand_name")}
    return out


FUNNEL_LATEST_VIEW = "wb_funnel_products_latest"   # sql/20260926_create_wb_funnel_products_latest.sql — по слову; нет вьюхи → чтение таблицы окном


def _missing_relation(error):
    """PostgREST про несуществующую таблицу/вьюху: PGRST205 (нет в кэше схемы) или 42P01 (relation does not exist)."""
    text = str(error)
    return "PGRST205" in text or "42P01" in text or "does not exist" in text


def read_latest_cards(sb, page=None):
    """Строки вьюхи «последняя карточка по nmId» страницами по ключу nm_id (одна строка на nmId). Нет вьюхи — None."""
    page = page or DICT_PAGE
    out, last = [], None
    while True:
        qb = sb.table(FUNNEL_LATEST_VIEW).select("nm_id,day,vendor_code,title,brand,subject_name").order("nm_id").limit(page)
        if last is not None:
            qb = qb.gt("nm_id", last)
        try:
            data = qb.execute().data or []
        except Exception as error:
            if _missing_relation(error):
                return None
            raise
        out.extend(data)
        if len(data) < page:
            return out
        last = int(data[-1]["nm_id"])


def load_product_dictionary(sb, d1=None, d2=None, use_view=True):
    """nmId → {title, brand, subject, vendor_code}: сначала вьюха wb_funnel_products_latest (последняя карточка по nmId,
    ~6 страниц; WB-10 §3.1), нет вьюхи — воронка по товарам за окно книги d1 … d2 страницами по ключу (последний день
    карточки побеждает; 493 страницы за 6 месяцев). nmId, которых в воронке нет, получают предмет и бренд из строки отчёта
    реализации (subject_name / brand_name с WB-8) — это делает build_rows, словарь тут ни при чём."""
    out = {}
    if not (d1 and d2):
        return out
    started = datetime.now(timezone.utc)
    if use_view:
        try:
            rows = read_latest_cards(sb)
        except Exception as error:
            print(f"словарь товаров: вьюха {FUNNEL_LATEST_VIEW} не прочитана — {str(error)[:160]}; читаю {wbm.FUNNEL_TABLE} окном", flush=True)
            rows = None
        if rows is not None:
            for r in rows:
                out[int(r["nm_id"])] = {"title": r.get("title"), "brand": r.get("brand"), "subject": r.get("subject_name"), "vendor_code": r.get("vendor_code")}
            print(f"словарь товаров: вьюха {FUNNEL_LATEST_VIEW} — строк {len(rows)}, nmId {len(out)}, "
                  f"{(datetime.now(timezone.utc) - started).total_seconds():.1f} с, страниц по {DICT_PAGE}: {max(1, (len(rows) + DICT_PAGE - 1) // DICT_PAGE)}", flush=True)
            return out
        print(f"словарь товаров: вьюхи {FUNNEL_LATEST_VIEW} нет (миграция по слову) — читаю {wbm.FUNNEL_TABLE} за {d1} … {d2} окном", flush=True)
    try:
        rows = read_keyset(sb, wbm.FUNNEL_TABLE, "day,nm_id,vendor_code,title,brand,subject_name", d1, d2)
    except Exception as error:
        print(f"словарь товаров: не прочитать {wbm.FUNNEL_TABLE} за {d1} … {d2} — {str(error)[:160]}; название — пусто, предмет и бренд — из строк отчёта", flush=True)
        return out
    for r in rows:   # отсортировано по day, nm_id — более поздний день побеждает
        out[int(r["nm_id"])] = {"title": r.get("title"), "brand": r.get("brand"), "subject": r.get("subject_name"), "vendor_code": r.get("vendor_code")}
    print(f"словарь товаров: {wbm.FUNNEL_TABLE} {d1} … {d2} — строк {len(rows)}, nmId {len(out)}, "
          f"{(datetime.now(timezone.utc) - started).total_seconds():.1f} с, страниц по {DICT_PAGE}: {max(1, (len(rows) + DICT_PAGE - 1) // DICT_PAGE)}", flush=True)
    return out


def load_ads_nm_db(sb, d1, d2):
    """{день: {nmId: Σ sum}} из wb_ad_spend_nm_daily (fullstats, с НДС) — доли для разнесения списаний дня. Таблицы нет — {} вслух."""
    try:   # sum > 0: строки с нулём в доли не входят (weights берут только v > 0), отрицательных в таблице нет (min 0, WB-9 §4) — треть страниц долой
        rows = stale_keys.read_window_rows(sb, ADS_NM_TABLE, "day,nm_id,sum", [("gte", "day", d1), ("lte", "day", d2), ("gt", "sum", 0)], ["day", "advert_id", "app_type", "nm_id"])
    except Exception as error:
        print(f"статистика по номенклатурам не прочитана ({ADS_NM_TABLE}): {str(error)[:120]} — реклама разносится по продажам дня", flush=True)
        return {}
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        out[str(r["day"])][int(r["nm_id"])] += D(r.get("sum"))
    return {d: dict(v) for d, v in out.items()}


# ---------- «Данные WB выкупы» ----------

def build_rows(month_from, month_to, date_to=None, sb=None, rows=None, costs=None, ads_by_day=None, products=None, ads_nm_by_day=None, stats=None):
    """Строки «Данные WB выкупы» за месяцы month_from … month_to (до date_to включительно, если задано).

    rows / costs / ads_by_day / products — для тестов и повторных сборок; по умолчанию читаются из базы.
    stats (dict) получает счётчики ярлыков (categories, brands, rows_unknown_category) и строки «Неопознанный товар» (unidentified)."""
    sb = _sb(sb) if rows is None or costs is None or ads_by_day is None or products is None else sb
    d1, d2 = month_bounds(month_from, month_to, date_to)
    rows = load_report_rows(sb, d1, d2, select=BUILD_SELECT) if rows is None else rows
    exact, uniform = wbm.load_costs(sb) if costs is None else costs
    if ads_by_day is None:
        try:
            ads_by_day, _undated = wbm.load_ads_db(sb, d1, d2)
        except RuntimeError as error:
            print(f"реклама не прочитана: {error}; колонка «Реклама» пуста", flush=True)
            ads_by_day = None
    products = load_product_dictionary(sb, d1, d2) if products is None else products
    ads_nm_by_day = load_ads_nm_db(sb, d1, d2) if (ads_nm_by_day is None and ads_by_day is not None) else (ads_nm_by_day or {})
    nm_subject = {}                                       # nmId → (предмет, бренд) из ЛЮБОЙ загруженной строки отчёта (окно + запас)
    for r in rows:
        nm = r.get("nm_id")
        if nm not in (None, "", 0, "0") and r.get("subject_name") and int(nm) not in nm_subject:
            nm_subject[int(nm)] = (r["subject_name"], r.get("brand_name"))
    unidentified = {"rows": 0, "days": set(), "sales": Z, "logistics": Z, "rebill": Z, "storage": Z, "other": Z}   # «Неопознанный товар» → «(без товара)»
    acc = {}
    for r in rows:
        day = wbm.row_day(r)
        if not (d1 <= day <= d2):
            continue
        nm = r.get("nm_id")
        nm = int(nm) if nm not in (None, "", 0, "0") else None
        if nm is not None and is_unidentified(r.get("vendor_code"), r.get("brand_name")):   # WB-9 §2: как строки без товара, вслух
            u = unidentified
            u["rows"] += 1; u["days"].add(day)
            u["sales"] += (1 if r["seller_oper_name"] == "Продажа" else -1) * D(r.get("retail_price_with_disc")) if r["seller_oper_name"] in ("Продажа", "Возврат") else Z
            u["logistics"] += D(r.get("delivery_service")); u["rebill"] += D(r.get("rebill_logistic_cost")); u["storage"] += D(r.get("paid_storage"))
            u["other"] += D(r.get("penalty")) + D(r.get("deduction")) - D(r.get("additional_payment"))
            nm = None
        key = (day, nm)
        a = acc.get(key)
        if a is None:
            a = acc[key] = {k: Z for k in MONEY_KEYS}
            a.update({"qty": 0, "no_cost_qty": 0, "vendor": str(r.get("vendor_code") or ""), "subject": None, "brand": None})
        if not a["subject"] and r.get("subject_name"):
            a["subject"] = r["subject_name"]
        if not a["brand"] and r.get("brand_name"):
            a["brand"] = r["brand_name"]
        op = r["seller_oper_name"]
        sign = 1 if op == "Продажа" else (-1 if op == "Возврат" else 0)
        if sign:
            price, amount, qty = D(r["retail_price_with_disc"]), D(r["retail_amount"]), int(r.get("quantity") or 1)
            a["sales"] += sign * price
            a["commission"] += sign * price * D(r.get("commission_percent")) / 100
            a["coinvest"] += sign * (price - amount)
            a["acquiring"] += sign * D(r.get("acquiring_fee"))
            a["qty"] += sign * qty
            cost, _source = wbm.unit_cost_for(exact, uniform, r.get("vendor_code"), r.get("tech_size"), "base")
            if cost is None:
                a["no_cost_qty"] += qty
            else:
                a["cogs"] += sign * cost * qty
        a["logistics"] += D(r.get("delivery_service"))            # rebillLogisticCost — не берём (инструкция владельца), справочно ниже
        a["rebill"] += D(r.get("rebill_logistic_cost"))
        a["storage"] += D(r.get("paid_storage"))
        a["other"] += D(r.get("penalty")) + D(r.get("deduction")) - D(r.get("additional_payment"))
        a["ppvz_reward"] += D(r.get("ppvz_reward"))
    # реклама дня — по артикулам пропорционально положительным продажам; без продаж — на строку «(без товара)»
    by_day = defaultdict(list)
    for key in acc:
        by_day[key[0]].append(key)
    if ads_by_day is not None:                       # день со списаниями без единой строки отчёта — тоже день: реклама не теряется
        for day in ads_by_day:
            if d1 <= day <= d2 and day not in by_day and ads_by_day[day]:
                by_day[day] = []
    ads_source = {}
    for day, keys in sorted(by_day.items()):
        ads = (ads_by_day or {}).get(day, Z) if ads_by_day is not None else None
        if ads is None:
            continue
        nm_shares = {nm_id: v for nm_id, v in (ads_nm_by_day.get(day) or {}).items() if v > 0}
        if ads and nm_shares:                                 # доли из fullstats, нормированные к списаниям дня (истина по деньгам — updSum)
            weights, source = {(day, nm_id): v for nm_id, v in nm_shares.items()}, "fullstats"
        else:
            weights, source = {k: acc[k]["sales"] for k in keys if k[1] is not None and acc[k]["sales"] > 0}, "по продажам"
        total = sum(weights.values(), Z)
        if ads and total:
            spent = Z
            ordered = sorted(weights, key=lambda k: k[1])
            for k in ordered:
                if k not in acc:                              # реклама у товара без продаж в этот день — своя строка
                    a = acc[k] = {kk: Z for kk in MONEY_KEYS}
                    a.update({"qty": 0, "no_cost_qty": 0, "vendor": "", "subject": None, "brand": None})
            for k in ordered[:-1]:
                share = q(ads * weights[k] / total)
                acc[k]["ads"] += share; spent += share
            acc[ordered[-1]]["ads"] += ads - spent            # остаток копеек — последнему, Σ по дню = списания дня
            ads_source[day] = source
        elif ads:
            a = acc.get((day, None))
            if a is None:
                a = acc[(day, None)] = {k: Z for k in MONEY_KEYS}
                a.update({"qty": 0, "no_cost_qty": 0, "vendor": "", "subject": None, "brand": None})
            a["ads"] += ads
            ads_source[day] = "(без товара)"
    if unidentified["rows"]:
        print(f"«{UNIDENTIFIED}» в строках отчёта: {unidentified['rows']} строк за {len(unidentified['days'])} дн., продажи {unidentified['sales']:,.2f}, "
              f"логистика {unidentified['logistics']:,.2f}, rebill {unidentified['rebill']:,.2f}, хранение {unidentified['storage']:,.2f}, прочее {unidentified['other']:,.2f} "
              f"— учтены в строках «{NO_PRODUCT}»", flush=True)
    missing = sorted({nm for (_d, nm), a in acc.items() if nm is not None and not (a["subject"] or nm_subject.get(nm) or products.get(nm, {}).get("subject"))})
    fetched = fetch_subjects_for(sb, missing) if (missing and hasattr(sb, "table")) else {}
    if missing:
        print(f"предмет не найден ни в строках окна, ни в воронке у {len(missing)} nmId; адресно из отчёта добрано {len(fetched)}", flush=True)
    out = []
    for (day, nm), a in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        p = products.get(nm, {}) if nm is not None else {}
        if nm is not None:
            fb = nm_subject.get(nm) or (fetched.get(nm, {}).get("subject"), fetched.get(nm, {}).get("brand"))
            a["subject"] = a["subject"] or fb[0] or p.get("subject")
            a["brand"] = a["brand"] or fb[1] or p.get("brand")
        vendor = a["vendor"] or (p.get("vendor_code") or "")
        row = {"date": day, "month": month_label(day), "month_no": int(day[5:7]), "platform": PLATFORM, "shop": SHOP,
               "article": (vendor.upper() or None) if nm is not None else NO_PRODUCT, "nm_id": nm,
               "title": p.get("title") if nm is not None else NO_PRODUCT,
               "brand": brand_of(vendor, a["brand"] or p.get("brand")) if nm is not None else NO_PRODUCT,
               "category": category_of(a["subject"] or p.get("subject")) if nm is not None else NO_PRODUCT,
               "ads_allocated": (ads_source.get(day) if (ads_by_day is not None and a["ads"]) else ""), "qty": a["qty"], "no_cost_qty": a["no_cost_qty"]}
        for k in MONEY_KEYS:
            row[k] = a[k] if (k != "ads" or ads_by_day is not None) else None
        out.append(row)
    if stats is not None:
        cats, brands = defaultdict(int), defaultdict(int)
        for r in out:
            cats[r["category"]] += 1; brands[r["brand"]] += 1
        stats.update({"categories": dict(cats), "brands": dict(brands), "rows_unknown_category": cats.get(CATEGORY_UNKNOWN, 0),
                      "unidentified": {k: (sorted(v) if isinstance(v, set) else v) for k, v in unidentified.items()}})
    return out


# ---------- «Выкупы WB» ----------

def _form(label, s, vat):
    """Колонки K … Z второго кабинета из сумм с НДС (тождества образца)."""
    k, l, o = s["sales"], s["commission"], s["cogs"]
    n = (k - l) / vat
    p = n - o
    r = (s["logistics"] + s["storage"]) / vat
    t = (s["ads"] / vat) if s["ads"] is not None else None
    v = s["acquiring"] / vat
    x = s["other"] / vat
    y = p - r - (t or Z) - v - x
    return {"label": label, "sales": k, "commission": l, "cogs": o, "ads": s["ads"], "storage": s["storage"], "logistics": s["logistics"], "other": s["other"],
            "k_turnover": k, "l_commission": l, "m_commission_pct": ratio(l, k), "n_revenue": n, "o_cogs": o, "p_margin": p, "q_margin_pct": ratio(p, n),
            "r_logistics": r, "s_logistics_pct": ratio(r, n), "t_ads": t, "u_drr_pct": ratio(t, n) if t is not None else None,
            "v_acquiring": v, "w_acquiring_pct": ratio(v, n), "x_other": x, "y_fin": y, "z_fin_pct": ratio(y, n),
            "acquiring": s["acquiring"], "vat": vat, "rows": s["rows"], "qty": s["qty"], "coinvest": s["coinvest"]}


def _sum_rows(rows):
    s = {k: Z for k in ("sales", "commission", "cogs", "storage", "logistics", "other", "acquiring", "coinvest")}
    s["ads"] = Z if any(r.get("ads") is not None for r in rows) else None
    s["rows"], s["qty"] = 0, 0
    for r in rows:
        for k in ("sales", "commission", "cogs", "storage", "logistics", "other", "acquiring", "coinvest"):
            s[k] += D(r.get(k))
        if s["ads"] is not None and r.get("ads") is not None:
            s["ads"] += D(r["ads"])
        s["rows"] += 1; s["qty"] += int(r.get("qty") or 0)
    return s


def _vat_for_rows(rows):
    return wbm.vat_for(min(r["date"] for r in rows)) if rows else wbm.vat_for("2026-01-01")


def build_month_sheet(rows):
    """Строки листа «Выкупы WB»: по месяцам (в порядке дат) и «Итого»."""
    by_month = defaultdict(list)
    for r in rows:
        by_month[str(r["date"])[:7]].append(r)
    out = [_form(month_label(f"{m}-01"), _sum_rows(rs), wbm.vat_for(f"{m}-01")) for m, rs in sorted(by_month.items())]
    out.append(_form("Итого", _sum_rows(rows), _vat_for_rows(rows)))
    return out


def build_split(rows, by="brand"):
    """Разрез группа × месяц (+ «Итого» группы), группа — brand или category; колонки те же, плюс «Группа»."""
    if by not in ("brand", "category"):
        raise ValueError("by: brand | category")
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[r.get(by) or "—"][str(r["date"])[:7]].append(r)
    out = []
    for g in sorted(groups):
        all_rows = []
        for m, rs in sorted(groups[g].items()):
            row = _form(month_label(f"{m}-01"), _sum_rows(rs), wbm.vat_for(f"{m}-01")); row["group"] = g; out.append(row); all_rows.extend(rs)
        total = _form("Итого", _sum_rows(all_rows), _vat_for_rows(all_rows)); total["group"] = g; out.append(total)
    return out


# ---------- §4: воронка по товарам для общего листа «Заказы» ----------

ACTIVE_FUNNEL_FILTER = "order_count.gt.0,order_sum.gt.0"   # WB-10 §3.2: только карточки с заказами — 22 719 строк из 492 128 за 04-01 … 09-24


def load_funnel_products(date_from, date_to, sb=None, only_with_orders=False):
    """Строки wb_funnel_products_daily за окно (день заказа МСК), отсортированные по ключу; страницы — индексными запросами
    (loaders.keyset): offset-страницы на 492 тыс. строк умирают на глубине по statement_timeout (57014 — у Ozon-сессии
    09-25 и в приёмке WB-10). only_with_orders=True — фильтр на сервере `or(order_count.gt.0,order_sum.gt.0)` (WB-10 §3.2):
    ~23 страницы вместо 493 за 6 месяцев; карточки без заказов, но с рекламой номенклатуры тогда собирает
    orders_rows_for_finrez из словаря (products=)."""
    filters = [("or_", ACTIVE_FUNNEL_FILTER)] if only_with_orders else None
    return keyset.read_keyset(_sb(sb), wbm.FUNNEL_TABLE, FUNNEL_SELECT, date_from, date_to, day_col="day", id_col="nm_id", page=DICT_PAGE, filters=filters)


def nm_ads_by_day_and_nm(ads_by_day, ads_nm_by_day):
    """{(день, nmId): реклама ₽} — списания дня (updSum) × доля номенклатуры из fullstats (Σ по дню = списаниям)."""
    out = {}
    for day, shares in (ads_nm_by_day or {}).items():
        total_day = (ads_by_day or {}).get(day, Z)
        weights = {nm: v for nm, v in shares.items() if v > 0}
        total = sum(weights.values(), Z)
        if not total_day or not total:
            continue
        spent = Z
        ordered = sorted(weights)
        for nm in ordered[:-1]:
            share = q(total_day * weights[nm] / total); out[(day, nm)] = share; spent += share
        out[(day, ordered[-1])] = total_day - spent
    return out


def orders_rows_for_finrez(date_from, date_to, sb=None, funnel_rows=None, costs=None, ads_by_day=None, ads_nm_by_day=None, keep_empty=False, stats=None,
                           products=None, only_with_orders=False):
    """Заказы WB по (день, nmId) для общего листа «Заказы»: созданные из воронки, выручка по правилу владельца
    (orderSum × 0,58 / НДС, WB-6 §2), СС снимка по базовому артикулу × штуки, реклама номенклатуры (updSum дня × доля
    fullstats); buyout_* воронки — по дню заказа. Пустые строки (orderSum 0, заказов 0, рекламы 0) не отдаются — их в
    воронке большинство, и книга Ozon-сессии собиралась 34 мин вместо 6 (WB-8 §6); keep_empty=True возвращает всё.
    stats (dict) получает строк было / стало и Σ orderSum по месяцам до и после — они обязаны совпасть.

    WB-10 §3.2 (по слову, по умолчанию выключено): only_with_orders=True читает воронку фильтром на сервере (только
    карточки с заказами), а пары (день, nmId) с рекламой номенклатуры без строки воронки собирает из словаря products
    (nmId → vendor_code / title / brand / subject; у таких карточек buyout_* и cancel_* воронки = 0 — проверено на
    27 287 парах 04-01 … 09-24). products= работает и без фильтра: тогда добираются только пары без строки воронки в тот
    день (83 пары, 33,07 ₽ рекламы за 04-01 … 09-24), которые прежде терялись; stats["synthesized"] — сколько собрано."""
    sb = _sb(sb) if funnel_rows is None or costs is None else sb
    funnel_rows = load_funnel_products(date_from, date_to, sb, only_with_orders=only_with_orders) if funnel_rows is None else funnel_rows
    exact, uniform = wbm.load_costs(sb) if costs is None else costs
    if ads_by_day is None and sb is not None and ads_nm_by_day is None:
        try:
            ads_by_day, _u = wbm.load_ads_db(sb, date_from, date_to)
            ads_nm_by_day = load_ads_nm_db(sb, date_from, date_to)
        except RuntimeError as error:
            print(f"реклама для заказов не прочитана: {error}", flush=True); ads_by_day, ads_nm_by_day = {}, {}
    nm_ads = nm_ads_by_day_and_nm(ads_by_day or {}, ads_nm_by_day or {})
    synthesized = 0
    if products:
        seen = {(str(r["day"]), int(r["nm_id"])) for r in funnel_rows}
        extra = []
        for (day, nm), ads in sorted(nm_ads.items()):
            if ads and (day, nm) not in seen and nm in products and date_from <= day <= date_to:
                p = products[nm]
                extra.append({"day": day, "nm_id": nm, "vendor_code": p.get("vendor_code"), "title": p.get("title"), "brand": p.get("brand"), "subject_name": p.get("subject"),
                              "order_count": 0, "order_sum": 0, "buyout_count": 0, "buyout_sum": 0, "cancel_count": 0, "cancel_sum": 0})
        synthesized = len(extra)
        funnel_rows = sorted(list(funnel_rows) + extra, key=lambda r: (str(r["day"]), int(r["nm_id"])))
    out, before, after = [], defaultdict(Decimal), defaultdict(Decimal)
    n_before = 0
    for r in funnel_rows:
        qty = int(r.get("order_count") or 0)
        day = str(r["day"])
        order_sum = D(r.get("order_sum"))
        ads = nm_ads.get((day, int(r["nm_id"])), Z)
        n_before += 1; before[day[:7]] += order_sum
        if not keep_empty and qty == 0 and order_sum == 0 and ads == 0:
            continue
        cost, _src = wbm.unit_cost_for(exact, uniform, r.get("vendor_code"), None, "base")
        vat = wbm.vat_for(day)
        after[day[:7]] += order_sum
        out.append({"date": day, "month": month_label(day), "month_no": int(day[5:7]), "platform": PLATFORM, "shop": SHOP, "article": (str(r.get("vendor_code") or "").upper() or None),
                    "nm_id": int(r["nm_id"]), "title": r.get("title"), "brand": brand_of(r.get("vendor_code"), r.get("brand")),
                    "category": category_of(r.get("subject_name")), "orders_qty": qty, "orders_sum": order_sum, "ads": ads,
                    "revenue": order_sum * wbm.OWNER_ORDERS_AFTER_COMMISSION / vat, "cogs": (cost * qty) if cost is not None else None,
                    "no_cost": cost is None and qty > 0, "funnel_buyouts_qty": int(r.get("buyout_count") or 0), "funnel_buyouts_sum": D(r.get("buyout_sum")),
                    "funnel_cancel_qty": int(r.get("cancel_count") or 0), "funnel_cancel_sum": D(r.get("cancel_sum"))})
    if stats is not None:
        stats.update({"rows_before": n_before, "rows_after": len(out), "sum_before": dict(before), "sum_after": dict(after), "equal": dict(before) == dict(after),
                      "synthesized": synthesized, "ads_total": sum((r["ads"] for r in out), Z)})
    return out


# ---------- §3 (WB-9): коэффициент выкупа WB для листа «Коэффициенты» ----------

BUYOUT_RATE_MATURE_DAYS = 25   # когорта месяца заказа дособрана к 25-му дню после конца месяца: апрель … июль 2026 — 99,95 … 100,1 % ₽ итога (WB-9 §3)
RATE_SELECT = "rrd_id,rr_date,sale_dt,order_dt,seller_oper_name,retail_price_with_disc,quantity"
ANALYTICS_TABLE = "marketplace_orders_analytics"


def _msk_month(ts):
    """YYYY-MM момента в московском времени (orderDt / saleDt отчёта); пусто или не дата — None."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(wbm.MSK).date().isoformat()[:7]


def months_between(month_from, month_to):
    out, y, m = [], int(month_from[:4]), int(month_from[5:7])
    while f"{y}-{m:02d}" <= month_to:
        out.append(f"{y}-{m:02d}"); y, m = y + (m == 12), m % 12 + 1
    return out


def load_orders_by_month(sb, month_from, month_to):
    """{YYYY-MM: (Σ orderSum, Σ заказов)} из marketplace_orders_analytics — сводки дней воронки (= Σ wb_funnel_products_daily
    по месяцам: апрель … сентябрь 2026 до рубля, WB-9 §3); одна страница вместо 490 тыс. строк по товарам."""
    d1, d2 = month_bounds(month_from, month_to)
    rows = stale_keys.read_window_rows(sb, ANALYTICS_TABLE, "order_date,orders_amount,orders_qty",
                                       [("eq", "marketplace_code", "wb"), ("gte", "order_date", d1), ("lte", "order_date", d2)], ["order_date", "id"])
    out = defaultdict(lambda: [Z, 0])
    for r in rows:
        m = str(r["order_date"])[:7]
        out[m][0] += D(r.get("orders_amount")); out[m][1] += int(D(r.get("orders_qty")))
    return {m: (v[0], v[1]) for m, v in out.items()}


def buyout_rate_for_finrez(month_from, month_to, sb=None, rows=None, orders_by_month=None, today=None, by="rub", details=None):
    """Коэффициент выкупа WB для листа «Коэффициенты»: {ярлык месяца: Decimal, "итого": Decimal}.

    Числитель — продажи отчёта реализации (Продажа − Возврат по retailPriceWithDisc; by="qty" — штуки) по МЕСЯЦУ ЗАКАЗА
    (orderDt в МСК) — та же когорта, что и знаменатель: Σ orderSum воронки за месяц (marketplace_orders_analytics).
    Календарный вариант (продажи по saleDt / заказы месяца) смешивает когорты: 11 … 35 % продаж месяца — заказы прошлого
    месяца (WB-9 §3). Только зрелые месяцы: конец месяца + BUYOUT_RATE_MATURE_DAYS ≤ today; незрелые в ответ не входят
    (в details — с пометкой mature=False). «итого» — Σ числителей / Σ знаменателей по зрелым месяцам. Отчёт читается с 1-го
    числа month_from по today: продажа заказа проходит и через 40 дней после конца месяца. Только чтение."""
    if by not in ("rub", "qty"):
        raise ValueError("by: rub | qty")
    today = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
    sb = _sb(sb) if rows is None or orders_by_month is None else sb
    d1, _d2 = month_bounds(month_from, month_to)
    if rows is None:
        rows = read_keyset(sb, report_loader.TABLE, RATE_SELECT, d1, today.isoformat(), day_col="rr_date", id_col="rrd_id")
    orders_by_month = load_orders_by_month(sb, month_from, month_to) if orders_by_month is None else orders_by_month
    num = defaultdict(Decimal)
    for r in rows:
        op = r.get("seller_oper_name")
        if op not in ("Продажа", "Возврат"):
            continue
        om = _msk_month(r.get("order_dt"))
        if om is None:
            continue
        num[om] += (1 if op == "Продажа" else -1) * (D(r.get("retail_price_with_disc")) if by == "rub" else Decimal(int(r.get("quantity") or 1)))
    out, tn, td = {}, Z, Z
    for mo in months_between(month_from, month_to):
        y, mm = int(mo[:4]), int(mo[5:7])
        mature_from = date(y + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1) + timedelta(days=BUYOUT_RATE_MATURE_DAYS)
        mature = mature_from <= today
        den = D(orders_by_month.get(mo, (Z, 0))[0 if by == "rub" else 1])
        rate = ratio(num.get(mo, Z), den) if den else None
        if details is not None:
            details[mo] = {"numerator": num.get(mo, Z), "denominator": den, "rate": rate, "mature": mature, "mature_from": mature_from.isoformat()}
        if mature and den:
            out[month_label(f"{mo}-01")] = rate; tn += num.get(mo, Z); td += den
    if td:
        out["итого"] = ratio(tn, td)
    return out


# ---------- приёмка против «WB - месяц» ----------

def bridge_to_month_sheet(rows, report_rows, d1, d2, costs, ads_by_day):
    """Мост между итогами формы и итогом «WB - месяц» (report_wb_month.build_daily по тем же строкам отчёта) за d1 … d2."""
    days = []
    d = date.fromisoformat(d1)
    while d <= date.fromisoformat(d2):
        days.append(d.isoformat()); d += timedelta(days=1)
    exact, uniform = costs
    cost_fn = lambda code, size: wbm.unit_cost_for(exact, uniform, code, size, "base")  # noqa: E731
    daily = wbm.build_daily(report_rows, days, cost_fn, ads_by_day or {}, date.today() + timedelta(days=3), ads_by_day is not None)
    m = wbm.total_row(daily)
    form = build_month_sheet(rows)[-1]
    vat = form["vat"]
    sums = _sum_rows(rows)
    ppvz = sum((D(r.get("ppvz_reward")) for r in rows), Z)
    rebill = sum((D(r.get("rebill")) for r in rows), Z)
    penalty_vat_part = m["penalty"] - m["penalty"] / vat       # владелец: штрафы без НДС; форма: H / НДС
    add_pay = sums["other"] - (m["penalty"] + m["deduction"])  # other = penalty + deduction − additionalPayment → остаток = −additionalPayment
    lines = [
        ("Оборот", form["k_turnover"], m["turnover"], []),
        ("Комиссия (Σ цена × кВВ)", form["l_commission"], m["commission"], []),
        ("Себестоимость (снимок, базовый артикул)", form["o_cogs"], m["cogs"], []),
        ("Эквайринг без НДС", form["v_acquiring"], m["acquiring"], []),
        ("Выручка без НДС", form["n_revenue"], m["revenue"], [("НДС за возмещение вычитается только у владельца", m["vat_refund"])]),
        ("Логистика без НДС", form["r_logistics"], m["logistics"], [("хранение / НДС внутри R у второго кабинета", m["storage"] / vat)]),
        ("Реклама без НДС", form["t_ads"] if form["t_ads"] is not None else Z, m["ads"] if m["ads"] is not None else Z, []),
        ("Прочее без НДС", form["x_other"], m["other"], [("хранение / НДС — у владельца в прочем, здесь в логистике", -(m["storage"] / vat)), ("штрафы: у владельца без НДС, здесь H / НДС", -penalty_vat_part), ("additionalPayment / НДС вычтен из H", add_pay / vat)]),
        ("Фин. рез.", form["y_fin"], m["fin_result"], [("НДС за возмещение", m["vat_refund"]), ("штрафы без НДС у владельца", penalty_vat_part), ("additionalPayment / НДС", -(add_pay / vat))]),
    ]
    out = []
    for title, ours, theirs, terms in lines:
        explained = sum((t for _n, t in terms), Z)
        out.append({"title": title, "form": q(ours), "month": q(theirs), "diff": q(ours - theirs), "terms": [(n, q(t)) for n, t in terms],
                    "rest": q(ours - theirs - explained)})
    return out, m, (ppvz, rebill)


def print_bridge(table, d1, d2):
    print(f"\nМост «Выкупы WB» (форма второго кабинета) ↔ «WB - месяц» за {d1} … {d2}")
    print(f"{'колонка':44}{'форма':>16}{'WB - месяц':>16}{'разница':>14}{'остаток':>12}")
    failures = 0
    for t in table:
        print(f"{t['title'][:43]:44}{t['form']:>16,.2f}{t['month']:>16,.2f}{t['diff']:>14,.2f}{t['rest']:>12,.2f}" + ("   ОСТАТОК ≠ 0" if t["rest"] != 0 else ""))
        for name, value in t["terms"]:
            print(f"      {value:>+14,.2f}  {name}")
        if t["rest"] != 0:
            failures += 1
    print(f"остаток моста ≠ 0 в строках: {failures}")
    return failures


# ---------- книга ----------

def write_xlsx(path, data_rows, month_rows, split_brand, split_category, notes):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fmt = {"money": "#,##0.00", "pct": "0.0%", "int": "#,##0", "date": "DD.MM.YYYY"}
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="D9E1F2")
    wb = openpyxl.Workbook()

    def sheet(title, cols, rows, footer=(), title_line=None):
        ws = wb.create_sheet(title)
        if title_line:
            ws["A1"] = title_line; ws["A1"].font = bold
        for j, (head, _k, _f) in enumerate(cols, 1):
            if head:
                c = ws.cell(row=3, column=j, value=head); c.font = bold; c.fill = head_fill
                c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        for i, r in enumerate(rows, 4):
            for j, (head, k, f) in enumerate(cols, 1):
                if not head:
                    continue
                v = r.get(k)
                if f == "date" and isinstance(v, str):
                    v = date.fromisoformat(v)
                elif isinstance(v, Decimal):
                    v = float(v)
                elif isinstance(v, bool):
                    v = "да" if v else ""
                c = ws.cell(row=i, column=j, value=v)
                if f:
                    c.number_format = fmt[f]
                if r.get("label") == "Итого":
                    c.font = bold
        for n, line in enumerate(footer):
            ws.cell(row=4 + len(rows) + 2 + n, column=1, value=line)
        ws.freeze_panes = "B4"; ws.row_dimensions[3].height = 48
        for j in range(1, len(cols) + 1):
            ws.column_dimensions[get_column_letter(j)].width = 14

    wb.remove(wb.active)
    sheet("Выкупы WB", SHEET_COLS, month_rows, notes, "Данные с НДС — форма второго кабинета; K … Z по его тождествам (R включает хранение)")
    sheet("Выкупы WB по брендам", SPLIT_COLS, split_brand, (), "Бренд × месяц")
    sheet("Выкупы WB по категориям", SPLIT_COLS, split_category, (), "Категория × месяц")
    sheet("Данные WB выкупы", DATA_COLS, data_rows, (), "Строка на (дата продажи saleDt МСК, nmId); «(без товара)» — операции без nmId")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--month-from", required=True); ap.add_argument("--month-to", required=True)
    ap.add_argument("--date-to", help="последний день (внутри month-to); по умолчанию — конец месяца")
    ap.add_argument("--xlsx", help="куда писать книгу")
    ap.add_argument("--check", action="store_true", help="мост к «WB - месяц» за тот же период")
    ap.add_argument("--buyout-rate", action="store_true", help="WB-9 §3: коэффициент выкупа по месяцам (когорта месяца заказа), ₽ и штуки")
    args = ap.parse_args(argv)
    sb = report_loader._client()
    d1, d2 = month_bounds(args.month_from, args.month_to, args.date_to)
    report_rows = load_report_rows(sb, d1, d2)
    costs = wbm.load_costs(sb)
    try:
        ads_by_day, _u = wbm.load_ads_db(sb, d1, d2)
    except RuntimeError as error:
        print(f"реклама не прочитана: {error}; колонка «Реклама» пуста"); ads_by_day = None
    products = load_product_dictionary(sb, d1, d2)
    ads_nm_by_day = load_ads_nm_db(sb, d1, d2) if ads_by_day is not None else {}
    bstats = {}
    rows = build_rows(args.month_from, args.month_to, args.date_to, sb, rows=report_rows, costs=costs, ads_by_day=ads_by_day, products=products, ads_nm_by_day=ads_nm_by_day, stats=bstats)
    from_row = sum(1 for r in report_rows if r.get("subject_name"))
    print(f"предмет в строках отчёта: {from_row} из {len(report_rows)}; дней со статистикой по номенклатурам: {len(ads_nm_by_day)}; "
          f"реклама разнесена (строк «Данных»): " + (", ".join(f"{k} — {v}" for k, v in sorted(__import__('collections').Counter(r['ads_allocated'] for r in rows if r['ads_allocated']).items())) or "нет списаний"))
    month_rows = build_month_sheet(rows)
    with_product = [r for r in rows if r["nm_id"] is not None]
    cats = defaultdict(int)
    for r in with_product:
        cats[r["category"]] += 1
    no_cat = [r for r in with_product if r["category"] == CATEGORY_UNKNOWN]
    print(f"категория есть у {len(with_product) - len(no_cat)} из {len(with_product)} строк «Данных» с товаром ({(1 - len(no_cat) / len(with_product)) if with_product else 1:.2%})"
          + (f"; без предмета: " + ", ".join(f"{r['date']} nmId {r['nm_id']} {r['article']}" for r in no_cat[:20]) if no_cat else ""))
    print(f"строк отчёта {len(report_rows)}; «Данные» {len(rows)} строк: с товаром {len(with_product)} (ключей день × nmId), «{NO_PRODUCT}» {len(rows) - len(with_product)}; "
          f"словарь товаров {len(products)} nmId; категории: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1]))
          + f"; без СС {sum(r['no_cost_qty'] for r in rows)} шт; реклама {'разнесена по продажам дня' if ads_by_day is not None else 'не прочитана'}")
    for r in month_rows:
        ads_text = f"{r['t_ads']:,.2f}" if r["t_ads"] is not None else "—"
        print(f"{r['label']:6} продажи {r['sales']:,.2f}, комиссия {r['commission']:,.2f} ({r['m_commission_pct'] or 0:.2%}), выручка {r['n_revenue']:,.2f}, СС {r['o_cogs']:,.2f}, "
              f"маржа {r['p_margin']:,.2f}, логистика+хранение {r['r_logistics']:,.2f}, реклама {ads_text}, эквайринг {r['v_acquiring']:,.2f}, прочее {r['x_other']:,.2f}, фин. рез. {r['y_fin']:,.2f}")
    split_brand, split_category = build_split(rows, "brand"), build_split(rows, "category")
    tot = month_rows[-1]
    sb_tot = sum((r["sales"] for r in split_brand if r["label"] == "Итого"), Z); sc_tot = sum((r["sales"] for r in split_category if r["label"] == "Итого"), Z)
    print(f"Σ брендов {sb_tot:,.2f} = Σ категорий {sc_tot:,.2f} = общий {tot['sales']:,.2f}: {'да' if sb_tot == sc_tot == tot['sales'] else 'НЕТ'}")
    by_month_total = {r["label"]: r["sales"] for r in month_rows if r["label"] != "Итого"}
    cat_by_month = defaultdict(Decimal)
    for r in split_category:
        if r["label"] != "Итого":
            cat_by_month[r["label"]] += r["sales"]
    cat_ok = set(cat_by_month) == set(by_month_total) and all(cat_by_month[m] == v for m, v in by_month_total.items())
    print(f"Σ категорий по месяцам = общему по месяцам: {'да' if cat_ok else 'НЕТ'} — " + ", ".join(f"{m} {v:,.2f}" for m, v in by_month_total.items()))
    labels_b, labels_c = set(bstats.get("brands", {})), set(bstats.get("categories", {}))
    labels_ok = labels_b <= BRAND_LABELS and labels_c <= CATEGORY_LABELS
    print(f"ярлыки (WB-9 §2): бренды {sorted(labels_b)} ⊆ владельца — {'да' if labels_b <= BRAND_LABELS else 'НЕТ'}; категории {sorted(labels_c)} ⊆ владельца — "
          f"{'да' if labels_c <= CATEGORY_LABELS else 'НЕТ'}; строк «{CATEGORY_UNKNOWN}» {bstats.get('rows_unknown_category', 0)}; «{UNIDENTIFIED}»: строк отчёта {bstats.get('unidentified', {}).get('rows', 0)}")
    code = 0 if (cat_ok and labels_ok) else 1
    if args.buyout_rate:
        today = date.today().isoformat()
        rate_rows = read_keyset(sb, report_loader.TABLE, RATE_SELECT, f"{args.month_from}-01", today, day_col="rr_date", id_col="rrd_id")
        obm = load_orders_by_month(sb, args.month_from, args.month_to)
        for by in ("rub", "qty"):
            det = {}
            res = buyout_rate_for_finrez(args.month_from, args.month_to, sb, rows=rate_rows, orders_by_month=obm, today=today, by=by, details=det)
            print(f"коэффициент выкупа WB ({'₽' if by == 'rub' else 'шт'}; когорта месяца заказа, зрелость {BUYOUT_RATE_MATURE_DAYS} дн. после конца месяца, на {today}): "
                  + (", ".join(f"{k} {v}" for k, v in res.items()) or "зрелых месяцев нет"))
            for m, d in det.items():
                print(f"  {m}: {d['numerator']:,.2f} / {d['denominator']:,.2f} = {d['rate']}" + ("" if d["mature"] else f" — незрелый, зрел с {d['mature_from']}"))
        print(f"строк отчёта для коэффициента {len(rate_rows)} (rr_date {args.month_from}-01 … {today}), месяцев заказов {len(obm)}")
    if args.check:
        table, _m, (ppvz, rebill) = bridge_to_month_sheet(rows, report_rows, d1, d2, costs, ads_by_day)
        failures = print_bridge(table, d1, d2)
        print(f"справочно: возмещение ПВЗ (ppvzReward) {ppvz:,.2f} и возмещение издержек по перевозке (rebillLogisticCost) {rebill:,.2f} с НДС за период — в форму не входят")
        code = 1 if failures else 0
    if args.xlsx:
        notes = [f"Источник: отчёт реализации WB (wb_sales_report_rows), день — saleDt МСК; период {d1} … {d2}; строк «Данные» {len(rows)}.",
                 "Форма — книга второго кабинета (лист «Свод»): N = (K − L)/НДС, R = (Логистика + Хранение)/НДС, T = E/НДС, X = H/НДС, Y = P − R − T − V − X, доли от N.",
                 "Отличие от образца: эквайринг V заполнен (у второго кабинета 0). Логистика ₽ = deliveryService (возмещение издержек по перевозке — не берём, справочно в «Данных»); Ост. расходы = penalty + deduction − additionalPayment.",
                 "Реклама — списания дня (wb_ad_spend_daily, биллинг, с НДС), разнесённые по артикулам долями из статистики по номенклатурам (fullstats, нормировка к списаниям дня; Σ по дню = списаниям), где статистики нет — пропорционально продажам дня.",
                 f"Себестоимость — снимок 1С {wbm.SNAP} по базовому артикулу. Ярлыки одни с Ozon: категория — предмет строки отчёта / карточки по словарю владельца "
                 "(строчными), прочее — вслух; бренд — по первой букве артикула (t → Топаз, иначе KARATOV); «Неопознанный товар» — как строки без товара."]
        write_xlsx(args.xlsx, rows, month_rows, split_brand, split_category, notes)
        print(f"записано: {args.xlsx}")
    print("db_writes = 0")
    return code


if __name__ == "__main__":
    sys.exit(main())
