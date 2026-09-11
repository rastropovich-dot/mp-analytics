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

try:
    from loaders import http_retry
except ImportError:  # пайплайн зовёт как скрипт
    import http_retry

BASE = "https://api-seller.ozon.ru"

# ДАТА РАЗРЫВА РЯДА РАСХОДОВ. До неё расходы собраны старой логикой (по
# operation_type целой операции), после — новой (построчно по услугам).
# Периоды НЕСОПОСТАВИМЫ, пока не выполнен пересчёт истории. Проверено строго:
# ни одно подмножество type_id не воспроизводит прежние суммы, потому что
# старая величина складывалась из услуг разных типов внутри одной операции.
EXPENSES_SERIES_BREAK_DATE = "2026-09-08"
EXPENSES_HISTORY_RECALCULATED = False

# Реклама берётся из Performance API. Финансы для неё — только сверка, иначе
# расход задвоится: витрины читают advertising_clicks и advertising_order_5.
AD_TYPE_IDS = {41, 54}

# Соответствие подтверждено сверкой обоих методов на 161 дате.
TYPE_TO_EXPENSE = {
    1: "other",    # Acquiring        <- MarketplaceRedistributionOfAcquiringOperation
    46: "other",   # Placements       <- OperationMarketplaceServiceStorage
    38: "other",   # PackageCost      <- OperationMarketplacePackageMaterialsProvision
    39: "other",   # PackingFee       <- OperationMarketplacePackageRedistribution
    61: "other",   # ReviewsPin       <- OperationMarketPlaceItemPinReview
    # Соответствие подтверждено сопоставлением операций со 100 % чистотой.
    79: "other",       # TemporaryPlacementsAgent <- OperationMarketplaceItemTemporaryStorageRedistribution
    17: "logistics",   # Drop-Off Agent           <- OperationReturnGoodsFBSofRMS
    62: "logistics",   # RfbsClientDeliveryCharge <- MarketplaceSellerReexposureDeliveryReturnOperation
    64: "logistics",   # RfbsDomesticDelivery     <- MarketplaceServiceRedistributionOfDeliveryServicesRFBS
}


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


def fetch_day(day):
    """Начисления за одну дату. Метод принимает ровно одну дату, не диапазон."""
    out, last_id = [], None
    while True:
        body = {"date": day}
        if last_id:
            body["last_id"] = last_id
        r = http_retry.post(f"{BASE}/v1/finance/accrual/by-day", label="accrual/by-day",
                            headers=headers(), json=body, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(
                f"accrual/by-day {day}: HTTP {r.status_code} {r.text[:200]}"
            )
        data = r.json() or {}
        batch = data.get("accruals") or []
        out.extend(batch)
        last_id = data.get("last_id")
        if not batch or not last_id:
            return out


def fetch_window(days_back=30):
    day_to = datetime.now(timezone.utc).date()
    day_from = day_to - timedelta(days=days_back)
    out, cur = [], day_from
    while cur <= day_to:
        out.extend(fetch_day(cur.isoformat()))
        cur += timedelta(days=1)
    return out


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
    return rows, dict(counters), {
        int(k): round(v, 2) for k, v in sorted(unknown.items(), key=lambda x: -abs(x[1]))
    }
