#!/usr/bin/env python3
"""Лист «WB - <месяц>» в утренней книге Ozon: строки — функциями scripts/report_wb_month.py (импорт, не копия), запись — в
уже собранную книгу генератора, вторым листом после «Ozon - <месяц>».

    venv/bin/python3 scripts/book_wb_sheet.py --book data/reports/ozon_2026-09_to_2026-09-24.xlsx --month 2026-09 --date-to 2026-09-24

Что импортируется из report_wb_month: month_days, load_rows_db, load_costs, unit_cost_for, load_ads_db, build_daily, total_row,
COLS, YOUNG_DAYS, SNAP — те же строки и та же раскладка колонок, что у отдельной книги WB (реклама — из таблицы
wb_ad_spend_daily, СС — снимок 1С, дни моложе двух суток — жёлтые). Не импортируется только write_xlsx: он создаёт свою
книгу (openpyxl.Workbook()) и не умеет писать в чужую — поэтому лист пишется здесь тем же списком колонок COLS.
Обращений к WB API — 0: всё из базы. В БД не пишет.

Листы «Заказы WB» (п. 95 РБХ, вариант 2 из п. 93.4): в ту же книгу, в конец, — «Заказы WB», «Заказы WB Standard» и, если в профиле
кабинета задана буква «Дискаунтера», «Заказы WB Дискаунтер» — теми же функциями report_wb_month, что пишут их в отдельную книгу WB:
load_funnel_db (wb_funnel_products_daily), coinvest_by_day, build_orders_daily, orders_total_row, _write_orders_sheet, площадки —
PLATFORMS. Воронка не прочиталась или пуста — книга без этих листов, причина печатается; лист «WB - <месяц>» от них не зависит.
"""
import argparse
import os
import sys
from datetime import date, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import cabinet  # noqa: E402
CABINET = cabinet.assert_env()  # кабинет (MP_CABINET) и база (SUPABASE_URL) должны совпасть — до чтения ключей и создания клиента
import report_wb_month as wb_report  # noqa: E402
import loaders.wb_sales_report_loader as wb_loader  # noqa: E402

Z = Decimal(0)


def build_wb_daily(month, date_to, sb=None, snapshot=None, today=None):
    """(строки по дням, итог, заметки, сводка обращений) — функциями report_wb_month; API не зовётся."""
    sb = sb or wb_loader._client()
    today = today or date.today()
    days = wb_report.month_days(month, date_to)
    d1, d2 = days[0], days[-1]
    rows = wb_report.load_rows_db(sb, d1, d2)
    cstats = {}
    hist = wb_report.load_cost_history_for(sb, (r.get("vendor_code") for r in rows), stats=cstats)   # WB-17 §2: СС по дате продажи
    ads_note, ads_known, ads_by_day = "", False, {}
    try:
        ads_by_day, undated = wb_report.load_ads_db(sb, d1, d2)
        ads_known = True
        ads_note = f"реклама: {wb_report.ads_loader.TABLE} за {d1}…{d2} — списаний {sum(ads_by_day.values(), Z):,.2f} с НДС" + (f", без даты {undated:,.2f}" if undated else "")
    except Exception as error:  # реклама не обязана останавливать лист; пусто — не ноль (как у report_wb_month)
        ads_note = f"реклама не получена: {error}"
    cost_fn = hist.cost_fn
    outside, excluded = {}, {}
    ainfo = {}
    try:   # WB-16 §1: нетто аванса «Баллы за отзывы» — из тех же строк окна; аванс раньше окна дочитывается по индексу rr_date
        advance_net, _open = wb_report.advance_net_by_day(sb, rows, d1, info=ainfo)
        advance_note = wb_report.advance_info_text(ainfo)
    except Exception as error:  # noqa: BLE001 — упала только дочитка; лист без нетто, сказано в заметках
        advance_net, advance_note = {}, f"аванс «Баллы за отзывы» не прочитан: {str(error)[:120]} — нетто аванса в «Прочее» не добавлено"
    daily = wb_report.build_daily(rows, days, cost_fn, ads_by_day, today, ads_known, outside, advance_net=advance_net, excluded=excluded)
    total = wb_report.total_row(daily)
    young = [r["date"] for r in daily if r.get("young")]
    notes = [f"Источник: отчёт реализации WB (finance-api sales-reports/detailed, period=daily) из таблицы {wb_loader.TABLE}; строк {len(rows)}; "
             "день строки — saleDt в московском времени, без saleDt — rrDate. Строки и колонки — те же функции, что у отдельной книги WB (scripts/report_wb_month.py).",
             f"Комиссия = Σ цена × кВВ строки (commissionPercent), как у владельца; справочно «удержано из выплаты всего» = Оборот − forPay. НДС {total['vat']}.",
             f"{hist.note()} Без СС {total['no_cost_positions']} из {total['positions']} позиций — оборот {total['no_cost_turnover']:,.2f}.",
             ads_note,
             "Правила WB-14 (loaders/wb_money_rules): «Прочее» без удержаний «WB Продвижение» (реклама с баланса) и без аванса «Баллы за отзывы» "
             "(нетто — в день возврата); реклама — только оплата «Баланс»; упаковка исключена"
             + (f" (строк {excluded['packaging']['rows']}, оборот {excluded['packaging']['turnover']:,.2f})" if excluded.get("packaging", {}).get("rows") else "")
             + (f"; {advance_note}" if advance_note else "") + ".",
             f"Жёлтым — дни моложе {wb_report.YOUNG_DAYS} суток: строки доезжают." + (f" Таких дней: {', '.join(young)}." if young else "")]
    try:   # п. 95: листы заказов не обязаны останавливать лист выкупов — без них книга как раньше, причина в строке
        orders, funnel_note = build_wb_orders(sb, rows, days, cost_fn, ads_by_day, ads_known, today, total, hist.note())
    except Exception as error:  # noqa: BLE001
        orders, funnel_note = [], f"листы «Заказы WB» не собраны: {type(error).__name__}: {str(error)[:160]}"
    return daily, total, notes, {"rows": len(rows), "days": len(days), "young": young, "orders": orders, "funnel_note": funnel_note}


