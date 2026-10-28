"""Строки отчёта ОЗЗ «Все» (all_sku_promo/orders) с номером и датой создания заказа — для органики (59-я, слово владельца 28.10).

Зачем: отчёт датирован днём СПИСАНИЯ, не созданием заказа (разведка 28.10, outbox fa68e42): за 23.10 133 строки из 252 — заказы,
созданные на 1–21 день раньше (ставка 5 % списывается при доставке, 6 % — при заказе; гипотеза по двум файлам). Атрибуция
ozon_daily_sku_ad_attribution и расходы остаются по дню списания (деньги); органика берёт ОЗЗ отсюда — по дню создания заказа.

Дата создания — из ozon_posting_status_log: номер отправления = номер заказа + «-N», order_date — день заказа по МСК (тот же, что
у marketplace_orders). Шаг лога статусов идёт в ночи раньше Performance, поэтому вчерашние заказы в нём уже есть. Не нашли —
order_created_date = null, органика ставит такую строку на день списания и называет число вслух.

Запись — по дате списания целиком: upsert строк дня, затем удаление ключей того же дня, которых нет среди построенных (отчёт за
день перезапрашивают — строки, исчезнувшие из ответа, не должны застревать). Таблицы нет / запись упала — предупреждение, шаг
рекламы не падает (как save_ad_attribution_rows).
"""
import csv
import io
import re

from loaders.stale_keys import read_window_rows

TABLE = "ozon_cpo_order_lines"
KEY = ("charge_date", "order_number", "marketplace_sku", "promoted_sku")
LOOKUP_BATCH = 50                     # номеров на запрос like(any); у заказа 1–3 отправления × несколько переходов статуса
PAGE = 1000


def _num(value):
    text = re.sub(r"[^0-9.\-]", "", str(value or "").replace("\xa0", "").replace(" ", "").replace(",", "."))
    try:
        return float(text) if text not in ("", "-", ".", "-.") else 0.0
    except ValueError:
        return 0.0


def _date(value):
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        d, m, y = text.split(".")
        return f"{y}-{m}-{d}"
    return None


def parse_lines(csv_text):
    """CSV отчёта → ([строка на (день списания, номер заказа, SKU, SKU продвигаемого)], счётчики). Строки без даты / номера / SKU
    не теряются молча: счётчики, и Σ расхода всех строк файла — в counters['spend_file'] для сверки с расходами."""
    lines = csv_text.lstrip("\ufeff").splitlines()
    if lines and lines[0].startswith(";"):
        lines = lines[1:]
    grouped, counters = {}, {"rows": 0, "without_date": 0, "without_number": 0, "without_sku": 0, "spend_file": 0.0}
    for raw in csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";"):
        counters["rows"] += 1
        spend = abs(_num(raw.get("Расход, ₽")))
        counters["spend_file"] += spend
        charge_date, number, sku = _date(raw.get("Дата")), str(raw.get("Номер заказа") or "").strip(), str(raw.get("SKU") or "").strip()
        if not charge_date:
            counters["without_date"] += 1
            continue
        if not number:
            counters["without_number"] += 1
            continue
        if not sku:
            counters["without_sku"] += 1
            continue
        promoted = str(raw.get("SKU продвигаемого товара") or "").strip()
        key = (charge_date, number, sku, promoted)
        row = grouped.setdefault(key, {
            "charge_date": charge_date, "order_number": number, "marketplace_sku": sku, "promoted_sku": promoted,
            "order_id": str(raw.get("ID заказа") or "").strip(), "article": str(raw.get("Артикул") or "").strip(),
            "qty": 0.0, "sale_amount": 0.0, "rate_percent": _num(raw.get("Ставка, %")), "spend": 0.0,
            "order_created_date": None, "created_date_source": None})
        row["qty"] += _num(raw.get("Количество"))
        row["sale_amount"] += _num(raw.get("Стоимость продажи, ₽"))
        row["spend"] += spend
    counters["spend_file"] = round(counters["spend_file"], 2)
    return list(grouped.values()), counters


