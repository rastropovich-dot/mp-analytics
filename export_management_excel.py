import os
from datetime import date, timedelta
from dotenv import load_dotenv
from supabase import create_client
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, BarChart, Reference

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_all(table, filters=None, order=None):
    all_rows = []
    start = 0
    page_size = 1000

    while True:
        query = supabase.table(table).select("*")

        if filters:
            for field, operator, value in filters:
                if operator == "eq":
                    query = query.eq(field, value)
                elif operator == "gte":
                    query = query.gte(field, value)
                elif operator == "gt":
                    query = query.gt(field, value)
                elif operator == "lte":
                    query = query.lte(field, value)

        if order:
            query = query.order(order)

        result = query.range(start, start + page_size - 1).execute()
        rows = result.data or []
        all_rows.extend(rows)

        if len(rows) < page_size:
            break

        start += page_size

    return all_rows


def add_table(ws, title, headers, rows, start_row):
    title_fill = PatternFill("solid", fgColor="EAF2F8")
    header_fill = PatternFill("solid", fgColor="D6EAF8")
    thin = Side(style="thin", color="D9E2EC")
    border = Border(bottom=thin)

    ws.cell(start_row, 1, title)
    ws.cell(start_row, 1).font = Font(bold=True, size=14)
    ws.cell(start_row, 1).fill = title_fill

    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(start_row + 2, col_idx, header)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center")

    for row_idx, row in enumerate(rows, start=start_row + 3):
        for col_idx, value in enumerate(row, start=1):
            cell = ws.cell(row_idx, col_idx, value)
            cell.border = border
            if isinstance(value, (int, float)):
                cell.number_format = '#,##0'

    return start_row + 3 + len(rows)


def style_sheet(ws):
    ws.freeze_panes = "A4"
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        ws.column_dimensions[letter].width = 18

    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="center")

    ws.sheet_view.showGridLines = False