def build_wb_orders(sb, rows, days, cost_fn, ads_by_day, ads_known, today, total, cost_note=""):
    """([(название листа, строки, итог, подвал)], строка о воронке) — листы заказов WB для утренней книги (п. 95 РБХ).

    Те же функции, что у отдельной книги WB (report_wb_month.main): load_funnel_db — wb_funnel_products_daily за дни листа,
    coinvest_by_day — по тем же строкам отчёта реализации, build_orders_daily / orders_total_row — по площадкам PLATFORMS;
    множитель «Выручки» — константа владельца из профиля, а без неё — 1 − комиссия листа выкупов (расчётный). Воронка не
    прочиталась (RuntimeError у load_funnel_db) или пуста — листов нет, причина в строке."""
    try:
        funnel_rows = wb_report.load_funnel_db(sb, days[0], days[-1])
    except RuntimeError as error:
        return [], f"воронка по товарам не прочитана: {error}; листы «Заказы WB» не собраны"
    funnel_note = f"воронка по товарам: {wb_report.FUNNEL_TABLE}, строк {len(funnel_rows)}"
    if not funnel_rows:
        return [], funnel_note + "; листы «Заказы WB» не собраны"
    after_k = wb_report.OWNER_ORDERS_AFTER_COMMISSION
    after_note = f"Выручка = Σ orderSum × {after_k} / НДС (как K владельца)"
    if after_k is None and total.get("commission_pct") is not None:
        after_k = (Decimal(1) - total["commission_pct"]).quantize(Decimal("0.0001"))
        after_note = (f"Выручка = Σ orderSum × {after_k} / НДС — расчётный множитель (1 − комиссия листа {total['commission_pct']:.2%}), "
                      "константа владельца не задана")
    letter = wb_report.DISCOUNTER_LETTER
    coinvest = wb_report.coinvest_by_day(rows)
    orders = []
    for platform, title in wb_report.PLATFORMS:
        o_rows = wb_report.build_orders_daily(funnel_rows, days, cost_fn, ads_by_day, coinvest, today, ads_known, platform, after_commission=after_k)
        o_total = wb_report.orders_total_row(o_rows)
        o_notes = [f"Источник: воронка продаж WB по товарам (sales-funnel/products, день заказа МСК) — {funnel_note}. Площадка — первая буква артикула: "
                   + (f"`{letter}` — Дискаунтер, остальное — Standard" if letter else "буква «Дискаунтера» в профиле кабинета не задана, площадки не делятся")
                   + f"; лист «{title}». Строки и колонки — те же функции, что у отдельной книги WB (scripts/report_wb_month.py).",
                   (after_note if after_k is not None else "Выручка и Маржа пусты: множитель владельца в профиле кабинета не задан") + ".",
                   f"Себестоимость = Σ orderCount × СС по дню заказа, базовый артикул. {cost_note} Без СС {o_total['no_cost_qty']} из {o_total['orders_qty']} заказов "
                   f"({o_total['no_cost_sum']:,.0f} с НДС); позиций по источникам: " + ", ".join(f"{k} {v}" for k, v in sorted(o_total["cost_sources"].items())) + ".",
                   "Соинвест, % — рабочее правило: (Σ retailPriceWithDisc − Σ retailAmount) / Σ retailPriceWithDisc по строкам «Продажа» отчёта реализации "
                   "за день (saleDt МСК); точного правила владельца нет (WB-6 §2).",
                   "Реклама — только на общем листе (списания по кампаниям на площадки не делятся); ДРР — от суммы заказов с НДС.",
                   f"Жёлтым — дни моложе {wb_report.YOUNG_DAYS} суток: воронка пересматривает день задним числом."]
        orders.append((title, o_rows, o_total, o_notes))
    return orders, funnel_note


