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
    classify_deduction(bonus_type_name)                       -> "ads_withheld" | "review_points_advance" | "review_points_refund" | "other" (WB-14 §2;
                                                                 правила денег — loaders/wb_money_rules: «Прочее», реклама «Баланс», упаковка)
    coinvest_share_by_month(month_from, month_to, sb=None)    -> {ярлык месяца: Decimal, "итого": Decimal} — доля соинвеста WB по месяцу продажи
                                                                 (P владельца: (Σ price − Σ amount) / Σ price по продажам и возвратам со знаком; WB-11 §2)
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
    Хранение = paidStorage; Ост. расходы и компенсации = penalty + удержания вида «прочее» − additionalPayment (WB-14: без удержаний
    «WB Продвижение» и без аванса «Баллы за отзывы», нетто аванса — в день возврата); Эквайринг = acquiringFee
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
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента

from loaders import keyset, stale_keys  # noqa: E402
from loaders import wb_money_rules as rules  # noqa: E402  — удержания по виду, реклама «Баланс», упаковка (WB-14)
import loaders.wb_sales_report_loader as report_loader  # noqa: E402
import report_wb_month as wbm  # noqa: E402  — те же строки отчёта, СС, реклама и НДС, что у «WB - месяц»

Z = Decimal(0)
C2 = Decimal("0.01")
PLATFORM = "WB"
SHOP = CABINET.SHOP                    # колонка «Магазин» книги — из профиля кабинета (вопрос о SHOP снят владельцем 09-30)
DISCOUNTER_LETTER = (CABINET.DISCOUNTER_LETTER or "").lower() or None   # буква артикула «Дискаунтер»; None — площадки не делятся
# Ярлыки бренда и категории — одни на обе площадки, как у Ozon-скрипта (WB-9 §2), из профиля кабинета: бренд по первой букве
# артикула (BRAND_BY_LETTER, иначе BRAND_DEFAULT; бренд WB — только когда артикула нет, и тогда нормализуется), категория —
# словарь владельца строчными (OWNER_CATEGORIES те же, что у ozon_product_catalog, — оба из профиля, тест на равенство).
BRAND_BY_LETTER = {str(k).lower(): v for k, v in CABINET.BRAND_BY_LETTER.items()}
BRAND_DEFAULT = CABINET.BRAND_DEFAULT
BRAND_NORMALIZE = {str(k).lower(): v for k, v in CABINET.BRAND_NORMALIZE.items()}
UNIDENTIFIED = "неопознанный товар"    # артикул и бренд WB у операций без опознанного товара — считаем строками без товара
NO_PRODUCT = "(без товара)"
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
OWNER_CATEGORIES = tuple(CABINET.OWNER_CATEGORIES)
# Предмет WB → категория владельца; всё остальное — «прочее», считается вслух (в данных на 09-25: иконы, упаковки, запонки).
CATEGORY_BY_SUBJECT = dict(CABINET.CATEGORY_BY_SUBJECT)
CATEGORY_OTHER = "прочее"
CATEGORY_UNKNOWN = "прочее (нет предмета)"
BRAND_LABELS = frozenset({BRAND_DEFAULT, NO_PRODUCT} | set(BRAND_BY_LETTER.values()))
CATEGORY_LABELS = frozenset(OWNER_CATEGORIES) | {CATEGORY_OTHER, CATEGORY_UNKNOWN, NO_PRODUCT}
SELECT = wbm.SELECT + ",additional_payment,brand_name"   # в wbm.SELECT с WB-14 уже subject_name и bonus_type_name   # поля «WB - месяц» (мост зовёт его build_daily) + доплаты, предмет, бренд
# build_rows читает только то, что использует: без doc_type, for_pay, cashback_discount (они нужны мосту через build_daily — main читает SELECT). WB-9 §4.
BUILD_SELECT = ("rrd_id,rr_date,sale_dt,seller_oper_name,vendor_code,tech_size,nm_id,quantity,retail_price_with_disc,retail_amount,commission_percent,"
                "ppvz_reward,rebill_logistic_cost,delivery_service,acquiring_fee,paid_storage,penalty,deduction,additional_payment,bonus_type_name,subject_name,brand_name")
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
    ("справочно: реклама за кэшбэк WB, ₽ (в форму не входит)", "ads_cashback", "money"),   # WB-14 §3: реклама за кэшбэк — не расход
    ("СС источник", "cost_source", None),   # WB-17 §2: снимок 1С по дате продажи и ступень ключа (как у Ozon)
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
MONEY_KEYS = ("sales", "ads", "commission", "logistics", "cogs", "storage", "other", "acquiring", "coinvest", "ppvz_reward", "rebill", "ads_cashback")
classify_deduction = rules.classify_deduction   # договор модуля (WB-14 §2): вид удержания по bonus_type_name


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
    """Ярлык бренда владельца: первая буква артикула первична (BRAND_BY_LETTER профиля, иначе BRAND_DEFAULT); бренд WB (строка
    отчёта / карточка) — только когда артикула нет, и тогда нормализуется (BRAND_NORMALIZE), неизвестный бренд без
    артикула — BRAND_DEFAULT; «Неопознанный товар» — «(без товара)»."""
    code = str(vendor_code or "").strip()
    if is_unidentified(code, funnel_brand):
        return NO_PRODUCT
    if code:
        return BRAND_BY_LETTER.get(code[:1].lower(), BRAND_DEFAULT)
    return BRAND_NORMALIZE.get(str(funnel_brand or "").strip().lower(), BRAND_DEFAULT)


def brand_rule_text():
    """Подпись правила бренда для примечаний: «t → Топаз, иначе KARATOV» / «правила по букве артикула не заданы, бренд …»."""
    if not BRAND_BY_LETTER:
        return f"правила по букве артикула не заданы, бренд {BRAND_DEFAULT}"
    return ", ".join(f"{k} → {v}" for k, v in sorted(BRAND_BY_LETTER.items())) + f", иначе {BRAND_DEFAULT}"


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


