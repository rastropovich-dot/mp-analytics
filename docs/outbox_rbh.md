# Отчёт рабочей сессии — RBH

Сюда RBH-сессия пишет результат по задаче из `docs/inbox_rbh.md`. Советник
читает этот файл сам. Требования к отчёту те же, что в `docs/outbox.md`:
что сделано с номерами коммитов; что не сделано и почему; числа, а не
оценки; любое расхождение объясняется в той же строке; «готово» — только
о проверенном; работа в ветке — с `git log main..HEAD`.

---
## 2026-09-30, полные ответы владельца вписаны в inbox; общий `.env` (Telegram) создан; **план §5 на два кабинета `rbh1` / `rbh2`** — ниже, код по нему — после мержа §4 и по слову; главный факт для плана: `sql/` не собирает базу с нуля — 11 объектов кода созданы в панели до появления `sql/`, нужен базовый снимок схемы KARATOV

Сделано сейчас (db_writes 0, обращений к API 0): раздел ответов в `docs/inbox_rbh.md` заменён полной строкой владельца (девять пунктов); общий
`~/mp-analytics-rbh/.env` создан как пустой шаблон только с `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (комментарии, права 600, в git не попадает,
открыт `open -e`) — `cabinet.assert_env` читает его первым, файл кабинета поверх, пустые строки кабинета его не затирают (тест есть). WB-проверки не
делаются до ключей (п. 7); наблюдение за файлами снято. Профили `rbh1` / `rbh2` по п. 2, 3, 5 — уже в `21e1490`.

### Факты, на которых стоит план (измерено в репозитории, не по памяти)

- **`sql/` — 43 файла: 42 датированных + 1 без даты** (`ozon_statistics_json_usage_last_24h.sql` — вьюха видимости трафика Performance, в
  порядок «по датам» не встаёт). Они создают **27 таблиц, 1 вьюху (`wb_funnel_products_latest`), 1 функцию** (`ozon_statistics_json_usage_quota_windows`).
- **11 объектов, которые читает и пишет код, `sql/` не создаёт:** `marketplace_orders`, `marketplace_buyouts`, `marketplace_expenses`,
  `daily_sku_kpi`, `daily_marketplace_kpi`, `stock_daily`, `sku_catalog`, `marketplaces`, `intraday_snapshots`, `marketplace_orders_analytics` и вьюха
  `v_ozon_orders_daily_by_schema` — созданы в панели Supabase до первого файла (`20260505`); пять из них файлы `sql/` только ALTER-ят
  (`marketplace_orders`, `marketplace_buyouts`, `daily_sku_kpi`, `daily_marketplace_kpi`, `stock_daily`). Значит «применить 43 файла к пустому проекту и
  получить схему KARATOV» невозможно — задача §5.2 в исходной формулировке не выполнима без **базового снимка**.
- Три файла не идемпотентны (`20260531_…unique`, `20260602_…pkey` — пересборка первичного ключа, `20260924_orders_buyer_nullable`) — на пустой базе
  идут один раз в порядке дат, повторно не применяются; остальные 39 — `if not exists`.
- Миграции KARATOV применялись руками через MCP (CLAUDE.md, пять записей); в `requirements.txt` нет драйвера Postgres (только `supabase`, `requests`,
  `python-dotenv`), PostgREST DDL не выполняет → единственный канал для схемы из кода — **Supabase Management API `POST /v1/projects/{ref}/database/query`**
  (персональный токен с правом `database_write`; endpoint помечен experimental — это тот же канал, которым пользуется MCP; проверить первым же шагом на
  пустом `rbh1`).
- `render.yaml` в репозитории нет; сервисы KARATOV созданы в панели. По спеке Blueprint Render: «если добавить в файл имя существующего сервиса, Render
  применит к нему конфигурацию из файла» — KARATOV можно описать без пересоздания, но первый sync обязан быть пустым по действию (конфигурация в файле
  = живой, сверенной по API до применения). `envVarGroups` в Blueprint есть; форму привязки группы к сервису (`fromGroup`) в двух чтениях спеки не
  нашёл — сверить при написании файла, запасной вариант — переменные кабинета в самом сервисе с `sync: false`.
- Справочник владельца для категорий косметики — `~/Downloads/Косметика_—_коды_брендов_и_предметов_WB_1.xlsx` (п. 2): не разбирался, только место в плане.

### План §5 — два кабинета, каждый шаг по слову

Общий порядок: §3 (после мержа четырнадцатой WB, «влить main» по слову; там же контрольный `--check` KARATOV по п. 4 — один раз, Seller API выключен)
→ §4 (прямая проверка хоста Render по п. 1, мерж `multi-cabinet` по слову) → §5. Внутри §5 — сначала `rbh1` целиком (5.1 → 5.5), затем `rbh2` тем же
чек-листом; шаги, не зависящие друг от друга (создание двух проектов Supabase, двух групп переменных), владелец может делать парой.

**5.1 Доступы (частично сделано).** Ozon `rbh1` и `rbh2` — проверены 09-29 (Seller 200, Performance 200). Осталось: WB — когда впишет ключи (одно
`seller-info` + категории токена локально из payload, готово); Telegram — `getMe` + `getUpdates` для общего `.env` (2 обращения); Supabase — `assert_env`
после того, как ref каждого проекта впишу в `cabinets/rbh1.py` / `rbh2.py` (хост — не секрет).

**5.2а (новый шаг) Базовый снимок схемы KARATOV.** `scripts/db_schema_snapshot.py --cabinet karatov` — одно чтение схемы `public` через Management API
с `read_only: true` (таблицы, колонки с типами / умолчаниями / nullability, PK / unique / check / FK, индексы, вьюхи через `pg_get_viewdef`, функции через
`pg_get_functiondef`, последовательности, RLS и гранты для `service_role`) → `data/schema/karatov_<дата>.json` (вне git) и сгенерированный
`sql/00000000_baseline_public_schema.sql` — только объекты, которых датированные файлы не создают (11 известных + всё, что покажет дрейф: колонки,
добавленные в панели мимо `sql/`, индексы, строки-справочники — `marketplaces` читает только `test_connection.py`, но FK на неё возможны). Это чтение базы
KARATOV — по п. 9 сверх п. 1 и п. 4, **нужно отдельное слово**. Проверка снимка: базовый файл + 42 датированных на пустой `rbh1` → снимок `rbh1` → diff с
KARATOV **0** (таблица объектов в отчёт); только после этого — `rbh2`. Файл без даты переименовать в датированный (по дате его коммита в git) или вынести
из `sql/` в `docs/` — решение при снимке.

**5.2б Версии схемы и одна команда на все кабинеты.** Таблица `schema_migrations(filename primary key, sha256, applied_at, applied_by)` в каждой базе
(создаёт сама команда). `scripts/db_migrate.py --cabinet karatov|rbh1|rbh2|all [--apply]`: план = датированные файлы `sql/` минус применённые; без `--apply`
только печатает; с `--apply` — каждый файл одной транзакцией через Management API, затем строка в `schema_migrations`; у применённого файла изменился
sha256 — стоп с именем файла. Токен Management API — `SUPABASE_ACCESS_TOKEN` в общем `.env` на Mac; сервисам Render он не нужен. Для KARATOV таблицу
засеять 42 строками (+ базовый маркер) по слову, после снимка. **Guard версий** — `cabinet.assert_schema(client)`: одно чтение `schema_migrations` через
PostgREST, сравнение с файлами репозитория; отстаёт → в пайплайне `SystemExit` сразу после строки «кабинет: …» с текстом «база кабинета РБХ-1 отстаёт от
кода: не применены 2 файла: …»; в алерте — не выход, а первая строка сообщения «⛔ база отстаёт от кода: …» и отправка без блоков данных (иначе о
поломке никто не узнает). Правило после мержа: `db_migrate --cabinet all --apply` по слову до ближайшей ночи; забыли — ночь РБХ остановится в 00:20 с
понятной строкой, а не упадёт на отсутствующей колонке в середине.

**5.3 Сухие прогоны** (по 2 дня, `--dry-run`, обращения и 429 считать, db_writes 0), по кабинету: WB — заказы, воронка, продажи, отчёт реализации,
реклама, остатки; Ozon — FBS / FBO (`/v3`, `/v4`), финоперации, начисления, Performance (только список кампаний и 1 выгрузка — квота организаций РБХ
своя, но 2 000 на организацию действует и там). Таблица «шаг → обращений / 429 / строк» в отчёт.

**5.4 Первая запись и история** — окно 60 дней назад по умолчанию (владелец может изменить); Performance — по квоте, несколько ночей; каждый шаг по
слову и с планом по числам.

**5.5 Render.** `render.yaml` в корне описывает шесть cron-сервисов: два KARATOV (`mp-analytics` `15 0 * * *`, `mp-analytics-telegram-report` `30 7 * * *`
— конфигурация списана с живой по API до записи файла, sync без изменений) и четыре РБХ: `mp-analytics-rbh1` (`20 0 * * *`), `mp-analytics-rbh1-telegram-report`
(`35 7 * * *`), `mp-analytics-rbh2` (`25 0 * * *`), `mp-analytics-rbh2-telegram-report` (`40 7 * * *`) — сдвиг на 5 минут, чтобы три ночи не стартовали в одну
секунду и утренние сообщения шли по порядку (сдвиг — предложение, владелец может оставить 00:15 всем). Команда старта РБХ = команда KARATOV
(`--skip-recovery --skip-organic --skip-telegram --skip-excel --skip-decision …`), `MP_CABINET=rbh1` / `rbh2` — в переменных сервиса; секреты кабинета —
группа окружения `rbh1` / `rbh2` (`sync: false`, значения владелец вводит в Dashboard при создании Blueprint); переменные KARATOV остаются в его сервисах,
как сейчас. `autoDeployTrigger: commit`, ветка `main`, регион и план — как у KARATOV (снять по API). Blueprint-экземпляр из репозитория создаёт владелец
после мержа `render.yaml` в `main`; первая ночь каждого кабинета — по чек-листу Ozon / WB-сессий (лог Render, SQL по базе кабинета, алерт).

**5.6 Ночная книга на Mac.** Предложение: обёртка `scripts/finrez_nightly_all.sh` — последовательно `MP_CABINET=karatov`, `rbh1`, `rbh2` →
`scripts/finrez_nightly.sh` (логи в `LOGS_DIR` кабинета, итог — в `pipeline_runtime_state` базы этого кабинета, строка «книга Фин рез» в его алерте);
кабинет без `SUPABASE_HOST` в профиле или без строк в базе пропускается вслух. Один plist (существующий, 06:00 МСК) переводится на обёртку; KARATOV ~30 мин,
РБХ поменьше — всё до утреннего алерта РБХ 07:35 UTC не успеет, но книги РБХ и не должны ждать алерта. Запасной вариант — три plist со сдвигом
06:00 / 06:40 / 07:00. `FINREZ_MONTH_FROM` по умолчанию `2026-04` → из профиля (`PROJECT_START`), §3. Образцы сводных владельца (`data/owner_finrez_*_pivot.xlsx`)
предлагаю держать одним комплектом в общем `data/` — форма книги одна на кабинеты; решение в §3.

**5.7 Что создаёт владелец, по порядку, каждое — по слову:** (1) два проекта Supabase (`mp-analytics-rbh1`, `mp-analytics-rbh2`; регион — как у
KARATOV) → Project URL и `service_role` в `.env.rbh1` / `.env.rbh2`; персональный токен Management API (Account → Access Tokens, право на базу) →
`SUPABASE_ACCESS_TOKEN` в общий `.env`; (2) после моего слова о снимке и миграциях — группа Telegram РБХ: создать, добавить бота, `TELEGRAM_CHAT_ID` в
общий `.env`; (3) ключи WB в `.env.rbh1` / `.env.rbh2`; (4) после мержа `render.yaml` — Blueprint в Render из репозитория, ввод секретов групп `rbh1` /
`rbh2`; (5) слово на первую запись 60 дней и на первую ночь — по кабинету. Между шагами — мои проверки чтением и отчёт.

**Отдельной задачей позже:** категории косметики РБХ по справочнику `~/Downloads/Косметика_—_коды_брендов_и_предметов_WB_1.xlsx`.

### Ждёт

Мерж четырнадцатой WB → «влить main» → §3. `WB_API_KEY` в обоих файлах пуст, `TELEGRAM_*` в общем `.env` пусты. Слово на чтение схемы KARATOV (5.2а)
— когда дойдём до §5.

---

## 2026-09-30, ответы владельца получены и вписаны в inbox; применено в ветке: профили `rbh1` / `rbh2` — косметика, ювелирных словарей и констант листов нет («не задано», тест), `SHOP` = имя кабинета («РБХ-1» / «РБХ-2»); набор 1 189 OK; WB по-прежнему пуст; §3 — после мержа четырнадцатой WB и слова «влить main»

Что закрыто ответами: (1) хост Supabase на Render — косвенно да, прямая проверка через Render API (только хост `SUPABASE_URL`, обоих сервисов, и групп
переменных, если есть) — перед мержем §4; (2) ювелирные словари KARATOV в профили РБХ не копируются — в `cabinets/rbh1.py` / `rbh2.py` так и было
(`BRAND_BY_LETTER {}`, `OZON_PLATFORMS ()`, `DISCOUNTER_LETTER None`), теперь это записано в docstring профилей и закреплено тестом; свои категории —
отдельной задачей; (3) константы листов — «не задано», не ноль — так и есть (`OWNER_* ()`/`{}`/`None`, `COST_INDEX ()`, `OVERHEAD_PER_DAY {}`, тест);
(4) контрольный `--check` за KARATOV — один раз, чтение базы по `~/mp-analytics/.env` процессом, Seller API выключен, значения не печатаются и не
сохраняются, db_writes 0 — сделаю в §3; (5) `SHOP` — колонка «Магазин» = «РБХ-1» / «РБХ-2» (было «Beautyhome.me», бренд остался в `BRAND_DEFAULT`);
(6) общий `.env` в `~/mp-analytics-rbh` с Telegram заполняет владелец — пустые Telegram-строки в `.env.rbh1` / `.env.rbh2` его не затрут.
`origin/main` = `9eae801` (сорок четвёртая Ozon слита `7786499`, сорок пятая принята); четырнадцатая WB не слита — §3 не начат, ветка `multi-cabinet`
на `main` не перебазирована (это будет «влить main» по слову). `WB_API_KEY` в обоих файлах пуст на момент записи — проверка WB ждёт «WB вписан».

---

## 2026-09-29 16:51 UTC, проверка доступа Ozon для `rbh1` и `rbh2` (по слову владельца) — оба кабинета: Seller HTTP 200, Performance HTTP 200 (токен получен, срок 1800 с); обращений 4 (по 2 на кабинет), 429 — 0, db_writes 0; WB не проверялся — `WB_API_KEY` пуст в обоих файлах

| кабинет | Seller `POST /v2/warehouse/list` | склады FBS/rFBS | Performance `POST /api/client/token` |
|---|---|---|---|
| `rbh1` (Ozon: Попова) | 200 | 2, `has_next false`: «ИП Попова (Beautyhome)», «Склад Тест СПБ» | 200, токен получен, срок 1800 с, Bearer |
| `rbh2` (Ozon: Малимон) | 200 | 2, `has_next false`: «Склад», «RealFBS» | 200, токен получен, срок 1800 с, Bearer |

Отдельный скрипт без импорта модулей проекта, значения читаются `dotenv_values(".env.<кабинет>")` — только из файла кабинета, окружение оболочки не
участвует; ключи и токены не печатались и не сохранялись. v2 вместо v1 — по спеке `/v1/warehouse/list` отключён с 7 апреля 2026 (запись выше). Имя
склада `rbh1` подтверждает юрлицо Ozon-стороны (Попова); у `rbh2` имена складов юрлица не называют — что ключ именно Малимон, этим ответом не доказано
(доказал бы `seller-info` Ozon — третье обращение, в слове его нет). Отчётов Performance не заказывалось. **WB `seller-info` и категории токена — когда
владелец впишет `WB_API_KEY`:** на 16:51 UTC пусто в `.env.rbh1` и `.env.rbh2` (проверено «задан/пуст», значение не читалось).

---

## 2026-09-29 16:48 UTC, проверка доступа Ozon для `rbh1` (по слову владельца) — Seller HTTP 200, 2 склада FBS/rFBS, первый «ИП Попова (Beautyhome)»; Performance HTTP 200, токен получен, срок 1800 с; обращений 2, 429 — 0, db_writes 0

Отдельный скрипт без импорта модулей проекта (`load_dotenv(".env.rbh1", override=True)`), ключи и токен не печатались, токен нигде не сохранён.
Seller — `POST /v2/warehouse/list` (`limit 200`) вместо `/v1/warehouse/list`: по описанию метода в `spec/ozon-seller.json` v1 «устаревает и будет
отключён 7 апреля 2026», замена — v2 с тем же смыслом (склады FBS и rFBS); ответ 200, складов 2, `has_next false`, первый — «ИП Попова (Beautyhome)»
(совпадает с Ozon-стороной профиля `rbh1` — Попова). Performance — `POST /api/client/token` (`client_credentials`, как в `ozon_performance_ads_loader.py:3319`):
200, `token_type Bearer`, `expires_in 1800`; отчётов не заказывалось, квота выгрузок не тронута. В `.env.rbh1` все четыре ключа Ozon заданы.
WB `seller-info`, Telegram `getMe` и Supabase для `rbh1` не проверялись — не было в слове.

---

## 2026-09-28 (ночь), поправка к дополнению учтена: профили РБХ — `rbh1` (Ozon Попова + WB ИП Рафикова) и `rbh2` (Ozon Малимон + WB ИП Плахов), «РБХ-1» / «РБХ-2», бренд Beautyhome.me; `.env.rbh1` / `.env.rbh2`; полный набор 1 189 OK (2 skipped)

Поправка пришла в двух редакциях (во второй стороны Ozon поменяны местами — в inbox записана вторая с пометкой). В ветке: `cabinets/malimon.py` →
`cabinets/rbh1.py`, `cabinets/popova.py` → `cabinets/rbh2.py` (git mv); в каждом профиле два новых имени — `OZON_LEGAL_NAME` и `WB_LEGAL_NAME`
(rbh1: «Попова» / «ИП Рафикова»; rbh2: «Малимон» / «ИП Плахов»; karatov: Ozon — `None`, не измерено; WB — «ООО «ГОЛДСТАРТ»» из seller-info
двенадцатой WB), `DISPLAY_NAME` «РБХ-1» / «РБХ-2», `BRAND_DEFAULT` и `SHOP` — «Beautyhome.me» (магазин = бренд — предположение, владелец поправит),
`DATA_DIR` / `LOGS_DIR` — `data/rbh1` … `logs/rbh2`, `REPORT_PREFIX` `rbh1_` / `rbh2_`, `ALERT_TITLE` «MP Analytics Alerts · РБХ-1» / «· РБХ-2».
`cabinet.KNOWN_CODES = ("karatov", "rbh1", "rbh2")`; схема профилей — 31 имя, одинаковая у трёх (тест). `tests/test_cabinet.py` переписан на новые коды
(неизвестный код в тесте — «rbh»); **полный набор 1 189 OK (skipped=2)**, `test_cabinet` 18 OK.

`.env`: файлы после создания не сохранялись (mtime = времени создания у обоих, 20:35:38 МСК), поэтому переименованы без потери правок:
`.env.popova` → `.env.rbh1`, `.env.malimon` → `.env.rbh2`; правились только строки-комментарии (`sed` по `^#`: заголовок, `MP_CABINET=`, имя сервиса
Render, `cabinets/<код>.py`, подписи Ozon-стороны «кабинет продавца Ozon «…»» и WB-стороны «кабинет WB «ИП …»»), строк без `#` изменено 0, значения не
читались; оба снова открыты `open -e`. Первый прогон переименования не выполнился (zsh не разбивает `$pair` по пробелам — `test -f` смотрел несуществующее
имя), файлы остались нетронутыми; второй прогон — явными аргументами.