def build_excel():
    today = date.today()
    date_from_14 = (today - timedelta(days=14)).isoformat()

    wb = Workbook()
    wb.remove(wb.active)

    wb_daily = fetch_all(
        "daily_marketplace_kpi",
        filters=[("marketplace_code", "eq", "wb"), ("kpi_date", "gte", date_from_14)],
        order="kpi_date"
    )

    ozon_realization = fetch_all(
        "daily_marketplace_kpi",
        filters=[
            ("marketplace_code", "eq", "ozon"),
            ("buyouts_qty", "gt", 0),
            ("buyouts_amount_seller", "gt", 0)
        ],
        order="kpi_date"
    )

    ozon_fbs = fetch_all(
        "marketplace_orders",
        filters=[("marketplace_code", "eq", "ozon"), ("order_date", "gte", date_from_14)],
        order="order_date"
    )

    daily_sku_kpi = fetch_all("daily_sku_kpi", order="kpi_date")

    ws = wb.create_sheet("Summary")
    ws["A1"] = "Управленческий отчет Marketplaces"
    ws["A1"].font = Font(bold=True, size=18)
    ws["A2"] = f"Дата формирования: {today.isoformat()}"

    wb_orders = sum(float(r.get("orders_qty") or 0) for r in wb_daily)
    wb_orders_amount = sum(float(r.get("orders_amount_seller") or 0) for r in wb_daily)
    wb_buyouts = sum(float(r.get("buyouts_qty") or 0) for r in wb_daily)
    wb_buyouts_amount = sum(float(r.get("buyouts_amount_seller") or 0) for r in wb_daily)

    ozon_real_qty = sum(float(r.get("buyouts_qty") or 0) for r in ozon_realization)
    ozon_real_amount = sum(float(r.get("buyouts_amount_seller") or 0) for r in ozon_realization)

    ozon_fbs_qty = sum(float(r.get("orders_qty") or 0) for r in ozon_fbs)
    ozon_fbs_amount = sum(float(r.get("orders_amount_seller") or 0) for r in ozon_fbs)

    summary_rows = [
        ["WB заказы, 14 дней", wb_orders],
        ["WB сумма заказов, 14 дней", wb_orders_amount],
        ["WB выкупы, 14 дней", wb_buyouts],
        ["WB сумма выкупов, 14 дней", wb_buyouts_amount],
        ["Ozon месячная реализация, шт", ozon_real_qty],
        ["Ozon месячная реализация, руб", ozon_real_amount],
        ["Ozon FBS заказы, 14 дней", ozon_fbs_qty],
        ["Ozon FBS сумма заказов, 14 дней", ozon_fbs_amount],
    ]

    add_table(ws, "Ключевые показатели", ["Метрика", "Значение"], summary_rows, 4)
    style_sheet(ws)

    ws_wb = wb.create_sheet("WB Daily")
    wb_rows = []
    for r in wb_daily:
        wb_rows.append([
            r.get("kpi_date"),
            float(r.get("orders_qty") or 0),
            float(r.get("orders_amount_seller") or 0),
            float(r.get("buyouts_qty") or 0),
            float(r.get("buyouts_amount_seller") or 0),
            float(r.get("buyout_rate") or 0),
        ])

    add_table(
        ws_wb,
        "WB: дневная динамика",
        ["Дата", "Заказы", "Сумма заказов", "Выкупы", "Сумма выкупов", "% выкупа"],
        wb_rows,
        1
    )
    style_sheet(ws_wb)

    if len(wb_rows) >= 2:
        chart = LineChart()
        chart.title = "WB: заказы и выкупы"
        chart.y_axis.title = "Шт"
        chart.x_axis.title = "Дата"
        data = Reference(ws_wb, min_col=2, max_col=4, min_row=3, max_row=3 + len(wb_rows))
        cats = Reference(ws_wb, min_col=1, min_row=4, max_row=3 + len(wb_rows))
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        ws_wb.add_chart(chart, "H3")

    ws_ozon = wb.create_sheet("Ozon Realization")
    ozon_rows = []
    for r in ozon_realization:
        ozon_rows.append([
            r.get("kpi_date"),
            float(r.get("buyouts_qty") or 0),
            float(r.get("buyouts_amount_seller") or 0),
            float(r.get("commission_amount") or 0),
        ])

    add_table(
        ws_ozon,
        "Ozon: месячная реализация",
        ["Дата отчета", "Выкупы/шт", "Сумма реализации", "Комиссии"],
        ozon_rows,
        1
    )
    style_sheet(ws_ozon)

    ws_fbs = wb.create_sheet("Ozon FBS")
    grouped_fbs = {}
    for r in ozon_fbs:
        d = r.get("order_date")
        if not d:
            continue
        if d not in grouped_fbs:
            grouped_fbs[d] = [d, 0, 0]
        grouped_fbs[d][1] += float(r.get("orders_qty") or 0)
        grouped_fbs[d][2] += float(r.get("orders_amount_seller") or 0)

    fbs_rows = [grouped_fbs[k] for k in sorted(grouped_fbs.keys())]

    add_table(
        ws_fbs,
        "Ozon FBS: дневные заказы",
        ["Дата", "Заказы FBS", "Сумма заказов FBS"],
        fbs_rows,
        1
    )
    style_sheet(ws_fbs)

    ws_raw = wb.create_sheet("Raw KPI")
    raw_rows = []
    for r in daily_sku_kpi:
        raw_rows.append([
            r.get("kpi_date"),
            r.get("marketplace_code"),
            r.get("marketplace_sku"),
            r.get("article"),
            r.get("product_name"),
            float(r.get("orders_qty") or 0),
            float(r.get("orders_amount_seller") or 0),
            float(r.get("buyouts_qty") or 0),
            float(r.get("buyouts_amount_seller") or 0),
            float(r.get("buyout_rate") or 0),
        ])

    add_table(
        ws_raw,
        "Raw SKU KPI",
        ["Дата", "MP", "SKU", "Артикул", "Название", "Заказы", "Сумма заказов", "Выкупы", "Сумма выкупов", "% выкупа"],
        raw_rows,
        1
    )
    style_sheet(ws_raw)

    filename = "management_report.xlsx"
    wb.save(filename)
    print(f"✅ Excel-отчет создан: {filename}")


if __name__ == "__main__":
    build_excel()
