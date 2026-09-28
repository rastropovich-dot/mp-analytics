"""Кабинет KARATOV — профиль по умолчанию (MP_CABINET не задан). Значения — те, что до 2026-09-28 были зашиты в коде
(docs/outbox_rbh.md, §1.2: файл и строка у каждого); переезд кода на профиль — §3 первой задачи RBH."""
from decimal import Decimal

CODE = "karatov"
DISPLAY_NAME = "KARATOV"
GROUP = "KARATOV"                                   # чья группа Telegram / чей набор кабинетов
OZON_LEGAL_NAME = None                              # юрлицо кабинета Ozon — не измерено
WB_LEGAL_NAME = "ООО «ГОЛДСТАРТ»"                    # seller-info WB (двенадцатая WB, 2026-09-28)
SHOP = "KARATOV"                                    # колонка «Магазин» книги «Фин рез» (report_finrez.py:285, report_finrez_wb.py:69)
SUPABASE_HOST = "pkrsrwjrlurlfpdyixei.supabase.co"  # хост SUPABASE_URL (не секрет); cabinet.assert_env сверяет с окружением
DATA_DIR = "data"                                   # как было: data/ и logs/ в корне
LOGS_DIR = "logs"
PROJECT_START = "2026-03-28"                        # граница проекта (данных раньше нет); книга «Фин рез» — с 2026-04

# Бренд и площадка — по первой букве артикула (ozon_product_catalog.py:89, report_finrez_wb.py:70–77, report_ozon_month.py:295,
# report_wb_month.py:370). Буква сравнивается без регистра.
BRAND_DEFAULT = "KARATOV"
BRAND_BY_LETTER = {"T": "Топаз"}
BRAND_NORMALIZE = {"коюз топаз": "Топаз", "топаз": "Топаз", "karatov": "KARATOV"}   # бренд WB → ярлык, когда артикула нет
DISCOUNTER_LETTER = "T"                             # «Дискаунтер» на обеих площадках = артикулы t… / T…
OZON_PLATFORMS = (("F", "Основная"), ("S", "Селект"), ("T", "Дискаунтер"))

# Константы листов владельца (ozon_orders_forecast.py:45–53, report_wb_month.py:369, report_finrez.py:63–64).
OWNER_SHEET = {"buyout_rate": Decimal("0.65"), "after_commission": Decimal("0.59"), "other_rate": Decimal("0.024")}
OWNER_AFTER_COMMISSION = (
    ("2026-09-01", {"Основная": Decimal("0.53"), "Дискаунтер": Decimal("0.53"), "Селект": Decimal("0.90"), "*": Decimal("0.53")}),
    ("0001-01-01", {"*": Decimal("0.59")}),
)
OWNER_ORDERS_AFTER_COMMISSION_WB = Decimal("0.58")
OWNER_COEF = {"buyout": (("Ozon", Decimal("0.84")), ("WB", Decimal("0.86"))),
              "commission": (("Ozon (<300)", Decimal("0.23")), ("Ozon (>300)", Decimal("0.52")), ("WB", Decimal("0.43")))}
# Накладные в день и индекс себестоимости с датой действия (report_ozon_month.py:84, 88; report_wb_month.py:71).
OVERHEAD_PER_DAY = {"ozon": (("2026-09-01", Decimal("311527.00")),), "wb": (("2026-09-01", Decimal("74002.00")),)}
COST_INDEX = (("2026-09-01", Decimal("1.150")),)

# Файлы данных — имена относительно DATA_DIR (cabinet.data_path).
COST_SNAPSHOT_DATE = "2026-05-20"                   # снимок 1С (load_article_unit_costs.py:219, report_*_month.py SNAP)
COST_SNAPSHOT_FILE = "cost_20260520.xlsx"
CATEGORY_TREE_FILE = "ozon_products/category_tree.json"
CATALOG_FILE = "ozon_products/catalog_latest.json"
OWNER_PIVOT_FILES = {"ozon_buyouts": "owner_finrez_ozon_buyouts_pivot.xlsx",
                     "wb_buyouts": "owner_finrez_wb_buyouts_pivot.xlsx",
                     "orders": "owner_finrez_orders_pivot.xlsx"}
REPORT_PREFIX = ""                                  # имена книг не меняются: ozon_…, wb_…, finrez_…, management_report.xlsx

ALERT_TITLE = "MP Analytics Alerts"                 # alerts_telegram.py:1130; первая строка сообщения — cabinet.banner()
RENDER = {"owner": "tea-d7n5qs1f9bms738bfvug", "pipeline": "crn-d7n7nan7f7vs73fk70kg", "alert": "crn-d7t5ed1j2pic73aiqmog"}
LAUNCHD_LABEL = "com.mp-analytics.finrez-nightly"
GOLDEN_SKU = {"article": "F000283615", "sku": "1300079194", "cogs": Decimal("29390.06")}   # reports_ozon_ad_diagnostic_rule.py