---
## 2026-09-28 (ночь), первая задача RBH — §2 сделан в ветке `multi-cabinet`: `cabinet.py` + три профиля (`karatov`, `malimon`, `popova`), guard по хосту Supabase в 75 точках входа и в `finrez_nightly.sh`, первая строка пайплайна и алерта — «кабинет: …»; полный набор **1 189 OK (2 skipped)**; db_writes 0, обращений к API 0; §3 ждёт мержей соседей и слова «влить main?»

Дополнение 09-28 учтено: профилей РБХ два — `malimon` и `popova` (пара Ozon + WB у каждого, своя база, свой сервис Render, одна группа
Telegram — кабинет в первой строке сообщения); `.env.<кабинет>` догружается поверх `.env`; `DATA_DIR` / `LOGS_DIR` у karatov — `data` / `logs`,
у РБХ — `data/<кабинет>` / `logs/<кабинет>`. Шаблон `.env` удалён; созданы `.env.malimon` и `.env.popova` (по 22 имени из `.env.example`, секреты
пустые, комментарий к каждой переменной, права 600), оба открыты `open -e`; `.gitignore` дополнен `.env.*` (`.env.example` остаётся в git).
Значения не читались и в чат не попадают.

### Что в ветке

```
afedaf8 RBH inbox: addendum 09-28 — two RBH profiles (malimon, popova), .env.<cabinet> loaded over .env, per-cabinet data/logs dirs and Supabase ref; .gitignore: .env.* ignored, .env.example kept
f55e9f1 First RBH task received in inbox (multi-cabinet profiles, guard, RBH cabinet); outbox: §1 inventory — env readers, 38 create_client sites and no shared factory, KARATOV hardcodes table, entry points; .env template for RBH created outside git; db_writes 0
(коммит §2 — следующий, см. ниже)
 83 files changed, 722 insertions(+)
```

