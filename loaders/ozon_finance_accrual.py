"""Разбор /v1/finance/accrual/by-day — замена умершего /v3/finance/transaction/list.

Старый метод отключён 2026-09-08 («obsolete method cannot be used»).

ЧТО ИЗМЕНИЛОСЬ В МОДЕЛИ ДАННЫХ. Старый метод отдавал операцию с одним
operation_type на всю операцию. Новый отдаёт начисление, внутри которого
услуги разложены построчно, у каждой свой type_id: 182 начисления из 1000 за
2026-09-03 несут по две услуги разных типов. Поэтому старое operation_type из
новых данных не восстанавливается в принципе, и классифицировать надо СТРОКУ
УСЛУГИ, а не начисление.

КОМИССИЯ — не услуга: она лежит отдельным блоком posting.products[].commission
с именованными полями и без type_id.

ПРИНЦИП РАЗБОРА (шаг первый миграции): менять смысл нельзя, поэтому в известные
типы расходов попадает только то, соответствие чего доказано — либо сверкой
обоих методов на 161 дате (data/ozon_finance_migration_parity_20260906.json),
либо сопоставлением операций по (номер отправления, сумма) со стопроцентной
чистотой. Всё остальное уходит в unknown_<type_id> — видимым и с ценником, а
не молча в «прочее». Разбор unknown — отдельная работа, шаг второй.
"""
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт
    import http_retry

BASE = "https://api-seller.ozon.ru"

# ДАТА РАЗРЫВА РЯДА РАСХОДОВ — снята пересчётом 2026-09-11. Вся история с
# 2026-03-28 пересобрана новой классификацией, ряд снова единый.
# Держим дату для истории: до неё расходы БЫЛИ собраны старой логикой. Проверено строго:
# ни одно подмножество type_id не воспроизводит прежние суммы, потому что
# старая величина складывалась из услуг разных типов внутри одной операции.
EXPENSES_SERIES_BREAK_DATE = "2026-09-08"
EXPENSES_HISTORY_RECALCULATED = True  # пересчёт выполнен 2026-09-11, 168 дат, расхождений 0

# Реклама берётся из Performance API. Финансы для неё — только сверка, иначе
# расход задвоится: витрины читают advertising_clicks и advertising_order_5.
AD_TYPE_IDS = {41, 54}