FUNNEL_CURRENT_TABLE = "wb_funnel_products_current"   # sql/20260928_create_wb_funnel_products_current.sql — по слову; пишет шаг воронки в конце окна (WB-12 §3)
FUNNEL_LATEST_VIEW = "wb_funnel_products_latest"   # sql/20260926_create_wb_funnel_products_latest.sql — по слову; нет вьюхи → чтение таблицы окном


def _missing_relation(error):
    """PostgREST про несуществующую таблицу/вьюху: PGRST205 (нет в кэше схемы) или 42P01 (relation does not exist)."""
    text = str(error)
    return "PGRST205" in text or "42P01" in text or "does not exist" in text


def _read_cards_by_nm(sb, relation, page=None):
    """Строки relation (таблица или вьюха «последняя карточка по nmId») страницами по ключу nm_id. Нет отношения — None."""
    page = page or DICT_PAGE
    out, last = [], None
    while True:
        qb = sb.table(relation).select("nm_id,day,vendor_code,title,brand,subject_name").order("nm_id").limit(page)
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


def read_latest_cards(sb, page=None, source=None):
    """Последняя карточка по nmId (одна строка на nmId): сначала таблица wb_funnel_products_current (WB-12 §3 — пишет шаг
    воронки, чтение по первичному ключу, от кэша не зависит); таблицы нет или она пуста — вьюха wb_funnel_products_latest
    (холодная упирается в statement_timeout 8 с — WB-12 §3); нет и вьюхи — None. source (dict) получает relation и why."""
    rows = _read_cards_by_nm(sb, FUNNEL_CURRENT_TABLE, page)
    if rows:
        if source is not None:
            source.update({"relation": FUNNEL_CURRENT_TABLE, "why": None})
        return rows
    why = f"{FUNNEL_CURRENT_TABLE}: " + ("таблицы нет (миграция по слову)" if rows is None else "таблица пуста")
    rows = _read_cards_by_nm(sb, FUNNEL_LATEST_VIEW, page)
    if source is not None:
        source.update({"relation": FUNNEL_LATEST_VIEW if rows is not None else None, "why": why})
    return rows


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
        why, src = None, {}
        try:
            rows = read_latest_cards(sb, source=src)
        except Exception as error:
            rows, why = None, f"не прочитана — {str(error)[:160]}"
        if rows is not None:
            for r in rows:
                out[int(r["nm_id"])] = {"title": r.get("title"), "brand": r.get("brand"), "subject": r.get("subject_name"), "vendor_code": r.get("vendor_code")}
            print(f"словарь товаров: {src.get('relation')} — строк {len(rows)}, nmId {len(out)}, "
                  f"{(datetime.now(timezone.utc) - started).total_seconds():.1f} с, страниц по {DICT_PAGE}: {max(1, (len(rows) + DICT_PAGE - 1) // DICT_PAGE)}"
                  + (f" ({src['why']})" if src.get("why") else ""), flush=True)
            return out
        print(f"словарь товаров: {FUNNEL_CURRENT_TABLE} и {FUNNEL_LATEST_VIEW} {why or (src.get('why') or '') + '; вьюхи нет (миграция по слову)'} — читаю {wbm.FUNNEL_TABLE} за {d1} … {d2} окном", flush=True)
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


def load_ads_nm_rows(sb, d1, d2):
    """Строки wb_ad_spend_nm_daily (fullstats, с НДС, sum > 0) за окно: день, кампания, nmId, сумма. Таблицы нет — [] вслух."""
    try:   # sum > 0: строки с нулём в доли не входят, отрицательных в таблице нет (min 0, WB-9 §4)
        return stale_keys.read_window_rows(sb, ADS_NM_TABLE, "day,advert_id,nm_id,sum", [("gte", "day", d1), ("lte", "day", d2), ("gt", "sum", 0)],
                                           ["day", "advert_id", "app_type", "nm_id"])
    except Exception as error:
        print(f"статистика по номенклатурам не прочитана ({ADS_NM_TABLE}): {str(error)[:120]} — реклама разносится по продажам дня", flush=True)
        return []


def nm_weights(nm_rows, advert_day=None, which="balance"):
    """{день: {nmId: вес}} — Σ sum fullstats по кампаниям × доля вида оплаты кампании в этот день (advert_day
    {(день, advert_id): (Баланс, всего)} из report_wb_month.load_ads_split). which: balance | other | all. Кампания без строки
    списаний в день считается «Балансом» (как было до WB-14); advert_day нет — веса без долей (all)."""
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in nm_rows or ():
        value = D(r.get("sum"))
        if value <= 0:
            continue
        day = str(r["day"])
        if which != "all" and advert_day is not None:
            adv = int(r["advert_id"]) if r.get("advert_id") not in (None, "") else None
            balance, total = advert_day.get((day, adv), (None, None))
            if total:
                frac = (balance / total) if which == "balance" else ((total - balance) / total)
            else:
                frac = Decimal(1) if which == "balance" else Z
            value = value * frac
        if value > 0:
            out[day][int(r["nm_id"])] += value
    return {d: dict(v) for d, v in out.items()}


def load_ads_nm_db(sb, d1, d2, advert_day=None, which="balance"):
    """{день: {nmId: Σ sum}} из wb_ad_spend_nm_daily — доли для разнесения списаний дня; с advert_day — по виду оплаты (WB-14)."""
    return nm_weights(load_ads_nm_rows(sb, d1, d2), advert_day, which if advert_day is not None else "all")


# ---------- «Данные WB выкупы» ----------

def costs_for_codes(sb, vendor_codes, what="строк"):
    """Все снимки 1С адресно по кодам строк (WB-17 §2: wbm.load_cost_history_for) с печатью объёма чтения → WbCostHistory."""
    started = datetime.now(timezone.utc)
    cstats = {}
    hist = wbm.load_cost_history_for(sb, vendor_codes, stats=cstats)
    how = "адресно" if cstats.get("mode") == "narrow" else f"целиком (баз больше {wbm.COST_NARROW_MAX_BASES} — так дешевле)"
    print(f"себестоимость: снимки 1С {', '.join(cstats['snapshots']) or 'нет'} {how} по кодам {what} — баз {cstats['bases']}, строк {cstats['rows']} "
          f"за {cstats['requests']} обращений, {(datetime.now(timezone.utc) - started).total_seconds():.1f} с; СС по дате продажи, ключ WB → Ozon-ключ строки", flush=True)
    return hist