def lookup_created_dates(sb, order_numbers):
    """{номер заказа: день создания по МСК} из ozon_posting_status_log; пачки like(any) «номер-*». У заказа с разными днями у
    отправлений (не встречалось: 246 из 246 за 23.10 — один день) берётся ранний."""
    found = {}
    numbers = sorted(set(order_numbers))
    for i in range(0, len(numbers), LOOKUP_BATCH):
        part = numbers[i:i + LOOKUP_BATCH]
        data = (sb.table("ozon_posting_status_log").select("posting_number,order_date")
                .filter("posting_number", "like(any)", "{" + ",".join(f"{n}-*" for n in part) + "}")
                .order("posting_number").order("observed_at").limit(PAGE).execute().data or [])
        if len(data) >= PAGE:
            raise RuntimeError(f"{TABLE}: пачка {len(part)} номеров дала ≥ {PAGE} строк лога статусов — уменьшить LOOKUP_BATCH")
        for r in data:
            number = r["posting_number"].rsplit("-", 1)[0]
            day = str(r["order_date"])[:10]
            if number not in found or day < found[number]:
                found[number] = day
    return found


def attach_created_dates(rows, created):
    """Проставляет order_created_date / created_date_source; возвращает число строк без даты создания."""
    missing = 0
    for row in rows:
        day = created.get(row["order_number"])
        row["order_created_date"], row["created_date_source"] = (day, "status_log") if day else (None, None)
        missing += 0 if day else 1
    return missing


def summary_line(rows, missing):
    earlier = sum(1 for r in rows if r["order_created_date"] and r["order_created_date"] < r["charge_date"])
    return (f"ОЗЗ по номеру заказа: строк {len(rows)}, созданы в день списания {len(rows) - earlier - missing}, "
            f"раньше {earlier}, без даты создания {missing} (органика поставит их на день списания)")


def save_lines(sb, rows):
    """Upsert строк и удаление ключей тех же дней списания, которых нет среди построенных. → (записано, удалено)."""
    if not rows:
        return 0, 0
    days = sorted({r["charge_date"] for r in rows})
    built = {tuple(r[k] for k in KEY) for r in rows}
    for i in range(0, len(rows), 500):
        sb.table(TABLE).upsert(rows[i:i + 500], on_conflict=",".join(KEY)).execute()
    existing = read_window_rows(sb, TABLE, ",".join(KEY), [("gte", "charge_date", days[0]), ("lte", "charge_date", days[-1])], KEY)
    existing = [r for r in existing if str(r["charge_date"])[:10] in days]
    stale = [r for r in existing if tuple(r[k] for k in KEY) not in built]
    for r in stale:
        q = sb.table(TABLE).delete()
        for k in KEY:
            q = q.eq(k, r[k])
        q.execute()
    return len(rows), len(stale)


def store(sb, csv_text, write=True):
    """Разобрать, найти даты создания, записать. Ошибка записи — предупреждение, не исключение. → сводка для run_summary."""
    rows, counters = parse_lines(csv_text)
    try:
        missing = attach_created_dates(rows, lookup_created_dates(sb, [r["order_number"] for r in rows]))
    except Exception as exc:  # noqa: BLE001 — лог статусов недоступен: строки пишутся без даты, органика возьмёт день списания
        print(f"WARNING: {TABLE}: даты создания не прочитаны из ozon_posting_status_log ({type(exc).__name__}: {str(exc)[:160]})")
        missing = attach_created_dates(rows, {})
    print(summary_line(rows, missing))
    out = {"rows": len(rows), "without_created_date": missing, "counters": counters, "writes": 0, "deleted": 0}
    if not write:
        print(f"{TABLE}: не пишу (сухой прогон), db_writes = 0")
        return out
    try:
        out["writes"], out["deleted"] = save_lines(sb, rows)
        print(f"✅ {TABLE}: записано {out['writes']}, удалено устаревших ключей {out['deleted']}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: {TABLE} не записана ({type(exc).__name__}: {str(exc)[:160]}) — миграция "
              "sql/20261029_ozon_cpo_order_lines_and_organic_flag.sql применена? Органика возьмёт ОЗЗ из атрибуции по дню списания")
    return out


def read_for_created_window(sb, day_from, day_to, max_lag_days=45):
    """Строки, нужные органике окна [day_from, day_to]: списанные в [day_from, day_to + max_lag]. Органика сама решает по каждой
    строке: день создания в окне → туда; без даты создания → день списания."""
    from datetime import date, timedelta
    to = (date.fromisoformat(day_to) + timedelta(days=max_lag_days)).isoformat()
    return read_window_rows(sb, TABLE, "*", [("gte", "charge_date", day_from), ("lte", "charge_date", to)], KEY)