def write_wb_orders_sheets(wb, orders):
    """Листы заказов WB в книгу wb — report_wb_month._write_orders_sheet (тот же лист, что в отдельной книге WB), в конец книги.
    Лист с таким названием уже есть — заменяется, не двоится (как лист выкупов). Возвращает названия записанных листов."""
    titles = []
    for title, o_rows, o_total, o_notes in orders:
        if title in wb.sheetnames:
            del wb[title]
        wb_report._write_orders_sheet(wb, title, o_rows, o_total, o_notes)
        titles.append(title)
    return titles


def write_wb_sheet(wb, month, daily, total, notes, position=1):
    """Лист «WB - <месяц>» в книгу wb (openpyxl.Workbook) на позицию position. Колонки — report_wb_month.COLS."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fmt = {"money": "#,##0.00", "pct": "0.0%", "int": "#,##0"}
    bold, young_fill, head_fill = Font(bold=True), PatternFill("solid", fgColor="FFF2CC"), PatternFill("solid", fgColor="D9E1F2")
    title = f"WB - {wb_report.MONTHS[int(month[5:7]) - 1]}"
    if title in wb.sheetnames:
        del wb[title]
    ws = wb.create_sheet(title, index=position)
    ws["A1"] = "Выкупы WB"; ws["A1"].font = bold
    ws["E2"] = "Вычитается НДС за возмещение из выручки! (как на листе владельца)"
    cols = wb_report.COLS
    for j, (head, _k, _f) in enumerate(cols, 1):
        if head:
            c = ws.cell(row=3, column=j, value=head); c.font = bold; c.fill = head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    for i, r in enumerate(list(daily) + [total], 4):
        r = dict(r)
        r["note"] = "; ".join(x for x in ("моложе двух суток — строки отчёта доезжают" if r.get("young") else "",
                                          f"без СС {r['no_cost_positions']} из {r['positions']} позиций" if r.get("no_cost_positions") else "",
                                          "реклама не запрашивалась" if r.get("ads") is None else "") if x)
        for j, (head, k, f) in enumerate(cols, 1):
            if not head:
                continue
            v = r.get(k)
            if k == "date" and v != "Итого":
                v = date.fromisoformat(v)
            elif isinstance(v, Decimal):
                v = float(v)
            c = ws.cell(row=i, column=j, value=v)
            if f:
                c.number_format = fmt[f]
            if k == "date" and r["date"] != "Итого":
                c.number_format = "DD.MM.YYYY"
            if r.get("young"):
                c.fill = young_fill
            if r["date"] == "Итого":
                c.font = bold
    base = 4 + len(daily) + 2
    for n, line in enumerate(notes):
        ws.cell(row=base + n, column=1, value=line)
    ws.freeze_panes = "B4"; ws.row_dimensions[3].height = 48
    for j in range(1, len(cols) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 15 if j > 1 else 16
    ws.column_dimensions[get_column_letter(len(cols))].width = 60
    return ws


def add_wb_sheet(book_path, month, date_to, sb=None, snapshot=None, today=None):
    """Добавить лист WB и листы заказов WB (п. 95) в книгу на диске. Возвращает (итог, сводка). Книга перезаписывается на месте."""
    import openpyxl
    daily, total, notes, info = build_wb_daily(month, date_to, sb=sb, snapshot=snapshot, today=today)
    wb = openpyxl.load_workbook(book_path)
    write_wb_sheet(wb, month, daily, total, notes)
    orders = info.pop("orders", [])
    try:
        info["orders_sheets"] = write_wb_orders_sheets(wb, orders)
    except Exception as error:  # noqa: BLE001 — недописанные листы заказов из книги убираются, лист выкупов остаётся
        for title, _rows, _total, _notes in orders:
            if title in wb.sheetnames:
                del wb[title]
        info["orders_sheets"] = []
        info["funnel_note"] = f"листы «Заказы WB» не записаны: {type(error).__name__}: {str(error)[:160]}"
    print(f"листы заказов WB: {', '.join(info['orders_sheets']) or 'нет'}; {info.get('funnel_note', '')}")
    wb.save(book_path)
    return total, info


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--month", required=True)
    ap.add_argument("--date-to", required=True)
    args = ap.parse_args(argv)
    total, info = add_wb_sheet(args.book, args.month, args.date_to)
    ads = "—" if total["ads"] is None else f"{total['ads']:,.2f}"
    print(f"лист WB добавлен в {args.book}: строк отчёта {info['rows']}, дней {info['days']}; итого оборот {total['turnover']:,.2f}, комиссия {total['commission']:,.2f}, "
          f"логистика {total['logistics']:,.2f}, реклама {ads}, фин. рез. {total['fin_result']:,.2f}; обращений к WB API 0; db_writes = 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