def packaging_nm_ids(rows, products=None):
    """nmId упаковки (WB-14 §4): предмет «Упаковки для украшений» в строках отчёта или в словаре карточек."""
    out = set()
    for r in rows or ():
        nm = r.get("nm_id")
        if nm not in (None, "", 0, "0") and rules.is_packaging(r.get("subject_name")):
            out.add(int(nm))
    for nm, p in (products or {}).items():
        if rules.is_packaging((p or {}).get("subject")):
            out.add(int(nm))
    return out


def _is_packaging_row(r, packaging_nm):
    nm = r.get("nm_id")
    return rules.is_packaging(r.get("subject_name")) or (nm not in (None, "", 0, "0") and int(nm) in packaging_nm)


def _new_acc():
    a = {k: Z for k in MONEY_KEYS}
    a.update({"qty": 0, "no_cost_qty": 0, "vendor": "", "subject": None, "brand": None, "cost_sources": set()})
    return a


def _add_report_row(a, r, hist, uniform=None):
    """Деньги строки отчёта в накопитель (день × nmId): продажи / возвраты со знаком, логистика, хранение, «прочее» по
    правилам WB-14 (rules.other_amount), возмещение ПВЗ и rebill справочно."""
    op = r["seller_oper_name"]
    sign = 1 if op == "Продажа" else (-1 if op == "Возврат" else 0)
    if sign:
        price, amount, qty = D(r["retail_price_with_disc"]), D(r["retail_amount"]), int(r.get("quantity") or 1)
        a["sales"] += sign * price
        a["commission"] += sign * price * D(r.get("commission_percent")) / 100
        a["coinvest"] += sign * (price - amount)
        a["acquiring"] += sign * D(r.get("acquiring_fee"))
        a["qty"] += sign * qty
        hist = wbm.as_cost_history(hist if uniform is None else (hist, uniform))
        cost, source, _step = hist.cost_source(r.get("vendor_code"), r.get("tech_size"), wbm.row_day(r), qty)   # WB-17 §2: снимок по дате продажи
        if cost is None:
            a["no_cost_qty"] += qty
        else:
            a["cogs"] += sign * cost * qty
            a["cost_sources"].add(source)
    a["logistics"] += D(r.get("delivery_service"))            # rebillLogisticCost — не берём (инструкция владельца), справочно ниже
    a["rebill"] += D(r.get("rebill_logistic_cost"))
    a["storage"] += D(r.get("paid_storage"))
    a["other"] += rules.other_amount(r)                        # WB-14: штрафы + удержания вида «прочее» − доплаты
    a["ppvz_reward"] += D(r.get("ppvz_reward"))


