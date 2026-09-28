# Отчёт рабочей сессии — RBH

Сюда RBH-сессия пишет результат по задаче из `docs/inbox_rbh.md`. Советник
читает этот файл сам. Требования к отчёту те же, что в `docs/outbox.md`:
что сделано с номерами коммитов; что не сделано и почему; числа, а не
оценки; любое расхождение объясняется в той же строке; «готово» — только
о проверенном; работа в ветке — с `git log main..HEAD`.

---

## 2026-09-28 (вечер), первая задача RBH — §1 инвентаризация сделана (только чтение: Supabase 0 запросов, Seller / Performance / WB / Telegram / Render — 0 обращений, db_writes 0); `.env` РБХ создан по просьбе владельца в окне; §2 в работе

Сессия RBH 1, worktree `~/mp-analytics-rbh`, ветка `multi-cabinet` = `origin/main` (`54168a4`), `git log origin/main..HEAD` пуст.
Прочитано: `CLAUDE.md`, `docs/how-we-work.md`, верхние блоки `docs/outbox.md` (передача сорок четвёртой: Ozon-сессия правит
`report_finrez.py`, `finrez_pivots.py`, `finrez_excel_check.py` на ветке `finrez-wb-acquiring`, не слито) и `docs/outbox_wb.md`
(двенадцатая WB слита `66f5388`; тринадцатая/четырнадцатая — `report_finrez_wb.py`, ветка `wb-fixes`). Соседям не писал.

Среда. В worktree не было `venv/`, `data/` владельца, `logs/`, `.env`: `venv` — симлинк на `~/mp-analytics/venv` (Python 3.9.6,
исключён локально через `.git/info/exclude`, в git не попадает; каталог соседа не менялся), тестов в наборе `discover -s tests`
**1 171**. Файлы `docs/inbox_rbh.md` / `docs/outbox_rbh.md` уже лежали в worktree untracked (создала предыдущая сессия
«Многокабинетность mp-analytics RBH», cwd `~/mp-analytics`, остановлена 17:16 UTC) — текст inbox отличался от присланного в двух
местах (§3 без упоминания четырнадцатой WB, §5.1 с «`/v1/…`»); перезаписан присланным дословно.

**`.env` РБХ** (просьба владельца в окне, 20:24 МСК): `~/mp-analytics-rbh/.env` создан из `.env.example` — 22 имени из шаблона +
`MP_CABINET=rbh`, секреты пустые, к каждой переменной комментарий, где взять значение; девять настроек сборщика рекламы
(`OZON_PERFORMANCE_STATE_BACKEND … CPC_BACKFILL_START_HHMM`) оставлены со значениями шаблона — они не секреты, а пустое значение
ломает `int()` при импорте `loaders/ozon_performance_ads_loader.py` (строки 59–73). Права 600, `git check-ignore` — да, открыт
`open -e`. Файл после записи не читался, значения в чат и отчёт не попадают.

### §1.1 — где читается окружение (файлы вне `tests/`, чтение `os.getenv` / `os.environ`)