# Соответствие подтверждено сверкой обоих методов на 161 дате.
TYPE_TO_EXPENSE = {
    # Классификация владельца от 2026-09-11. Основание по каждому типу — в
    # docs/ozon_finance_migration.md. Реклама (41, 54) не пишется: её источник
    # Performance API. Типы 25 ItemCompensation и 10 Compensation оставлены ВНЕ
    # классификации: у них возвраты кратно больше списаний, это деньги нам, а
    # не от нас, и природа не установлена.
    #
    # Имя external_promo НЕ начинается с advertising намеренно: пять мест берут
    # рекламу по startswith("advertising"), и префикс утянул бы внешнее
    # продвижение в рекламный расход и в расчёт органики.
    #
    # Доставка, приёмка, обработка, возвратная логистика:
    32: "logistics",  # Logistic
    29: "logistics",  # LastMileCourier
    98: "logistics",  # DeliveryToHandoverPlaceByOzon
    59: "logistics",  # ReturnFlowLogistic
    65: "logistics",  # RfbsEasyReturn
    71: "logistics",  # SellerReturns
    6:  "logistics",  # Cancellation
    9:  "logistics",  # ClientReturn
    40: "logistics",  # PartialReturn
    64: "logistics",  # RfbsDomesticDelivery
    # По справочнику владельца «Тип начисления → Вид» (docs/owner_manual_report_instruction.md, сверено 2026-09-23 с его
    # полотном за ноябрь 2025 по суммам до копейки): «Размещение товаров на складах Ozon» и «Агентское вознаграждение Ozon
    # Агрегатор realFBS» у него — Логистика. До 09-23 лежали в other.
    46: "logistics",  # Placements — «Размещение товаров на складах Ozon»
    63: "logistics",  # RfbsDomesticAgentFee — «Агентское вознаграждение Ozon Агрегатор realFBS»
    # Подписки — отдельная статья: платим за подписку, а не за операцию.
    # Проверено 2026-09-11: в комиссию НЕ входят. sale_commission равна
    # sale_amount × commission_ratio на 1000 строк из 1000, а услуга 51
    # приходит начислениями категории ITEM, тогда как комиссия живёт в POSTING.
    51: "subscription",  # PremiumMembership
    52: "subscription",  # PremiumSubscription
    74: "subscription",  # StarsMembership
    # Внешнее продвижение: заказов не приписывает, в органике не участвует.
    23: "external_promo",  # InternetSiteAdvertising
    # Прочие затраты: штрафы, разовые услуги, корректировки.
    1:  "other",  # Acquiring
    # По тому же справочнику владельца эти шесть — Прочее, не Логистика (до 2026-09-23 лежали в logistics): обработка
    # Drop-off (СЦ / ПВЗ / партнёрами), обработка возвратов, отмен и невыкупов партнёрами, перечисление за доставку от
    # покупателя, временное размещение в СЦ/ПВЗ, дополнительная обработка ОВХ. Сентябрь 1–21: Σ +812,00 со знаком Ozon.
    16: "other",  # Drop-Off — «Обработка отправления Drop-off»
    17: "other",  # Drop-Off Agent — «Обработка отправления Drop-off партнёрами»
    45: "other",  # PickUpPointReturnAcceptance — «Обработка возвратов, отмен и невыкупов партнёрами»
    62: "other",  # RfbsClientDeliveryCharge — «Перечисление за доставку от покупателя» (доход, знак +)
    78: "other",  # TemporaryPlacement — «Краткосрочное размещение возврата FBS» (у владельца «Временное размещение товара в СЦ/ПВЗ»)
    82: "other",  # VolumeWeightCharacteristicsProcessing — «Дополнительная обработка ОВХ»
    124: "other",  # RfbsBuyerDelivery — то же название ЛК «Перечисление за доставку от покупателя», что у 62 (доход, знак +)
    38: "other",  # PackageCost
    39: "other",  # PackingFee
    61: "other",  # ReviewsPin
    79: "other",  # TemporaryPlacementsAgent
    76: "other",  # StockInsurance
    94: "other",  # DefectFineShipmentDelayRate
    96: "other",  # AcceleratedReviewCollection
    93: "other",  # DefectFineErrors
    92: "other",  # DefectFineComplaint
    47: "other",  # PointsForReviews
    15: "other",  # Disposal — утилизация, не доставка
    57: "other",  # RealizationReportCorrection
    11: "other",  # CorrectionCommission
    # Были в полотне владельца за ноябрь 2025, в сентябре 2026 не встречались (тридцать пятая задача, §5): без строки здесь
    # упали бы в unknown_*. По его справочнику: 3 «Продвижение бренда» → Реклама (у нас рекламной статьи из начислений нет —
    # реклама идёт из Performance, решение 5; кладём в other, как 96, и помечаем), 14 и 77 → Прочее.
    3:  "other",  # BrandCommission — «Продвижение бренда»; у владельца Реклама, у нас other с пометкой (как 96)
    14: "other",  # DefectRate — «Обработка операционных ошибок продавца»
    77: "other",  # SupplyInbound — «Обработка товара» (в ЛК владельца — «Удержание за недовложение товара»)
}

# Вне классификации: деньги идут НАМ, природа не установлена. В расходы не
# пишем, видим отдельно. Разрешит только сверка с отчётом в кабинете.
UNCLASSIFIED_TYPE_IDS = {25, 10}

def headers():
    return {
        "Client-Id": os.getenv("OZON_CLIENT_ID"),
        "Api-Key": os.getenv("OZON_API_KEY"),
        "Content-Type": "application/json",
    }


