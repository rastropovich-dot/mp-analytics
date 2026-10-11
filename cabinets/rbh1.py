"""Кабинет РБХ-1 (РБХ): Ozon — Попова, WB — ИП Рафикова; бренд Beautyhome.me. Своя база Supabase, свой сервис Render, группа Telegram —
общая РБХ (кабинет — в первой строке сообщения). Заполняется по мере подключения (первая задача RBH, §5, поправка 09-28): «не задано» —
None или пусто, не ноль (ответ владельца 09-30: константы листов — «не задано», не ноль). РБХ продаёт КОСМЕТИКУ: ювелирные словари KARATOV
(категории, металл, площадки F/S/T, бренд по букве артикула) сюда не копируются — «не задано»; свои категории — отдельной задачей позже."""
from decimal import Decimal  # noqa: F401  — для будущих констант владельца

CODE = "rbh1"
DISPLAY_NAME = "РБХ-1"
GROUP = "РБХ"
OZON_LEGAL_NAME = "Попова"                       # юрлицо кабинета Ozon
WB_LEGAL_NAME = "ИП Рафикова"                      # юрлицо кабинета WB
SHOP = "РБХ-1"                                     # колонка «Магазин» книги = имя кабинета (вопрос о SHOP снят владельцем 09-30)
SUPABASE_HOST = None                                # хост проекта Supabase кабинета — впишет сессия по §5.1; до этого запуск невозможен
DATA_DIR = "data/rbh1"
LOGS_DIR = "logs/rbh1"
PROJECT_START = None                                # первая запись — окно 60 дней назад по умолчанию (§5.4)
BOOK_MONTH_FROM = None                              # первый месяц книги «Фин рез» — после первой записи (finrez_nightly.sh без него не идёт)

BRAND_DEFAULT = "Beautyhome.me"
BRAND_BY_LETTER = {}
BRAND_NORMALIZE = {}
DISCOUNTER_LETTER = None
OZON_PLATFORMS = ()
METALS = ()
# словари товара — косметика, свои категории позже (справочник владельца ~/Downloads/Косметика_—_коды_брендов_и_предметов_WB_1.xlsx)
OWNER_CATEGORIES = ()
TYPE_TO_CATEGORY = {}
KIND_TO_CATEGORY = {}
NAME_RULES = ()
CATEGORY_BY_SUBJECT = {}

OWNER_SHEET = {}
OWNER_AFTER_COMMISSION = ()
OWNER_ORDERS_AFTER_COMMISSION_WB = None
OWNER_COEF = {}
OVERHEAD_PER_DAY = {}
COST_RATE_K = {}                                    # k по пробам и видам (поправка СС по курсу 1С): косметика — не задано, СС снимка без поправки

COST_SNAPSHOT_DATE = None
COST_SNAPSHOT_FILE = None
CATEGORY_TREE_FILE = "ozon_products/category_tree.json"     # относительно DATA_DIR
CATALOG_FILE = "ozon_products/catalog_latest.json"
OWNER_PIVOT_FILES = {"ozon_buyouts": "owner_finrez_ozon_buyouts_pivot.xlsx",   # форма книги «Фин рез» — одна на кабинеты
                     "wb_buyouts": "owner_finrez_wb_buyouts_pivot.xlsx",
                     "orders": "owner_finrez_orders_pivot.xlsx"}
REPORT_PREFIX = "rbh1_"

ALERT_TITLE = "MP Analytics Alerts · РБХ-1"
RENDER = {"owner": "tea-d7n5qs1f9bms738bfvug", "pipeline": None, "alert": None}   # сервисы создаст владелец (§5.5)
LAUNCHD_LABEL = "com.mp-analytics.rbh1.finrez-nightly"
GOLDEN_SKU = None