**`cabinet.py`** (корень): `cabinet_code()` — `MP_CABINET` → `karatov` по умолчанию, регистр не важен, неизвестный код — `SystemExit` со списком
известных; `profile(code)` — модуль `cabinets/<код>.py`; `assert_env()` — (1) общий `<корень>/.env` без перезаписи (то же, что `load_dotenv()` в модулях;
guard не зависит от того, успел ли модуль его вызвать — часть скриптов грузит `.env` только через импорт загрузчика), (2) `<корень>/.env.<код>`
поверх окружения (значение файла кабинета главнее общего `.env` и оболочки; **пустая строка файла кабинета ничего не затирает** — общие Telegram-переменные
можно держать в `.env`; у karatov файла нет — шаг пропускается; на Render файлов нет — переменные сервиса), (3) хост `SUPABASE_URL` против `SUPABASE_HOST`
профиля: не совпал / не задан / у профиля хоста ещё нет — `SystemExit` с внятной строкой, код 1, **до чтения ключей и создания клиента**. Хост в зоне
`.invalid` (RFC 2606, никогда не резолвится) guard пропускает — это заглушка `tests/test_000_no_network.py`, иначе 30 модулей с клиентом при импорте не
импортировались бы в наборе без сети. Ещё: `banner()` → «кабинет: <имя>», `data_dir()` / `logs_dir()` / `data_path()` — пути кабинета для §3.