def money(node):
    """Сумма со ЗНАКОМ. Модуль на деньгах запрещён: знак отличает списание от
    возврата, и его потеря дважды приводила к неверным выводам."""
    try:
        return float((node or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def load_accrual_types():
    r = http_retry.post(f"{BASE}/v1/finance/accrual/types", label="accrual/types",
                        headers=headers(), json={}, timeout=60)
    if r.status_code != 200:
        return {}
    return {t["id"]: t.get("name") or "" for t in (r.json() or {}).get("accrual_types", [])}


def fetch_day(day, stats=None):
    """Начисления за одну дату. Метод принимает ровно одну дату, не диапазон. stats — см. http_retry.request."""
    out, last_id = [], None
    while True:
        body = {"date": day}
        if last_id:
            body["last_id"] = last_id
        r = http_retry.post(f"{BASE}/v1/finance/accrual/by-day", label="accrual/by-day",
                            headers=headers(), json=body, timeout=180, stats=stats)
        if r.status_code != 200:
            raise RuntimeError(
                f"accrual/by-day {day}: HTTP {r.status_code} {r.text[:200]}"
            )
        if stats is not None:
            stats["pages"] = stats.get("pages", 0) + 1
        data = r.json() or {}
        batch = data.get("accruals") or []
        out.extend(batch)
        last_id = data.get("last_id")
        if not batch or not last_id:
            return out


def fetch_window_checked(days_back=30, now_utc=None):
    """Начисления окна [сегодня − days_back, сегодня] по UTC и паспорт сбора.

    Паспорт: day_from, day_to, days, pages, requests, retries, failures, complete. complete — все дни получены
    без единого повтора и отказа: только такой сбор годится как основание удалять ключи окна, которых в нём нет
    (loaders/stale_keys.py). Отказ дня (не 200 после повторов) — исключение, как и раньше: шаг падает целиком,
    ничего не пишется и не удаляется.
    """
    day_to = (now_utc or datetime.now(timezone.utc)).date()
    day_from = day_to - timedelta(days=days_back)
    stats = {}
    out, cur = [], day_from
    while cur <= day_to:
        out.extend(fetch_day(cur.isoformat(), stats))
        cur += timedelta(days=1)
    window = {"day_from": day_from.isoformat(), "day_to": day_to.isoformat(), "days": days_back + 1,
              "pages": stats.get("pages", 0), "requests": stats.get("requests", 0),
              "retries": stats.get("retries", 0), "failures": stats.get("failures", 0)}
    window["complete"] = window["retries"] == 0 and window["failures"] == 0
    return out, window


def fetch_window(days_back=30):
    return fetch_window_checked(days_back)[0]


def _service_lines(accrual):
    """Строки услуг: (type_id, sku, сумма со знаком)."""
    posting = accrual.get("posting") or {}
    for product in (posting.get("products") or []):
        sku = str(product.get("sku") or "")
        for block in ("delivery", "commission"):
            node = product.get(block) or {}
            for service in (node.get("services") or []):
                yield service.get("type_id"), sku, money(service.get("accrued"))
    for fee in ((accrual.get("item_fees") or {}).get("fees") or []):
        sku = str(fee.get("sku") or "")
        for service in (fee.get("fees") or []):
            yield service.get("type_id"), sku, money(service.get("accrued"))
    non_item = accrual.get("non_item_fee")
    if isinstance(non_item, dict):
        for service in (non_item.get("fees") or []):
            yield service.get("type_id"), "", money(service.get("accrued"))
        if non_item.get("type_id") is not None:
            yield non_item.get("type_id"), "", money(non_item.get("accrued"))
    container = accrual.get("container_fees")
    if isinstance(container, dict):
        for service in (container.get("fees") or []):
            yield service.get("type_id"), "", money(service.get("accrued"))


def build_expense_rows(accruals, type_names=None):
    """Строки расходов из начислений. Возвращает (строки, счётчики, unknown)."""
    type_names = type_names or {}
    grouped = {}
    counters = defaultdict(int)
    unknown = defaultdict(float)
    unclassified = defaultdict(float)

    def put(day, sku, expense_type, amount):
        # Расход положителен при списании: Ozon отдаёт списание отрицательным.
        value = -amount
        if value == 0:
            counters["zero"] += 1
            return
        key = (day, "ozon", sku, expense_type)
        if key not in grouped:
            grouped[key] = {
                "expense_date": day,
                "marketplace_code": "ozon",
                "marketplace_sku": sku,
                "article": "",
                "expense_type": expense_type,
                "expense_amount": 0.0,
            }
        grouped[key]["expense_amount"] += value
        counters[expense_type] += 1

    for accrual in accruals:
        day = str(accrual.get("date") or "")[:10]
        if not day:
            counters["without_date"] += 1
            continue
        # Ozon (2026-09-10): одинаковых SKU внутри начисления не бывает, а две
        # строки услуги с ОДНИМ type_id внутри одного SKU «теоретически»
        # возможны — «мы сейчас не отдаём, но явного запрета нет».
        # Суммирование ниже это выдерживает (замещения нет), но факт надо
        # видеть: если «теоретически» станет практикой, мы узнаем из лога, а не
        # из расхождения по деньгам через месяц.
        seen_service_keys = set()
        posting = accrual.get("posting") or {}
        for product in (posting.get("products") or []):
            commission = product.get("commission") or {}
            if commission:
                put(day, str(product.get("sku") or ""), "commission",
                    money(commission.get("sale_commission")))
        for type_id, sku, amount in _service_lines(accrual):
            if type_id in AD_TYPE_IDS:
                counters["advertising_skipped"] += 1
                continue
            if type_id in UNCLASSIFIED_TYPE_IDS:
                # Деньги идут НАМ, природа не установлена. Ни в расходы, ни в
                # доходы, пока владелец не сверит с кабинетом.
                counters["unclassified_skipped"] += 1
                unclassified[type_id] += -amount
                continue
            expense_type = TYPE_TO_EXPENSE.get(type_id)
            if expense_type is None:
                expense_type = f"unknown_{type_id}"
                unknown[type_id] += -amount
            service_key = (accrual.get("accrual_id"), sku, type_id)
            if service_key in seen_service_keys:
                counters["duplicate_service_line"] += 1
                print(
                    "ВНИМАНИЕ: в одном SKU две строки услуги одного типа — "
                    f"accrual_id={accrual.get('accrual_id')} sku={sku} type_id={type_id}. "
                    "Суммируем (замещения нет), но факт зафиксирован."
                )
            seen_service_keys.add(service_key)
            put(day, sku, expense_type, amount)

    rows = [dict(r, expense_amount=round(r["expense_amount"], 2)) for r in grouped.values()]
    rows = [r for r in rows if r["expense_amount"] != 0]
    if unclassified:
        print("Вне классификации (в расходы НЕ идут, ждут сверки с кабинетом):")
        for type_id, amount in sorted(unclassified.items(), key=lambda x: -abs(x[1])):
            print(f"    type_id {type_id:<5}{amount:>16,.2f}")
    return rows, dict(counters), {
        int(k): round(v, 2) for k, v in sorted(unknown.items(), key=lambda x: -abs(x[1]))
    }


def build_type_ledger_rows(accruals, type_names=None, loaded_at=None):
    """Леджер по типам: (дата начисления, type_id) → сумма со знаком Ozon и число строк услуг.

    ВСЕ типы, какие отдал Ozon, — включая рекламу 41 / 54 и компенсации 25 / 10,
    которых в marketplace_expenses нет, и незнакомые. Классификации здесь нет
    намеренно: тип хранится числом, статья — дело читателя (TYPE_TO_EXPENSE).

    Знак — как у Ozon: списание с продавца < 0, начисление продавцу > 0. В
    marketplace_expenses он перевёрнут (расход положителен) — не путать.
    Нулевая сумма пишется: «тип был, но свернулся в ноль» — не то же, что
    «типа не было». Строки услуг — те же, что у расходов (_service_lines), поэтому
    Σ леджера по типам статьи со сменой знака обязана равняться статье расходов.
    Комиссия продажи — не услуга, type_id у неё нет, в леджер не входит.
    """
    type_names = type_names or {}
    loaded_at = loaded_at or datetime.now(timezone.utc).isoformat()
    sums, lines = defaultdict(Decimal), defaultdict(int)
    without_date = without_type = 0
    for accrual in accruals:
        day = str(accrual.get("date") or "")[:10]
        if not day:
            without_date += 1
            continue
        for type_id, _sku, amount in _service_lines(accrual):
            if type_id is None:
                without_type += 1      # строка услуги без типа: в леджер по типам её положить некуда — называем
                continue
            # money() отдаёт float из строки Ozon с двумя знаками; str(float) возвращает её же, Decimal точен
            sums[(day, int(type_id))] += Decimal(str(amount))
            lines[(day, int(type_id))] += 1
    if without_date or without_type:
        print(f"Леджер типов: начислений без даты {without_date}, строк услуг без type_id {without_type} — в леджер не вошли")
    return [{
        "accrual_date": day,
        "type_id": type_id,
        "type_name": type_names.get(type_id) or None,
        "amount": float(amount.quantize(Decimal("0.01"))),
        "lines": lines[(day, type_id)],
        "loaded_at": loaded_at,
    } for (day, type_id), amount in sorted(sums.items())]


def build_buyout_rows(accruals):
    """Выкупы из начислений: продажи и возвраты одной таблицей.

    Продажа и возврат в новой модели — одно и то же начисление с обратными
    знаками во всех полях. Отдельного типа операции больше нет, знак несёт
    смысл сам:

        продажа   sale_amount +2751,00   sale_commission −1237,95
        возврат   sale_amount −2272,00   sale_commission +1067,84

    Комиссия у продажи отрицательна (удержана), у возврата положительна
    (возвращена нам). Модуль здесь запрещён: он превратил бы возврат комиссии
    в расход. Старая конструкция «abs() плюс sign по типу операции» не просто
    не нужна — она бы здесь всё сломала.
    """
    grouped = {}
    counters = defaultdict(int)

    for accrual in accruals:
        day = str(accrual.get("date") or "")[:10]
        if not day:
            counters["without_date"] += 1
            continue
        for product in ((accrual.get("posting") or {}).get("products") or []):
            commission = product.get("commission") or {}
            if not commission:
                continue
            sale_amount = money(commission.get("sale_amount"))
            sale_commission = money(commission.get("sale_commission"))
            if sale_amount == 0 and sale_commission == 0:
                counters["zero"] += 1
                continue
            sku = str(product.get("sku") or "")
            key = (day, "ozon", sku)
            if key not in grouped:
                grouped[key] = {
                    "buyout_date": day,
                    "marketplace_code": "ozon",
                    "marketplace_sku": sku,
                    "article": "",
                    "product_name": None,
                    "buyouts_qty": 0,
                    "buyouts_amount_buyer": 0.0,
                    "buyouts_amount_seller": 0.0,
                    "commission_amount": 0.0,
                    "revenue_after_commission_vat": 0.0,
                    "vat_amount": 0.0,
                    "bonus_amount": 0.0,
                    "coinvestment_amount": 0.0,
                }
            row = grouped[key]
            # ВНИМАНИЕ: количество. Поля штук в новой модели НЕТ — у товарной
            # строки всего три поля: sku, delivery, commission. Считаем позиции.
            # Старый код считал записи items[], где один SKU мог встречаться
            # дважды (две моносерьги = две записи), поэтому его число больше:
            # на 2026-09-07 в базе 234 против 227 позиций. Расхождение известно
            # и НЕ подогнано: сумма, комиссия и выручка сходятся точно, а штуки
            # новая модель не отдаёт.
            row["buyouts_qty"] += 1 if sale_amount >= 0 else -1
            # Оплачено покупателем — commission.sale_price, ЗА СТРОКУ (не за единицу: 2026-09-24, 62 из 64 строк с кол-вом > 1
            # равны CSV ЛК «Оплачено покупателем» × кол-во); баллы за скидки и зелёные цены (соинвест Ozon) — bonus и coinvestment.
            # Тождество построчно: sale_amount − sale_price = bonus + coinvestment (983 из 998 строк на 4 днях; остаток — счётчик).
            # До 2026-09-24 в buyouts_amount_buyer писалась цена продавца (дубль buyouts_amount_seller).
            sale_price = money(commission.get("sale_price"))
            bonus = money(commission.get("bonus"))
            coinvestment = money(commission.get("coinvestment"))
            row["buyouts_amount_buyer"] += sale_price
            row["bonus_amount"] += bonus
            row["coinvestment_amount"] += coinvestment
            if round(sale_amount - sale_price - bonus - coinvestment, 2) != 0:
                counters["coinvest_identity_broken"] += 1
            row["buyouts_amount_seller"] += sale_amount
            # Комиссия в таблице хранится положительной у продажи.
            row["commission_amount"] += -sale_commission
            # Выручка после комиссии включает и услуги доставки: старый код
            # брал её из amount целой операции, а он вычитал логистику тоже.
            # Проверено на 2026-09-07: sale_amount + commission даёт
            # 2 443 481,77 против 2 421 187,15 в базе, с услугами — ровно
            # 2 421 187,15.
            delivery_services = sum(
                money(service.get("accrued"))
                for service in ((product.get("delivery") or {}).get("services") or [])
            )
            row["revenue_after_commission_vat"] += sale_amount + sale_commission + delivery_services
            counters["sale" if sale_amount >= 0 else "return"] += 1

    rows = []
    for row in grouped.values():
        for field in ("buyouts_amount_buyer", "buyouts_amount_seller",
                      "commission_amount", "revenue_after_commission_vat", "bonus_amount", "coinvestment_amount"):
            row[field] = round(row[field], 2)
        if any(row[f] for f in ("buyouts_qty", "buyouts_amount_seller", "buyouts_amount_buyer", "commission_amount")):
            rows.append(row)
    return rows, dict(counters)