def build_rows(month_from, month_to, date_to=None, sb=None, rows=None, costs=None, ads_by_day=None, products=None, ads_nm_by_day=None, stats=None,
               ads_cashback_by_day=None, cashback_nm_by_day=None, advance_rows=None):
    """Строки «Данные WB выкупы» за месяцы month_from … month_to (до date_to включительно, если задано).

    rows / costs / ads_by_day / products — для тестов и повторных сборок; по умолчанию читаются из базы.
    stats (dict) получает счётчики ярлыков (categories, brands, rows_unknown_category), строки «Неопознанный товар» (unidentified)
    и stats["excluded"] — что исключено правилами WB-14 и сколько (для моста к отчёту реализации).

    WB-14 (loaders/wb_money_rules, решения владельца 28.09): «Ост. расходы» H = штрафы + удержания вида «прочее» − доплаты —
    удержания «Оказание услуг «WB Продвижение»» (реклама с баланса, её деньги уже в «Рекламе» из upd) не входят; аванс «Баллы
    за отзывы» и его возврат — только нетто в день возврата, строкой «(без товара)»; «Реклама» — только оплата «Баланс», реклама
    за кэшбэк WB — справочно в ads_cashback (доли номенклатур — fullstats × доля оплаты кампании в день); строки упаковки
    (предмет «Упаковки для украшений») в «Данные» не входят. ads_cashback_by_day / cashback_nm_by_day / advance_rows — для тестов."""
    sb = _sb(sb) if rows is None or costs is None or ads_by_day is None or products is None else sb
    d1, d2 = month_bounds(month_from, month_to, date_to)
    rows = load_report_rows(sb, d1, d2, select=BUILD_SELECT) if rows is None else rows
    products = load_product_dictionary(sb, d1, d2) if products is None else products
    packaging_nm = packaging_nm_ids(rows, products)
    if costs is None:
        costs = costs_for_codes(sb, (r.get("vendor_code") for r in rows if not _is_packaging_row(r, packaging_nm)), "строк отчёта")
    hist = wbm.as_cost_history(costs)   # WB-17 §2: история снимков; пара карт (тесты) — один снимок
    exact = uniform = None   # прежние карты одного снимка больше не читаются
    advert_day = None
    if ads_by_day is None:
        try:
            split = wbm.load_ads_split(sb, d1, d2)
            ads_by_day, advert_day = split["balance"], split["advert_day"]
            ads_cashback_by_day = split["other"] if ads_cashback_by_day is None else ads_cashback_by_day
        except RuntimeError as error:
            print(f"реклама не прочитана: {error}; колонка «Реклама» пуста", flush=True)
            ads_by_day = None
    ads_cashback_by_day = ads_cashback_by_day or {}
    if ads_nm_by_day is None and ads_by_day is not None:
        nm_rows = load_ads_nm_rows(sb, d1, d2)
        ads_nm_by_day = nm_weights(nm_rows, advert_day, "balance")
        if cashback_nm_by_day is None and ads_cashback_by_day:
            cashback_nm_by_day = nm_weights(nm_rows, advert_day, "other")
    ads_nm_by_day = ads_nm_by_day or {}
    cashback_nm_by_day = cashback_nm_by_day if cashback_nm_by_day is not None else ads_nm_by_day
    nm_subject = {}                                       # nmId → (предмет, бренд) из ЛЮБОЙ загруженной строки отчёта (окно + запас)
    for r in rows:
        nm = r.get("nm_id")
        if nm not in (None, "", 0, "0") and r.get("subject_name") and int(nm) not in nm_subject:
            nm_subject[int(nm)] = (r["subject_name"], r.get("brand_name"))
    unidentified = {"rows": 0, "days": set(), "sales": Z, "logistics": Z, "rebill": Z, "storage": Z, "other": Z}   # «Неопознанный товар» → «(без товара)»
    tally = rules.DeductionTally()
    pack = _new_acc(); pack.update({"rows": 0, "nm_ids": set()})
    acc = {}
    for r in rows:
        day = wbm.row_day(r)
        if not (d1 <= day <= d2):
            continue
        tally.add(r)
        if _is_packaging_row(r, packaging_nm):               # WB-14 §4: упаковка — не товар, в «Данные» не входит
            _add_report_row(pack, r, hist)
            pack["rows"] += 1
            if r.get("nm_id") not in (None, "", 0, "0"):
                pack["nm_ids"].add(int(r["nm_id"]))
            continue
        nm = r.get("nm_id")
        nm = int(nm) if nm not in (None, "", 0, "0") else None
        if nm is not None and is_unidentified(r.get("vendor_code"), r.get("brand_name")):   # WB-9 §2: как строки без товара, вслух
            u = unidentified
            u["rows"] += 1; u["days"].add(day)
            u["sales"] += (1 if r["seller_oper_name"] == "Продажа" else -1) * D(r.get("retail_price_with_disc")) if r["seller_oper_name"] in ("Продажа", "Возврат") else Z
            u["logistics"] += D(r.get("delivery_service")); u["rebill"] += D(r.get("rebill_logistic_cost")); u["storage"] += D(r.get("paid_storage"))
            u["other"] += rules.other_amount(r)
            nm = None
        key = (day, nm)
        a = acc.get(key)
        if a is None:
            a = acc[key] = _new_acc()
            a["vendor"] = str(r.get("vendor_code") or "")
        if not a["subject"] and r.get("subject_name"):
            a["subject"] = r["subject_name"]
        if not a["brand"] and r.get("brand_name"):
            a["brand"] = r["brand_name"]
        _add_report_row(a, r, hist)
    # аванс «Баллы за отзывы»: нетто — в день возврата, строкой «(без товара)» (аванс и возврат сами в «прочее» не идут).
    # WB-16 §1: строки аванса/возврата — из тех же rows (отдельного чтения всей истории нет); аванс раньше окна — дочитка по
    # индексу rr_date (advance_net_by_day); advance_rows — для тестов
    ainfo = {}
    if advance_rows is None:
        advance_rows = rules.review_points_rows(rows)
        lookback_sb = sb if hasattr(sb, "table") else None
    else:
        lookback_sb = None
    try:
        advance_net, advance_open = wbm.advance_net_by_day(lookback_sb, advance_rows, d1, info=ainfo)
    except Exception as error:  # noqa: BLE001 — упала только дочитка аванса раньше окна; нетто без неё, сказано вслух
        print(f"аванс «Баллы за отзывы» не прочитан (дочитка раньше окна): {str(error)[:160]} — нетто аванса в «Прочее» не добавлено", flush=True)
        advance_net, advance_open = wbm.advance_net_by_day(None, advance_rows, d1, info=ainfo)
    advance_in_window = {d: v for d, v in advance_net.items() if d1 <= d <= d2}
    for day, v in advance_in_window.items():
        a = acc.get((day, None))
        if a is None:
            a = acc[(day, None)] = _new_acc()
        a["other"] += v
    # реклама дня — доли номенклатур из fullstats (нормированы к списаниям дня), без статистики — по продажам, без продаж — «(без товара)»
    by_day = defaultdict(list)
    for key in acc:
        by_day[key[0]].append(key)
    for totals in (ads_by_day or {}, ads_cashback_by_day):   # день со списаниями без единой строки отчёта — тоже день: реклама не теряется
        for day, value in totals.items():
            if d1 <= day <= d2 and day not in by_day and value:
                by_day[day] = []
    ads_source = {}

    def allocate(field, totals, nm_by_day):
        for day, keys in sorted(by_day.items()):
            amount = totals.get(day, Z)
            if not amount:
                continue
            nm_shares = {nm_id: v for nm_id, v in (nm_by_day.get(day) or {}).items() if v > 0 and nm_id not in packaging_nm}
            if nm_shares:                                     # доли из fullstats, нормированные к списаниям дня (истина по деньгам — updSum)
                weights, source = {(day, nm_id): v for nm_id, v in nm_shares.items()}, "fullstats"
            else:
                weights, source = {k: acc[k]["sales"] for k in keys if k[1] is not None and acc[k]["sales"] > 0}, "по продажам"
            total = sum(weights.values(), Z)
            if total:
                spent = Z
                ordered = sorted(weights, key=lambda k: k[1])
                for k in ordered:
                    if k not in acc:                          # реклама у товара без продаж в этот день — своя строка
                        acc[k] = _new_acc()
                for k in ordered[:-1]:
                    share = q(amount * weights[k] / total)
                    acc[k][field] += share; spent += share
                acc[ordered[-1]][field] += amount - spent     # остаток копеек — последнему, Σ по дню = списания дня
            else:
                a = acc.get((day, None))
                if a is None:
                    a = acc[(day, None)] = _new_acc()
                a[field] += amount
                source = "(без товара)"
            if field == "ads":
                ads_source[day] = source

    if ads_by_day is not None:
        allocate("ads", ads_by_day, ads_nm_by_day)
    allocate("ads_cashback", ads_cashback_by_day, cashback_nm_by_day)
    if unidentified["rows"]:
        print(f"«{UNIDENTIFIED}» в строках отчёта: {unidentified['rows']} строк за {len(unidentified['days'])} дн., продажи {unidentified['sales']:,.2f}, "
              f"логистика {unidentified['logistics']:,.2f}, rebill {unidentified['rebill']:,.2f}, хранение {unidentified['storage']:,.2f}, прочее {unidentified['other']:,.2f} "
              f"— учтены в строках «{NO_PRODUCT}»", flush=True)
    cashback_total = sum((v for d, v in ads_cashback_by_day.items() if d1 <= d <= d2), Z)
    print(f"правила WB-14: {tally.text()}; нетто аванса «Баллы за отзывы» в окне {', '.join(f'{d} {v:,.2f}' for d, v in sorted(advance_in_window.items())) or 'нет'}"
          + (f", открытый аванс без возврата {advance_open:,.2f}" if advance_open else "")
          + f"; {wbm.advance_info_text(ainfo)}"
          + f"; реклама за кэшбэк WB (справочно, не в форме) {cashback_total:,.2f}; упаковка исключена: строк {pack['rows']}, продажи {pack['sales']:,.2f}, "
          f"штук {pack['qty']}, nmId {sorted(pack['nm_ids']) or '—'}", flush=True)
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
               "ads_allocated": (ads_source.get(day) if (ads_by_day is not None and a["ads"]) else ""), "qty": a["qty"], "no_cost_qty": a["no_cost_qty"],
               "cost_source": "; ".join(sorted(a["cost_sources"]))}
        for k in MONEY_KEYS:
            row[k] = a[k] if (k != "ads" or ads_by_day is not None) else None
        out.append(row)
    if stats is not None:
        cats, brands = defaultdict(int), defaultdict(int)
        for r in out:
            cats[r["category"]] += 1; brands[r["brand"]] += 1
        stats.update({"categories": dict(cats), "brands": dict(brands), "rows_unknown_category": cats.get(CATEGORY_UNKNOWN, 0),
                      "unidentified": {k: (sorted(v) if isinstance(v, set) else v) for k, v in unidentified.items()},
                      "excluded": {"packaging": {k: (sorted(v) if isinstance(v, set) else v) for k, v in pack.items()},
                                   "deductions": dict(tally.by_kind), "deduction_rows": dict(tally.rows), "unknown_deductions": dict(tally.unknown),
                                   "advance_net": advance_in_window, "advance_open": advance_open, "advance_info": ainfo, "ads_cashback": cashback_total,
                                   "tally_text": tally.text()}})
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
                           products=None, only_with_orders=True):
    """Заказы WB по (день, nmId) для общего листа «Заказы»: созданные из воронки, выручка по правилу владельца
    (orderSum × 0,58 / НДС, WB-6 §2), СС снимка по базовому артикулу × штуки, реклама номенклатуры (updSum дня × доля
    fullstats); buyout_* воронки — по дню заказа. Пустые строки (orderSum 0, заказов 0, рекламы 0) не отдаются — их в
    воронке большинство, и книга Ozon-сессии собиралась 34 мин вместо 6 (WB-8 §6); keep_empty=True возвращает всё.
    stats (dict) получает строк было / стало и Σ orderSum по месяцам до и после — они обязаны совпасть.

    WB-10 §3.2 (включено по умолчанию словом владельца 09-25): only_with_orders=True читает воронку фильтром на сервере
    (только карточки с заказами, ~23 страницы вместо 493), а пары (день, nmId) с рекламой номенклатуры без строки
    воронки собирает из словаря products (nmId → vendor_code / title / brand / subject; у таких карточек buyout_* и
    cancel_* воронки = 0 — проверено на 27 287 парах 04-01 … 09-24); products не передан — словарь читается из вьюхи
    wb_funnel_products_latest (откат — таблица окном). С готовыми funnel_rows словарь добирает только пары без строки
    воронки в тот день (83 пары, 33,14 ₽ рекламы за 04-01 … 09-24), которые прежде терялись; приёмка 09-25: три сборки
    дали одни Σ orderSum по месяцам, выручку и СС, реклама +33,14. stats["synthesized"] — сколько собрано."""
    sb = _sb(sb) if funnel_rows is None or costs is None else sb
    funnel_rows = load_funnel_products(date_from, date_to, sb, only_with_orders=only_with_orders) if funnel_rows is None else funnel_rows
    if products is None and hasattr(sb, "table"):
        products = load_product_dictionary(sb, date_from, date_to)
    packaging_nm = packaging_nm_ids(funnel_rows, products)   # WB-14 §4: упаковка — не товар, в заказы не входит
    pack_rows = [r for r in funnel_rows if rules.is_packaging(r.get("subject_name")) or int(r["nm_id"]) in packaging_nm]
    if pack_rows:
        funnel_rows = [r for r in funnel_rows if not (rules.is_packaging(r.get("subject_name")) or int(r["nm_id"]) in packaging_nm)]
    if ads_by_day is None and sb is not None and ads_nm_by_day is None:
        try:
            split = wbm.load_ads_split(sb, date_from, date_to)   # WB-14 §3: только «Баланс»; доли номенклатур — с долей оплаты кампании
            ads_by_day = split["balance"]
            ads_nm_by_day = load_ads_nm_db(sb, date_from, date_to, split["advert_day"], "balance")
        except RuntimeError as error:
            print(f"реклама для заказов не прочитана: {error}", flush=True); ads_by_day, ads_nm_by_day = {}, {}
    nm_ads = nm_ads_by_day_and_nm(ads_by_day or {}, ads_nm_by_day or {})
    synthesized = 0
    if products:
        seen = {(str(r["day"]), int(r["nm_id"])) for r in funnel_rows}
        extra = []
        for (day, nm), ads in sorted(nm_ads.items()):
            if ads and (day, nm) not in seen and nm in products and nm not in packaging_nm and date_from <= day <= date_to:
                p = products[nm]
                extra.append({"day": day, "nm_id": nm, "vendor_code": p.get("vendor_code"), "title": p.get("title"), "brand": p.get("brand"), "subject_name": p.get("subject"),
                              "order_count": 0, "order_sum": 0, "buyout_count": 0, "buyout_sum": 0, "cancel_count": 0, "cancel_sum": 0})
        synthesized = len(extra)
        funnel_rows = sorted(list(funnel_rows) + extra, key=lambda r: (str(r["day"]), int(r["nm_id"])))
    if costs is None or (isinstance(costs, wbm.SnapshotMaps) and hasattr(sb, "table")):
        # WB-17 §2: карты одного снимка из базы (report_finrez.py: wbm.load_costs) при живом клиенте заменяются историей снимков
        # по дате заказа — вслух; обычная пара карт (тесты, файлы) — один снимок
        if costs is not None:
            print(f"себестоимость заказов: карты снимка {costs.snapshot} от вызывающего заменены историей снимков по дате заказа (WB-17 §2)", flush=True)
        hist = costs_for_codes(sb, (r.get("vendor_code") for r in funnel_rows), "строк воронки")
    else:
        hist = wbm.as_cost_history(costs)
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
        cost, cost_src, _step = hist.cost_source(r.get("vendor_code"), None, day, qty)   # WB-17 §2: снимок по дню заказа
        vat = wbm.vat_for(day)
        after[day[:7]] += order_sum
        out.append({"date": day, "month": month_label(day), "month_no": int(day[5:7]), "platform": PLATFORM, "shop": SHOP, "article": (str(r.get("vendor_code") or "").upper() or None),
                    "nm_id": int(r["nm_id"]), "title": r.get("title"), "brand": brand_of(r.get("vendor_code"), r.get("brand")),
                    "category": category_of(r.get("subject_name")), "orders_qty": qty, "orders_sum": order_sum, "ads": ads,
                    "revenue": order_sum * wbm.OWNER_ORDERS_AFTER_COMMISSION / vat, "cogs": (cost * qty) if cost is not None else None,
                    "no_cost": cost is None and qty > 0, "cost_source": cost_src if cost is not None else "",
                    "funnel_buyouts_qty": int(r.get("buyout_count") or 0), "funnel_buyouts_sum": D(r.get("buyout_sum")),
                    "funnel_cancel_qty": int(r.get("cancel_count") or 0), "funnel_cancel_sum": D(r.get("cancel_sum"))})
    if stats is not None:
        stats.update({"rows_before": n_before, "rows_after": len(out), "sum_before": dict(before), "sum_after": dict(after), "equal": dict(before) == dict(after),
                      "synthesized": synthesized, "ads_total": sum((r["ads"] for r in out), Z),
                      "packaging_excluded": {"rows": len(pack_rows), "orders_qty": sum(int(r.get("order_count") or 0) for r in pack_rows),
                                             "orders_sum": sum((D(r.get("order_sum")) for r in pack_rows), Z), "nm_ids": sorted({int(r["nm_id"]) for r in pack_rows})}})
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