**Профили** (`cabinets/`, 29 одинаковых имён в каждом — тест на равенство схем): `CODE`, `DISPLAY_NAME`, `GROUP`, `SHOP`, **`SUPABASE_HOST`**
(karatov — `pkrsrwjrlurlfpdyixei.supabase.co`, взят только хост из `SUPABASE_URL` соседнего `.env`; у `malimon` / `popova` — `None`, впишется по §5.1 —
до этого их запуск невозможен по построению), `DATA_DIR`, `LOGS_DIR`, `PROJECT_START`, правило бренда и площадки по букве (`BRAND_DEFAULT`,
`BRAND_BY_LETTER`, `BRAND_NORMALIZE`, `DISCOUNTER_LETTER`, `OZON_PLATFORMS`), константы листов владельца (`OWNER_SHEET`, `OWNER_AFTER_COMMISSION`,
`OWNER_ORDERS_AFTER_COMMISSION_WB`, `OWNER_COEF`, `OVERHEAD_PER_DAY`, `COST_INDEX`), файлы данных относительно `DATA_DIR` (`COST_SNAPSHOT_DATE`,
`COST_SNAPSHOT_FILE`, `CATEGORY_TREE_FILE`, `CATALOG_FILE`, `OWNER_PIVOT_FILES`), `REPORT_PREFIX` (karatov — пусто: имена книг не меняются; РБХ —
`malimon_` / `popova_`), `ALERT_TITLE`, `RENDER` (owner общий на организацию; сервисы РБХ — `None`), `LAUNCHD_LABEL`, `GOLDEN_SKU`. Значения karatov
— ровно те, что зашиты в коде (таблица §1.2, файл и строка у каждого); **код на профиль ещё не переведён — это §3**. У РБХ всё «не задано» (`None` /
пусто), `SHOP` = «Малимон» / «Попова» — предположение, владелец поправит; `DISPLAY_NAME` = «РБХ Малимон» / «РБХ Попова».