| переменная | файлов | где именно | судьба |
|---|---:|---|---|
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | 38 | 16 `loaders/`, 13 корневых (`alerts_telegram`, `export_management_excel`, `report_*`, `reports_*`), 7 `scripts/`, 2 корневых `test_*.py` (живые пробы) | env; хост `SUPABASE_URL` — в профиль как ожидаемый ref |
| `SUPABASE_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | 3 | запасные имена в `wb_ads_loader.py:49`, `wb_ads_nm_loader.py:59`, `wb_sales_funnel_orders_loader.py:53` (`or`-цепочка) | env, оставить |
| `OZON_CLIENT_ID` / `OZON_API_KEY` | 18 / 17 | 12 `loaders/` (в т. ч. `ozon_finance_accrual.py:123`, `ozon_postings_report.py:30`), 3 `scripts/` (`load_ozon_realization_by_day`, `posting_api_parity`, `ozon_finance_migration_parity`), 3 корневых `test_*.py` | env |
| `OZON_PERFORMANCE_CLIENT_ID` / `_SECRET` | 3 | `ozon_performance_ads_loader.py:38–39`, две пробы `scripts/ozon_search_promo_*` | env |
| `OZON_PERFORMANCE_*` (настройки) | 3 | 28 имён — все в `ozon_performance_ads_loader.py`; в пробах по 3 | env; на Render у РБХ — те же значения, что у KARATOV |
| `WB_API_KEY` | 12 | 7 `loaders/wb_*`, 4 пробы `scripts/wb_*_probe*`/`wb_flag1_raw_capture`, `test_marketplaces_api.py` | env |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 7 / 5 | `alerts_telegram.py:15–16`, `run_daily_pipeline.py:16–17`, `ozon_performance_ads_loader.py:35–36`, `scripts/send_ozon_month_report.py:185`, `scripts/fetch_ozon_month_report.py:119` (только токен), `get_telegram_chat_id.py`, `test_telegram.py` | env; бот общий, `TELEGRAM_CHAT_ID` — своя группа |
| `APP_TIMEZONE` | 14 | везде с умолчанием `Europe/Moscow`; ещё 4 литерала `ZoneInfo("Europe/Moscow")` (`alerts_telegram.py:948`, `report_management.py:265, 415`) и параметр запроса WB `"timezone": "Europe/Moscow"` (`wb_sales_funnel_orders_loader.py:163`) | env, остаётся |
| `ENABLE_OZON_SELECTED_CPO_DAILY`, `APPROVE_…_WRITE` | 1 | `ozon_performance_ads_loader.py` | env (Render) |
| `RENDER_API_KEY` | 2 | `scripts/fetch_render_logs.py:23–34` (**запасной путь `~/mp-analytics/.env`, строка 24 — у РБХ читал бы чужой файл**), `scripts/fetch_ozon_month_report.py` | env; запасной путь убрать |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | 4 | `report_7_days`, `report_60_days`, `report_management`, `test_connection` — decision layer выключен | env, пусто |
| `FINREZ_COPY_DIR`, `FINREZ_NO_STATUS`, `FINREZ_MONTH_FROM` | 1 | `scripts/finrez_nightly.sh` | env launchd; `FINREZ_MONTH_FROM=2026-04` — граница проекта KARATOV, у РБХ своя |
| прочие 30 имён (`OZON_POSTING_*`, `OZON_STOCKS_*`, `SKU_DECISION_*`, `WB_SALES_FUNNEL_DAYS_BACK`, `EXCEL_EXPORT_LOOKBACK_DAYS` …) | по 1 | настройки с умолчаниями в коде | env, не задаются |
| `MP_CABINET` | 0 | нигде — вводится в §2 | — |

`load_dotenv`: **92 файла, 92 вызова** вне `tests/` (+1 в `tests/test_000_no_network.py`); упоминаний слова вместе с импортами — 184
(«183» из задачи — это упоминания, не вызовы). Формы: 44 × `load_dotenv()` (файл `.env` ищется от cwd), 40 × `load_dotenv(os.path.join(ROOT,
".env"))` (от `__file__`), 3 × то же через `dirname(dirname(__file__))`, 2 × `load_dotenv(".env")`. Для кабинета-каталога обе формы
дают свой `.env`, если процесс запущен из корня своего worktree; `python-dotenv` уже заданные переменные не перезаписывает — на Render
переменные сервиса главнее файла.

**Общей фабрики клиента Supabase нет.** `create_client(` — **38 мест в 38 файлах**: 30 на уровне модуля (клиент создаётся при импорте:
16 `loaders/`, `alerts_telegram.py:29`, `export_management_excel.py:17`, `report_7_days`, `report_60_days`, `report_management`,
`reports_daily_marketplace_kpi`, `reports_daily_sku_kpi`, 6 `reports_*`, `test_connection.py`, `test_ozon_stock_one_product.py`) и 8 в
функциях (`wb_ads_loader._client`, `wb_ads_nm_loader`, `wb_sales_report_loader`, `scripts/load_article_unit_costs.supabase_client`,
`load_ozon_realization_by_day.supabase_client`, `measure_buyouts_history_vs_accrual`, `ozon_posting_status_log`,
`rewrite_buyouts_20260331`). Ещё **9 файлов берут готовый клиент импортом из загрузчика** (`from loaders.ozon_fbo_orders_loader import
supabase` и т. п.): `run_daily_pipeline.py` (2 места), `scripts/finrez_nightly_status.py`, `ozon_catalog_topup_step.py`,
`ozon_cpc_data_gap_report.py`, `ozon_product_catalog.py`, `reclassify_advertising_other.py`, `report_finrez.py`, `stale_keys_deep_check.py`,
`wb_remeasure_analyze.py`. Прямых обращений к PostgREST через `requests` (`rest/v1`) — 0 файлов. Следствие для §2: guard ставится
перед каждым из 38 `create_client` (в загрузчиках — до строки создания клиента при импорте, значит любой импортёр загрузчика
проверяется автоматически) и явно в точках входа.

### §1.2 — где зашит кабинет KARATOV (grep: `KARATOV`, `Топаз`, `ГОЛДСТАРТ`, `t-`, даты снимков, константы владельца)

| файл:строка | что это | куда уедет |
|---|---|---|
| `scripts/report_finrez.py:285` | `SHOP = "KARATOV"` — колонка «Магазин» в «Данных заказы» | профиль `SHOP` |
| `scripts/report_finrez_wb.py:69` | `SHOP = "KARATOV"` | профиль `SHOP` |
| `scripts/report_finrez_wb.py:70, 74–77, 89` | `DISCOUNTER_LETTER = "t"`, `BRAND_TOPAZ`, `BRAND_BY_LETTER`, `BRAND_DEFAULT = "KARATOV"`, `BRAND_NORMALIZE`, `BRAND_LABELS` | профиль (правило бренда по букве) |
| `scripts/report_finrez_wb.py:80, 82–86` | `OWNER_CATEGORIES`, `CATEGORY_BY_SUBJECT` — словарь категорий владельца (ювелирные предметы) | профиль (словарь) — **вопрос владельцу**, ниже |
| `scripts/report_finrez_wb.py:18, 158–160, 926` | docstring и примечание, записываемое в книгу («t → Топаз, иначе KARATOV») | :926 — f-строка от профиля; docstring остаётся |
| `scripts/report_finrez.py:19, 1623` | то же для Ozon-части («T — Топаз, иначе KARATOV») | :1623 — от профиля |
| `scripts/report_finrez.py:63–64` | `OWNER_COEF` (выкуп 0,84 / 0,86; комиссия 0,23 / 0,52 / 0,43) — коэффициенты листа владельца | профиль |
| `scripts/report_finrez.py:65–66, 1727` | `CATALOG_FILE = data/ozon_products/catalog_latest.json`, `OUT_DIR`, имя `finrez_{from}_{to}.xlsx` | профиль (путь снимка; префикс имён файлов) |
| `scripts/ozon_product_catalog.py:89` | `brand_of`: `"Топаз" if first == "T" else "KARATOV"` | профиль |
| `scripts/ozon_product_catalog.py:41–42, 48–75` | `OUT_DIR`, `OWNER_CATEGORIES`, `TYPE_TO_CATEGORY`, `KIND_TO_CATEGORY`, `NAME_RULES` | путь — профиль; словари — **вопрос владельцу** |
| `scripts/ozon_catalog_topup_step.py:32` | `TREE_FILE = …/category_tree.json` | профиль (путь дерева) |
| `scripts/report_ozon_month.py:72, 84, 88, 295, 308, 1568` | `SNAP = "2026-05-20"`, `OVERHEAD_PER_DAY ozon 311 527,00 с 09-01`, `COST_INDEX 1,150 с 09-01`, `PLATFORMS F/S/T → Основная/Селект/Дискаунтер`, `METALS` (серебро/золото по названию), имя `ozon_{month}.xlsx` | профиль (снимок, накладные, индекс, площадки по букве, префикс); `METALS` — словарь, вопрос |
| `scripts/report_wb_month.py:65, 71, 369, 370, 833` | `SNAP`, `OVERHEAD_PER_DAY wb 74 002,00`, `OWNER_ORDERS_AFTER_COMMISSION 0,58`, `DISCOUNTER_LETTER "t"`, примечание «КОЮЗ Топаз» | профиль |
| `scripts/ozon_orders_forecast.py:45, 50–53` | `OWNER` (0,65 / 0,59 / 0,024), `OWNER_AFTER_COMMISSION` (с 09-01: 0,53 / 0,53 / 0,90; до — 0,59) | профиль (множители владельца) |
| `scripts/reconcile_manual_report_ozon.py:44, 115` | `SNAP = "2026-05-20"`; умолчание листа «Ozon - сентябрь» | `SNAP` — профиль; лист — аргумент, остаётся |
| `scripts/load_article_unit_costs.py:7–9, 219` | умолчание `--file data/cost_20260520.xlsx` | профиль (имя файла снимка СС) |
| `scripts/cogs_by_units_from_accrual.py:53` | умолчание `--snapshot 2026-05-20` | профиль |
| `reports_ozon_ad_diagnostic_rule.py:24–47, 52–54` | `KNOWN_CAMPAIGN_HINTS` (артикул F000283615, кампании 24375352 / 24375331), `KNOWN_SKU_COGS {"1300079194": 29390.06}` — золотой SKU | профиль (диагностический скрипт, не в пайплайне) |
| `scripts/finrez_pivots.py:49–53` | образцы сводных владельца `data/owner_finrez_{ozon_buyouts,wb_buyouts,orders}_pivot.xlsx` | профиль (пути; форма — «второго кабинета») |
| `alerts_telegram.py:1130, 1193` | «📊 MP Analytics Alerts», описание парсера | профиль `ALERT_TITLE`; первая строка «кабинет: …» — §2 |
| `run_daily_pipeline.py:262, 347, 587` | «Пайплайн MP Analytics упал», «Запуск ежедневного пайплайна MP Analytics» | остаётся (имя продукта); первая строка «кабинет: …» — §2 |
| `scripts/fetch_render_logs.py:16, 24`; `scripts/fetch_ozon_month_report.py:35–36` | `OWNER = tea-d7n5qs1f9bms738bfvug` (организация Render, общая), `crn-d7t5ed1j2pic73aiqmog` (алерт KARATOV); запасное чтение `~/mp-analytics/.env` | сервисы — профиль `RENDER`; запасной путь — убрать |
| `scripts/finrez_nightly.sh:22, 26–27` | `FINREZ_MONTH_FROM` по умолчанию `2026-04`; имена `finrez_…`, `ozon_…` | префикс — профиль; граница — env |
| `ops/launchd/com.mp-analytics.finrez-nightly.plist:9, 13, 16, 37, 39` | label `com.mp-analytics.finrez-nightly`, пути `/Users/mihaileliseev/mp-analytics/…` | остаётся у KARATOV; для РБХ — свой plist (label и WorkingDirectory) |
| `run_daily.sh:2` | `cd /Users/mihaileliseev/mp-analytics` — старый локальный запуск | остаётся (не используется Render) |
| `export_management_excel.py:461` | `management_report.xlsx` | префикс профиля |
| `scripts/report_ozon_month.py:554`, `report_wb_month.py:560`, `book_wb_sheet.py:66` | заголовки листов «Ozon - <месяц>», «WB - <месяц>» | остаётся (форма листа) |
| `sql/20260924_*.sql` (3 файла, комментарии) | «KARATOV / КОЮЗ Топаз» в комментариях колонок | остаётся (комментарии схемы) |
| `scripts/accrual_types_owner_check.py:33` | `docs/owner_manual_report_instruction.md` — справочник владельца KARATOV | остаётся (аналитика) |
| `TELEGRAM_CHAT_ID` (5 файлов, §1.1) | id группы KARATOV — только в окружении, в коде нет | env |

Не нашлось: «ГОЛДСТАРТ» — в коде 0 вхождений (только `docs/outbox_wb.md`, ответ `seller-info`); «t-» — единственное `kind == "t"` в
`loaders/wb_sales_report_loader.py:124` — это тип поля «текст», не артикул. Файлы данных, на которые ссылается код
(`data/cost_20260520.xlsx`, `data/owner_finrez_*_pivot.xlsx`, `data/ozon_products/category_tree.json`, `catalog_latest.json`) в этом
worktree отсутствуют (вне git), в `~/mp-analytics` есть все шесть — у РБХ будут свои копии в своём `data/`.

### §1.3 — точки входа (куда встанет `cabinet.assert_env()`)

- **Render (сверять с панелью, не с этим файлом):** `mp-analytics` (`15 0 * * *`): `python3 run_daily_pipeline.py --skip-recovery
  --skip-organic --skip-telegram --skip-excel --skip-decision --ozon-campaign-selection smart_recent_active --ozon-recent-activity-days 7
  --ozon-dormant-probe-size 100 --ozon-max-daily-cpc-units 1200 --ozon-allow-staged-cpc-partial`; `mp-analytics-telegram-report`
  (`30 7 * * *`): `python3 alerts_telegram.py`.
- **`run_daily_pipeline.py`** — 25 шагов в `build_steps` (строки 207–253), каждый отдельным процессом `python3 <файл>` с cwd = корень
  (без `PYTHONPATH`; загрузчики поэтому импортируют соседей через `except ImportError: import http_retry` — корня в `sys.path` у них нет,
  `import cabinet` там нужен с запасным путём): 9 `loaders/` (`wb_orders`, `wb_sales_funnel_orders`, `wb_sales`, `wb_sales_report`,
  `wb_ads`, `wb_stocks`, `ozon_finance_transactions`, `ozon_expenses`, `ozon_performance_ads`, `ozon_sku_total_analytics`, `ozon_stocks`),
  7 `scripts/` (`ozon_performance_recovery_worker` ×2, `wb_buyout_cohort_step`, `ozon_fbs_orders_step`, `ozon_fbo_orders_step`,
  `ozon_catalog_topup_step`, `ozon_posting_status_log`, `ozon_buyout_units_step`), 6 корневых (`reports_ozon_sku_organic`,
  `reports_daily_sku_kpi`, `reports_daily_marketplace_kpi`, `reports_sku_decision_daily_input`, `export_management_excel`, `alerts_telegram`).
- **`alerts_telegram.py`** — клиент при импорте (строка 29); дочерний процесс `scripts/send_ozon_month_report.py` (книга месяца, читает
  `report_ozon_month`, `book_wb_sheet`).
- **Скрипты с `--apply` — 29:** `loaders/wb_buyout_cohort.py`, `run_daily_pipeline.py`, `scripts/backfill_ozon_buyouts_coinvest`,
  `backfill_ozon_orders_buyer`, `delete_stale_buyout_keys`, `delete_stale_kpi_keys`, `fix_recovery_ledger_units`, `load_article_unit_costs`,
  `load_ozon_realization_by_day`, `merge_telegram_export` (файлы, не база), `ozon_catalog_topup_step`, `ozon_posting_status_log`,
  `ozon_product_catalog`, `raw_retention` (файлы), `rebuild_ozon_expenses_from_raw`, `rebuild_ozon_orders_history`,
  `reclassify_advertising_other`, `rewrite_buyouts_20260331`, `rewrite_buyouts_history`, `seed_buyout_units`, `seed_ozon_accrual_daily_types`,
  `seed_wb_sales_report_subjects`, `stale_keys_deep_check`, `wb_ads_backfill`, `wb_ads_nm_backfill`, `wb_buyout_cohort_step`,
  `wb_buyouts_rebuild_from_report`, `wb_funnel_products_backfill`, `wb_sales_report_backfill`.
- **Пишут в базу без `--apply`** (флаги `--write` / `--approve-*` / без флага): 17 загрузчиков, 6 `reports_*`, `scripts/ozon_cpo_one_day`,
  `ozon_selected_cpo_one_day`, `ozon_performance_recovery_worker`, `finrez_nightly_status --write`, `wb_orders_repair`,
  `wb_orders_history_restore`, `ozon_search_promo_orders_controlled_submit`.
- **Книга и ночь:** `scripts/report_finrez.py` (клиент — импортом из `ozon_fbo_orders_loader`, строка 1571), `scripts/finrez_nightly.sh`
  (зовёт `report_finrez.py` и `finrez_nightly_status.py --write`), `ops/launchd/…plist` (не установлен), `run_daily.sh` (старый).
- **Живые пробы в корне** с клиентом при импорте: `test_connection.py`, `test_ozon_stock_one_product.py` (и `test_marketplaces_api.py`,
  `test_telegram.py`, `test_ozon_realization.py` — без Supabase).

Итого файлов под guard в §2: все 38 с `create_client` + 9 берущих клиент импортом + скрипты с `--apply` без клиента + `run_daily_pipeline.py`
(явно, до первого шага) + `finrez_nightly.sh`; точное число — в отчёте §2 (считается тестом, а не руками).

### Вопросы владельцу (задача не закончена)

1. **Хост Supabase на Render KARATOV.** Профиль `karatov` получит `SUPABASE_HOST = pkrsrwjrlurlfpdyixei.supabase.co` (из `.env`
   `~/mp-analytics`, только хост). После мержа guard встанет в начале ночи и алерта: если в переменных сервисов Render `SUPABASE_URL`
   указывает на другой хост (иная запись того же проекта), первая ночь остановится на первой строке. Проверить по API я не могу без
   чтения секретов KARATOV (§6) — просьба взглянуть в панели: `mp-analytics` и `mp-analytics-telegram-report` → Environment →
   `SUPABASE_URL` содержит `pkrsrwjrlurlfpdyixei.supabase.co`? Одно слово «да» — и риск снят.
2. **Словари товара.** Категории («кольца … броши»), металл по названию, площадки Ozon по первой букве артикула (F / S / T) и бренд по
   букве — это KARATOV. РБХ — тоже ювелирный кабинет с теми же правилами артикулов, или у него свои? От ответа зависит, копируются ли
   словари в `cabinets/rbh.py` или там пусто с пометкой «не задано».
3. **Константы листов владельца для РБХ** (множители 0,53 / 0,90 / 0,58, накладные 311 527 и 74 002 в день, индекс СС 1,150, снимок СС
   1С) — у РБХ пока нет ничего: в профиле будут пустые значения, справочные колонки книг для РБХ — «не задано», не ноль.
4. **§3 требует прогона `report_finrez.py --check` за KARATOV** (сравнить с `main`). Из этого worktree он читает базу KARATOV и, для дней
   моложе двух суток, Seller API KARATOV; секретов KARATOV у сессии нет и читать их §6 запрещает. Предлагаю: прогон делает процесс с
   `load_dotenv` на `~/mp-analytics/.env` (значения в чат и отчёт не попадают), Seller API отключить ключами сборки (файлы сырья на диске /
   без живого ответа) — либо прогон делает владелец в `~/mp-analytics` на слитом коде. Нужно решение до §3.