COINVEST_SELECT = "rrd_id,rr_date,sale_dt,seller_oper_name,retail_price_with_disc,retail_amount"


def coinvest_share_by_month(month_from, month_to, sb=None, rows=None, date_to=None):
    """Доля соинвеста WB по месяцам — {ярлык месяца: Decimal, "итого": Decimal} (WB-11 §2, для строк заказов Ozon-скрипта
    без измеренной цены покупателя). Формула P владельца по отчёту реализации: (Σ retailPriceWithDisc − Σ retailAmount) /
    Σ retailPriceWithDisc по строкам «Продажа» (+) и «Возврат» (−), день — saleDt МСК, месяц — месяц этого дня (как в мосте
    и в build_rows: coinvest = Σ sign × (price − amount)). Отчёт читается узким SELECT по ключу (rr_date, rrd_id) за
    rr_date month_from-01 … конец month_to + 7 дней (запас под дату продажи, как у листа); rows= — для тестов и повторов."""
    d1, d2 = month_bounds(month_from, month_to, date_to)
    sb = _sb(sb) if rows is None else sb
    if rows is None:
        rows = read_keyset(sb, report_loader.TABLE, COINVEST_SELECT, d1, wbm.window_end(d2), day_col="rr_date", id_col="rrd_id")
    num, den = defaultdict(Decimal), defaultdict(Decimal)
    for r in rows:
        op = r.get("seller_oper_name")
        if op not in ("Продажа", "Возврат"):
            continue
        day = wbm.row_day(r)
        if not (d1 <= day <= d2):
            continue
        sign = 1 if op == "Продажа" else -1
        price, amount = D(r.get("retail_price_with_disc")), D(r.get("retail_amount"))
        num[day[:7]] += sign * (price - amount); den[day[:7]] += sign * price
    out = {}
    for m in months_between(month_from, month_to):
        if den.get(m):
            out[month_label(f"{m}-01")] = ratio(num[m], den[m])
    total_den = sum(den.values(), Z)
    if total_den:
        out["итого"] = ratio(sum(num.values(), Z), total_den)
    return out