**Guard в точках входа — 75 файлов**, вставлен скриптом по правилу (после module-level `load_dotenv(…)`; нет его — после `sys.path.insert(0, …)`; нет —
перед первым `import loaders/scripts`; нет — после `ROOT = …` с добавлением `sys.path.insert`), список и причина у каждого — `/private/tmp/claude-501/-Users-mihaileliseev-mp-analytics-rbh/a3c1d0bd-f259-45ee-a473-226ceed3fcdb/scratchpad/guard_targets.txt`
(75 строк): 38 файлов с `create_client` (в 16 загрузчиках — с запасным путём импорта, потому что пайплайн зовёт их `python3 loaders/<файл>.py` и корня в
`sys.path` там нет), 9 берущих клиент импортом, все скрипты с `--apply` и `__main__`, все шаги `build_steps`, пайплайн, алерт, книги (`report_finrez`,
`report_finrez_wb`, `report_ozon_month`, `report_wb_month`, `send_ozon_month_report`, `book_wb_sheet`). В `run_daily_pipeline.main` первая строка —
`print(cabinet.banner(CABINET))`, в `alerts_telegram.build_message` первая строка сообщения — `cabinet.banner(CABINET)` (у KARATOV это единственное видимое
изменение алерта: строка «кабинет: KARATOV» над «📊 MP Analytics Alerts»). `scripts/finrez_nightly.sh`: guard первой строкой лога, отказ — код 3 до сборки.
`tests/test_000_no_network.py`: в фальшивое окружение добавлен `MP_CABINET=karatov` — набор считает числа KARATOV, что бы ни стояло в оболочке или `.env`.
Файлы соседей задеты минимально (по 2 строки после `load_dotenv`): `report_finrez.py`, `report_finrez_wb.py`, `finrez_nightly.sh`, загрузчики WB — конфликт
при мерже их веток разрешается тривиально.

