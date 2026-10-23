"""Кабинет РБХ-2 (РБХ): Ozon — Малимон, WB — ИП Плахов; бренд Beautyhome.me. Своя база Supabase, свой сервис Render, группа Telegram —
общая РБХ (кабинет — в первой строке сообщения). Заполняется по мере подключения (первая задача RBH, §5, поправка 09-28): «не задано» —
None или пусто, не ноль (ответ владельца 09-30: константы листов — «не задано», не ноль). РБХ продаёт КОСМЕТИКУ: ювелирные словари KARATOV
(категории, металл, площадки F/S/T, бренд по букве артикула) сюда не копируются — «не задано»; свои категории — отдельной задачей позже."""
from decimal import Decimal  # noqa: F401  — для будущих констант владельца

CODE = "rbh2"
DISPLAY_NAME = "РБХ-2"
GROUP = "РБХ"
OZON_LEGAL_NAME = "Малимон"                       # юрлицо кабинета Ozon
WB_LEGAL_NAME = "ИП Плахов"                      # юрлицо кабинета WB
SHOP = "РБХ-2"                                     # колонка «Магазин» книги = имя кабинета (вопрос о SHOP снят владельцем 09-30)
SUPABASE_HOST = "rmeikipzaplqcglhwcue.supabase.co"  # проект mp-analytics-rbh2 (eu-central-1, создан владельцем 2026-10-08…10); cabinet.assert_env сверяет с окружением
DATA_DIR = "data/rbh2"
LOGS_DIR = "logs/rbh2"
PROJECT_START = None                                # первая запись — окно 60 дней назад по умолчанию (§5.4)
BOOK_MONTH_FROM = "2026-08"                              # первый месяц книги «Фин рез»: август 2026 — первый месяц с данными §5.4 (с 23.08)

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

COST_SNAPSHOT_DATE = "2026-10-15"                 # прайс владельца rbh_cost_20261015.xlsx (дата файла; владелец дату не назвал)
COST_SNAPSHOT_FILE = "../rbh_costs/rbh_cost_20261015.xlsx"   # относительно DATA_DIR: общий прайс на оба кабинета (data/rbh_costs, вне git)
COST_PRICE_FORMAT = "rbh"                          # формат файла для load_article_unit_costs.py: loaders/rbh_cost_price.py
COST_PRICE_VAT_RATE = Decimal("1.22")              # «С-ть до МП» — С НДС (слово 22.10): в unit_cost пишем цена ÷ 1,22, цена с НДС — в unit_cost_uniform
COST_SOURCE_NOTE = "прайс РБХ 15.10, с НДС ÷ 1,22"   # суффикс к источнику СС в листах и книгах: у РБХ один снимок, продажи раньше него — подпись объясняет
BOOK_BRAND_SOURCE = "price"          # бренд строк книг — из прайса (article_unit_costs.analytics_group, 42 бренда); карточки Ozon везде Beautyhome.me (решение советника 23.10)
BOOK_CATEGORY_SOURCE = "platform"      # категория строк книг — тип карточки Ozon (ozon_products.type_name) и предмет WB как есть (решение владельца 22.10)
CATEGORY_TREE_FILE = "ozon_products/category_tree.json"     # относительно DATA_DIR
CATALOG_FILE = "ozon_products/catalog_latest.json"
OWNER_PIVOT_FILES = {"ozon_buyouts": "owner_finrez_ozon_buyouts_pivot.xlsx",   # форма книги «Фин рез» — одна на кабинеты
                     "wb_buyouts": "owner_finrez_wb_buyouts_pivot.xlsx",
                     "orders": "owner_finrez_orders_pivot.xlsx"}
REPORT_PREFIX = "rbh2_"

ALERT_TITLE = "MP Analytics Alerts · РБХ-2"
RENDER = {"owner": "tea-d7n5qs1f9bms738bfvug", "pipeline": None, "alert": None}   # сервисы создаст владелец (§5.5)
LAUNCHD_LABEL = "com.mp-analytics.rbh2.finrez-nightly"
GOLDEN_SKU = None