def buyout_rate_caption(month_from, month_to, sb=None, rows=None, orders_by_month=None, today=None):
    """Подпись для книги и алерта: «Выкуп по когорте (зрелые дни ≥ 25 сут.): апр 0,5188, …; итого 0,4981; незрелые
    (прогноз по зрелым дням): сен» — одно определение с листом «Коэффициенты» (WB-10 §2)."""
    det = {}
    rates = buyout_rate_for_finrez(month_from, month_to, sb, rows=rows, orders_by_month=orders_by_month, today=today, details=det)
    mature = [m for m, d in det.items() if d["mature"] and d["rate"] is not None]
    immature = [m for m, d in det.items() if not d["mature"]]
    text = f"Выкуп по когорте (зрелые дни ≥ {BUYOUT_RATE_MATURE_DAYS} сут.): " + (", ".join(f"{month_label(m + '-01')} {det[m]['rate']}" for m in mature) or "зрелых месяцев нет")
    if "итого" in rates:
        text += f"; итого {rates['итого']}"
    if immature:
        text += "; незрелые (прогноз по зрелым дням): " + ", ".join(month_label(m + "-01") for m in immature)
    return text


# ---------- приёмка против «WB - месяц» ----------

def bridge_to_month_sheet(rows, report_rows, d1, d2, costs, ads_by_day, advance_net=None):
    """Мост между итогами формы и итогом «WB - месяц» (report_wb_month.build_daily по тем же строкам отчёта) за d1 … d2."""
    days = []
    d = date.fromisoformat(d1)
    while d <= date.fromisoformat(d2):
        days.append(d.isoformat()); d += timedelta(days=1)
    cost_fn = wbm.as_cost_history(costs).cost_fn   # WB-17 §2: та же история снимков, что у строк «Данных»
    daily = wbm.build_daily(report_rows, days, cost_fn, ads_by_day or {}, date.today() + timedelta(days=3), ads_by_day is not None, advance_net=advance_net)
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