Цена: любая точка входа теперь требует правильного окружения даже для `--help` (guard стоит на импорте, до argparse) — так задумано: «SystemExit до создания
любого клиента».

### Проверено (команды с выводом; сеть не открывалась — guard выходит до клиента)

| сценарий | вывод | код |
|---|---|---|
| `MP_CABINET=karatov SUPABASE_URL=https://wrong-host.supabase.co python3 loaders/wb_orders_loader.py` (и `ozon_fbo_orders_loader.py` — запасной путь импорта) | «кабинет KARATOV: SUPABASE_URL указывает на wrong-host.supabase.co, а кабинет ждёт pkrsrwjrlurlfpdyixei.supabase.co — не тот .env или не тот MP_CABINET; клиент Supabase не создан» | 1 |
| `MP_CABINET=karatov SUPABASE_URL=https://pkrsrwjrlurlfpdyixei.supabase.co/` → `banner(assert_env())` | «кабинет: KARATOV» | 0 |
| без `MP_CABINET` и без `SUPABASE_URL` | «кабинет KARATOV: SUPABASE_URL не задан — заполните …/.env.karatov (или общий .env), на Render — переменные сервиса» | 1 |
| `MP_CABINET=malimon`, `.env.malimon` с пустыми значениями | «кабинет РБХ Малимон: SUPABASE_URL не задан — заполните …/.env.malimon …» | 1 |
| `MP_CABINET=malimon SUPABASE_URL=<база KARATOV>` | «кабинет РБХ Малимон: в cabinets/malimon.py не задан SUPABASE_HOST — … (окружение указывает на pkrsrwjrlurlfpdyixei.supabase.co)» | 1 |
| `MP_CABINET=rbh` | «MP_CABINET='rbh': неизвестный кабинет; известны: karatov, malimon, popova» | 1 |
| `run_daily_pipeline.py --help`, `alerts_telegram.py --dry-run --no-send --skip-snapshot`, `merge_telegram_export.py --help` с чужим хостом | та же строка guard, дальше не идут | 1 |

