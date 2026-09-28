"""Кабинет РБХ «Малимон» (юрлицо Малимон: пара Ozon + WB; своя база Supabase, свой сервис Render, группа Telegram — общая РБХ).
Заполняется по мере подключения (первая задача RBH, §5): «не задано» — None или пусто, не ноль. Значения владельца
(множители, накладные, индекс СС, снимок 1С) появятся, когда владелец их даст; словари товара — по его ответу на вопрос §1."""
from decimal import Decimal  # noqa: F401  — для будущих констант владельца

CODE = "malimon"
DISPLAY_NAME = "РБХ Малимон"
GROUP = "РБХ"
SHOP = "Малимон"                                      # колонка «Магазин» книги — уточнить у владельца
SUPABASE_HOST = None                                # хост проекта Supabase кабинета — впишет сессия по §5.1; до этого запуск невозможен
DATA_DIR = "data/malimon"
LOGS_DIR = "logs/malimon"
PROJECT_START = None                                # первая запись — окно 60 дней назад по умолчанию (§5.4)

BRAND_DEFAULT = None
BRAND_BY_LETTER = {}
BRAND_NORMALIZE = {}
DISCOUNTER_LETTER = None
OZON_PLATFORMS = ()

OWNER_SHEET = {}
OWNER_AFTER_COMMISSION = ()
OWNER_ORDERS_AFTER_COMMISSION_WB = None
OWNER_COEF = {}
OVERHEAD_PER_DAY = {}
COST_INDEX = ()

COST_SNAPSHOT_DATE = None
COST_SNAPSHOT_FILE = None
CATEGORY_TREE_FILE = "ozon_products/category_tree.json"     # относительно DATA_DIR
CATALOG_FILE = "ozon_products/catalog_latest.json"
OWNER_PIVOT_FILES = {"ozon_buyouts": "owner_finrez_ozon_buyouts_pivot.xlsx",   # форма книги «Фин рез» — одна на кабинеты
                     "wb_buyouts": "owner_finrez_wb_buyouts_pivot.xlsx",
                     "orders": "owner_finrez_orders_pivot.xlsx"}
REPORT_PREFIX = "malimon_"

ALERT_TITLE = "MP Analytics Alerts · РБХ Малимон"
RENDER = {"owner": "tea-d7n5qs1f9bms738bfvug", "pipeline": None, "alert": None}   # сервисы создаст владелец (§5.5)
LAUNCHD_LABEL = "com.mp-analytics.malimon.finrez-nightly"
GOLDEN_SKU = None