def bridge_to_report(rows, report_rows, d1, d2, excluded, ads_split=None):
    """Мост Σ строк «Данных» ↔ сырьё отчёта реализации за d1 … d2 (день строки — row_day) с явными строками правил WB-14:
    упаковка (исключена), удержания за рекламу (исключены), аванс / возврат аванса (исключены) и его нетто, реклама за
    кэшбэк (справочно). Остаток каждой строки обязан быть 0,00."""
    raw = defaultdict(Decimal)
    for r in report_rows:
        if not (d1 <= wbm.row_day(r) <= d2):
            continue
        op = r["seller_oper_name"]
        sign = 1 if op == "Продажа" else (-1 if op == "Возврат" else 0)
        if sign:
            price = D(r["retail_price_with_disc"])
            raw["sales"] += sign * price
            raw["commission"] += sign * price * D(r.get("commission_percent")) / 100
            raw["acquiring"] += sign * D(r.get("acquiring_fee"))
        raw["logistics"] += D(r.get("delivery_service")); raw["storage"] += D(r.get("paid_storage")); raw["rebill"] += D(r.get("rebill_logistic_cost"))
        raw["other"] += D(r.get("penalty")) + D(r.get("deduction")) - D(r.get("additional_payment"))
    ours = defaultdict(Decimal)
    for r in rows:
        for k in ("sales", "commission", "acquiring", "logistics", "storage", "rebill", "other", "ads", "ads_cashback"):
            ours[k] += D(r.get(k))
    pack, ded = excluded.get("packaging") or {}, excluded.get("deductions") or {}
    net = sum((v for v in (excluded.get("advance_net") or {}).values()), Z)
    balance = sum((v for d, v in ((ads_split or {}).get("balance") or {}).items() if d1 <= d <= d2), Z)
    other_pay = sum((v for d, v in ((ads_split or {}).get("other") or {}).items() if d1 <= d <= d2), Z)
    pk = lambda k: -D(pack.get(k))  # noqa: E731
    lines = [
        ("Продажи (оборот)", ours["sales"], raw["sales"], [("упаковка (исключена)", pk("sales"))]),
        ("Комиссия (Σ цена × кВВ)", ours["commission"], raw["commission"], [("упаковка (исключена)", pk("commission"))]),
        ("Эквайринг", ours["acquiring"], raw["acquiring"], [("упаковка (исключена)", pk("acquiring"))]),
        ("Логистика (доставка)", ours["logistics"], raw["logistics"], [("упаковка (исключена)", pk("logistics"))]),
        ("Хранение", ours["storage"], raw["storage"], [("упаковка (исключена)", pk("storage"))]),
        ("справочно: rebill", ours["rebill"], raw["rebill"], [("упаковка (исключена)", pk("rebill"))]),
        ("Ост. расходы и компенсации (H)", ours["other"], raw["other"],
         [("удержания за рекламу «WB Продвижение» (исключены)", -D(ded.get(rules.ADS_WITHHELD))),
          ("аванс / возврат аванса «Баллы за отзывы» (исключены)", -(D(ded.get(rules.REVIEW_ADVANCE)) + D(ded.get(rules.REVIEW_REFUND)))),
          ("нетто аванса в день возврата", net), ("упаковка (исключена)", pk("other"))]),
        ("Реклама (только «Баланс»)", ours["ads"], balance + other_pay, [("кэшбэк-реклама (справочно, вне формы)", -other_pay)]),
        ("справочно: реклама за кэшбэк WB", ours["ads_cashback"], other_pay, []),
    ]
    out = []
    for title, o, t, terms in lines:
        explained = sum((v for _n, v in terms), Z)
        out.append({"title": title, "form": q(o), "month": q(t), "diff": q(o - t), "terms": [(n, q(v)) for n, v in terms], "rest": q(o - t - explained)})
    return out


def ads_withheld_identity(report_rows, ads_split, d1, d2):
    """Тождество WB-14 §2: удержания «WB Продвижение» (по rrDate документа) = списания upd с оплатой «Баланс». Документ дня D
    покрывает дни списаний [дата предыдущего документа, D) — так сумма за 02.04 … 31.08 равна списаниям 01.04 … 31.08 до рубля.
    Возвращает (по месяцам, по документам)."""
    docs = defaultdict(Decimal)
    for r in report_rows:
        if rules.classify_deduction(r.get("bonus_type_name")) == rules.ADS_WITHHELD and D(r.get("deduction")):
            docs[str(r["rr_date"])[:10]] += D(r["deduction"])
    balance = {d: v for d, v in ((ads_split or {}).get("balance") or {}).items()}
    by_month = defaultdict(lambda: [Z, Z])
    for d, v in docs.items():
        if d1 <= d <= d2:
            by_month[d[:7]][0] += v
    for d, v in balance.items():
        if d1 <= d <= d2:
            by_month[d[:7]][1] += v
    per_doc, prev = [], None
    for d in sorted(docs):
        lo = prev or min(balance, default=d)
        spend = sum((v for day, v in balance.items() if lo <= day < d), Z)
        if d1 <= d <= d2:
            per_doc.append((d, docs[d], spend, lo))
        prev = d
    return {m: tuple(v) for m, v in sorted(by_month.items())}, per_doc