Тесты: `tests/test_cabinet.py` — 18 (профиль по умолчанию; выбор `malimon` / `popova` без учёта регистра; неизвестный код; равенство схем трёх
профилей; `data/logs` karatov как раньше; хост из URL в четырёх формах; guard — совпал / чужой (в строке оба хоста) / не задан / профиль без хоста /
профиль с хостом принимает только свою базу / `.invalid` для всех трёх; overlay — кабинет главнее `.env`, пустая строка не затирает, оболочка не
перезаписывается, karatov не читает `.env.malimon`; **точки входа** — список строится тем же правилом, что скрипт вставки (`create_client` / импорт клиента /
`--apply` + `__main__` / команды `build_steps` / названные), у каждого есть `cabinet.assert_env(` и он раньше `create_client(`; guard в `finrez_nightly.sh`
раньше сборки, баннер раньше «🚀 Запуск», баннер первой строкой `lines` алерта). Полный набор `venv/bin/python3 -m unittest discover -s tests`:
**Ran 1189, OK (skipped=2)**, попыток открыть сеть 0 (`logs/tests_rbh1_s2.out`). `py_compile` всех 76 изменённых и 5 новых файлов — чисто.

### Не сделано и почему

- **§3** (перенос значений в профили, тест «литералов KARATOV / Топаз вне `cabinets/` и `tests/` нет», проверка чисел против `main`) — по задаче ждёт
  мержей сорок четвёртой (Ozon, `report_finrez.py`) и четырнадцатой (WB, `report_finrez_wb.py`); на 22:10 UTC обе не слиты (`origin/main` = `54168a4`).
  Когда сольются — строка владельцу «влить main?».
- §4 отчёт-приёмка и мерж — после §3. §5 — после мержа, дважды (Малимон, Попова), каждый шаг по слову.

### Вопросы (задача не закончена) — к четырём из блока §1 добавились

5. `SHOP` для книг РБХ: «Малимон» / «Попова» или иное имя магазина? `DISPLAY_NAME` «РБХ Малимон» / «РБХ Попова» устраивает?
6. Общие переменные РБХ (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) можно положить один раз в общий `~/mp-analytics-rbh/.env` — пустые строки в
   `.env.malimon` / `.env.popova` их не затрут (проверено тестом). Если владельцу так удобнее — создать общий `.env` из двух строк по слову.

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