def print_bridge_report(table, d1, d2):
    print(f"\nМост «Данные WB выкупы» ↔ отчёт реализации (сырьё) за {d1} … {d2} — правила WB-14")
    print(f"{'строка':52}{'«Данные»':>16}{'отчёт':>16}{'разница':>14}{'остаток':>12}")
    failures = 0
    for t in table:
        print(f"{t['title'][:51]:52}{t['form']:>16,.2f}{t['month']:>16,.2f}{t['diff']:>14,.2f}{t['rest']:>12,.2f}" + ("   ОСТАТОК ≠ 0" if t["rest"] != 0 else ""))
        for name, value in t["terms"]:
            if value:
                print(f"      {value:>+14,.2f}  {name}")
        if t["rest"] != 0:
            failures += 1
    print(f"остаток моста к отчёту ≠ 0 в строках: {failures}")
    return failures


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
    products = load_product_dictionary(sb, d1, d2)
    packaging_nm = packaging_nm_ids(report_rows, products)
    costs = costs_for_codes(sb, (r.get("vendor_code") for r in report_rows if not _is_packaging_row(r, packaging_nm)), "строк отчёта")
    try:
        split = wbm.load_ads_split(sb, d1, d2)   # WB-14 §3: «Реклама» — только оплата «Баланс», кэшбэк — справочно
        ads_by_day = split["balance"]
        print("реклама по видам оплаты (с НДС): " + ", ".join(f"{k} {v:,.2f}" for k, v in sorted(split["by_type"].items())), flush=True)
    except RuntimeError as error:
        print(f"реклама не прочитана: {error}; колонка «Реклама» пуста"); ads_by_day, split = None, None
    nm_rows = load_ads_nm_rows(sb, d1, d2) if ads_by_day is not None else []
    ads_nm_by_day = nm_weights(nm_rows, split["advert_day"], "balance") if split else {}
    cashback_nm = nm_weights(nm_rows, split["advert_day"], "other") if split else {}
    advance_rows = rules.review_points_rows(report_rows)   # WB-16 §1: из прочитанных строк окна, не отдельным чтением
    bstats = {}
    rows = build_rows(args.month_from, args.month_to, args.date_to, sb, rows=report_rows, costs=costs, ads_by_day=ads_by_day, products=products, ads_nm_by_day=ads_nm_by_day, stats=bstats,
                      ads_cashback_by_day=(split or {}).get("other", {}), cashback_nm_by_day=cashback_nm, advance_rows=advance_rows)
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
        advance_net, _open = wbm.advance_net_by_day(sb, advance_rows, d1)
        table, _m, (ppvz, rebill) = bridge_to_month_sheet(rows, report_rows, d1, d2, costs, ads_by_day, advance_net=advance_net)
        failures = print_bridge(table, d1, d2)
        failures += print_bridge_report(bridge_to_report(rows, report_rows, d1, d2, bstats["excluded"], split), d1, d2)
        by_month, per_doc = ads_withheld_identity(report_rows, split, d1, d2)
        print("\nТождество WB-14 §2: удержания «WB Продвижение» (по rrDate документа) против списаний upd с оплатой «Баланс» (по дню списания):")
        for m, (docs, spend) in by_month.items():
            print(f"  {m}: удержания {docs:>14,.2f}  списания «Баланс» {spend:>14,.2f}  разница {docs - spend:>+14,.2f}")
        tot_docs, tot_spend = sum(v[0] for v in by_month.values()), sum(v[1] for v in by_month.values())
        print(f"  итого окна: удержания {tot_docs:,.2f}, списания «Баланс» {tot_spend:,.2f}, разница {tot_docs - tot_spend:+,.2f}")
        exact = sum(1 for _d, v, sp, _lo in per_doc if v == sp)
        print(f"  по документам (документ дня D = списания [предыдущий документ, D)): совпало {exact} из {len(per_doc)}; "
              + "; ".join(f"{d} {v:,.2f} против {sp:,.2f} ({v - sp:+,.2f})" for d, v, sp, _lo in per_doc if v != sp))
        print(f"справочно: возмещение ПВЗ (ppvzReward) {ppvz:,.2f} и возмещение издержек по перевозке (rebillLogisticCost) {rebill:,.2f} с НДС за период — в форму не входят")
        code = 1 if failures else 0
    if args.xlsx:
        notes = [f"Источник: отчёт реализации WB (wb_sales_report_rows), день — saleDt МСК; период {d1} … {d2}; строк «Данные» {len(rows)}.",
                 "Форма — книга второго кабинета (лист «Свод»): N = (K − L)/НДС, R = (Логистика + Хранение)/НДС, T = E/НДС, X = H/НДС, Y = P − R − T − V − X, доли от N.",
                 "Отличие от образца: эквайринг V заполнен (у второго кабинета 0). Логистика ₽ = deliveryService (возмещение издержек по перевозке — не берём, справочно в «Данных»); Ост. расходы = penalty + deduction − additionalPayment.",
                 "Реклама — списания дня (wb_ad_spend_daily, биллинг, с НДС), разнесённые по артикулам долями из статистики по номенклатурам (fullstats, нормировка к списаниям дня; Σ по дню = списаниям), где статистики нет — пропорционально продажам дня.",
                 f"Себестоимость — снимок 1С {wbm.SNAP} по базовому артикулу. Ярлыки одни с Ozon: категория — предмет строки отчёта / карточки по словарю владельца "
                 f"(строчными), прочее — вслух; бренд — по первой букве артикула ({brand_rule_text()}); «Неопознанный товар» — как строки без товара.",
                 "Правила WB-14 (loaders/wb_money_rules, решения владельца 28.09): «Ост. расходы» = штрафы + удержания вида «прочее» − доплаты; удержания "
                 "«Оказание услуг «WB Продвижение»» — реклама с баланса, её деньги уже в «Рекламе» (из upd), сюда не входят; аванс «Баллы за отзывы» и его "
                 "возврат — только нетто в день возврата, строкой «(без товара)»; «Реклама» — только оплата «Баланс», реклама за кэшбэк WB — справочной "
                 "колонкой справа, в форму не входит; упаковка (предмет «Упаковки для украшений») в «Данные» не входит. " + bstats["excluded"]["tally_text"] + ".",
                 buyout_rate_caption(args.month_from, args.month_to, sb)]
        print(notes[-1])
        write_xlsx(args.xlsx, rows, month_rows, split_brand, split_category, notes)
        print(f"записано: {args.xlsx}")
    print("db_writes = 0")
    return code


if __name__ == "__main__":
    sys.exit(main())
