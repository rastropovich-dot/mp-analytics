# MP Analytics — Project Context for Claude Code

> Единый источник правды для всех AI-сессий.
> Сверен с кодом, данными и Render: **2026-09-03**.

---

## 0. Состояние на сегодня

- **Работает:** дневной сбор Ozon CPC/CPO, загрузчики WB и Ozon, витрины KPI. Ночь идёт ~00:22–03:00 UTC.
- **Выключено:** recovery-воркер (`--skip-recovery`) — пока не починен его выбор дат; Telegram/Excel/Decision — флагами, давно.
- **Сломано и не чинится прямо сейчас:** органика не считается с 2026-05-21; остатки WB не обновляются с 2026-07-14; история заказов WB осыпается каждую ночь.
- **⚠️ Открытый риск:** `--skip-organic` есть в коде, но **в команде Render его может не быть** — сверяться с панелью. Без него шаг органики держится единственным условием `ozon_downstream_allowed` и при сборе без хвоста запустится сам, записав завышенную примерно на четверть органику (не собран Selected CPO) и сняв блокер в утреннем алерте.
- **В очереди:** починка выбора дат у воркера → возврат recovery; `flag=1` для заказов WB и восстановление истории; миграция остатков WB на новый эндпоинт; сбор Selected CPO → пересчёт органики.
- **Открытые вопросы:** чему равен реальный дневной потолок Ozon (плавает); превращать ли «молчунов» в частичный статус.

---

## 1. Проект и инфраструктура

Управленческий инструмент для маркетплейсов Ozon и WB. Auto-actions выключены.

БД — Supabase (PostgreSQL). Хостинг — Render (cron job). Уведомления — Telegram.
Репозиторий — github.com/rastropovich-dot/mp-analytics, ветка `main`.

**Render деплоит автоматически по коммиту в `main`** (`autoDeploy: yes`) — любой push
уезжает в прод до ближайшей ночи.

### Живая команда `mp-analytics` (crn-d7n7nan7f7vs73fk70kg, `15 0 * * *`)

```
python3 run_daily_pipeline.py --skip-recovery --skip-telegram --skip-excel --skip-decision --ozon-campaign-selection smart_recent_active --ozon-recent-activity-days 7 --ozon-dormant-probe-size 100 --ozon-max-daily-cpc-units 1200 --ozon-allow-staged-cpc-partial
```

`mp-analytics-telegram-report` (crn-d7t5ed1j2pic73aiqmog, `30 7 * * *`): `python3 alerts_telegram.py`.

> **Сверять команду с Render, а не с этим файлом.** Файл отстаёт: он не менялся,
> когда 2026-09-03 добавили `--skip-recovery`. Живое значение — в панели Render
> или через `GET /v1/services/{id}` → `serviceDetails.envSpecificDetails.startCommand`.

---

## 2. Hard Constraints

```
- НЕ запускать full pipeline без подтверждения
- НЕ трогать /api/client/statistics/json без подтверждения
- НЕ отправлять Telegram без --dry-run
- НЕ менять Render без подтверждения
- НЕ включать auto-actions
- НЕ делать full daily rerun по partial dates
- НЕ начинать recovery с batch 0, если есть partial progress
- НЕ retry при daily_quota_exhausted в том же quota window
- stop on first 429, не retry storm
- db_writes = 0 в dry-run/plan-only
```

---

## 3. Как писать в этот файл

За неделю он разошёлся с реальностью в дюжине мест, а читает его каждая сессия.

1. Утверждение несёт, **чем и когда проверено**: «602 кампании (статусные строки, 2026-09-03)», а не «~1206 units».
2. Непроверенное — **помечать гипотезой**, не подавать фактом.
3. Опровергнутое **не удалять, а помечать опровергнутым с причиной** — иначе версия вернётся через месяц. Образец: отозванный §2 в `docs/ozon_cpc_backfill_plan.md`.
4. Длиннее нескольких строк — в `docs/`, здесь ссылка. **§2 не трогать.**

---

## 4. Ozon Performance API

```
Одновременных выгрузок с аккаунта: 1   ← КРИТИЧНО (подтверждено retryable_429)
Максимум кампаний в отчёте:        10  (DEFAULT_CAMPAIGN_BATCH_SIZE)
```

**Модель квоты** (по ledger, 2026-09-03): одна кампания в submit = одна выгрузка,
проверяется **только при submit**; опросы и скачивания квоту **не расходуют**
(`campaign_units = 0`). Расход = `sum(campaign_units)` по `request_kind='submit'` в
`ozon_performance_statistics_json_usage`. Статусная таблица занижает вдвое: ключ
`(load_date, target_date, mode)` затирает `*_this_run` предыдущего прогона.

**Дневной потолок плавающий, величина неизвестна.** Опровергнуто 2026-09-03:
версия «потолок 2000, половину выбирает кто-то ещё». По всем 19 ночам с
`daily_quota_exhausted` отказ приходил на **610** единицах (08-20) и на **1190**
(07-07), а **1601** за 09-01 прошла без отказа.
**Гипотеза (не проверена):** `min(активные_кампании × 240, 2000)` — при 2–3 активных
потолок ~600, при 4–5 ~1000–1200. Нужен подсчёт активных кампаний по датам.
`STATS_DAILY_CAMPAIGN_LIMIT = 2000` — константа в коде, не факт про Ozon.

```
CPC:          POST /api/client/statistics/json                          ← в лимите
CPO «Все»:    GET  /api/client/statistics/all_sku_promo/orders/generate ← НЕ в лимите
Selected CPO: POST /api/client/statistic/orders/generate ← singular!    ← НЕ в лимите
              kind: SEARCH_PROMO_ORGANISATION_ORDERS
```

`retryable_throttle` — ждём Retry-After, продолжаем. `daily_quota_exhausted` (body
«Превышен дневной лимит») — стоп до сброса окна. В ledger первый пишется как
`retryable_429` — два словаря на одно понятие.

**Не путать с 429 Seller API** (`api-seller.ozon.ru`, `"code": 8`): там частота
запросов в секунду, отказ транзиентный, повтор осмыслен — `loaders/http_retry.py`.
Правило §2 «stop on first 429» относится к Performance.

`smart_recent_active` — единственный режим в рантайме: **554–642 кампании** на свежих
датах (статусные строки, 08-28…09-02), на августовских планах до 2654 — размер зависит
от даты, а не от режима. Прежние «~1675 / ~1206 units» устарели.

---

## 5. Recovery Worker — сейчас ВЫКЛЮЧЕН

`scripts/ozon_performance_recovery_worker.py`. Выключен `--skip-recovery` с 2026-09-03,
пока не починен выбор дат.

```
pre  — гейт is_yesterday_cpc_loaded: идёт, ТОЛЬКО если вчера уже success,
       то есть всегда работает со СТАРЫМИ датами. Это НЕ подбор хвоста.
post — подбирает хвост ВЧЕРАШНЕЙ даты, затем уходит в исторический бэкфилл.
       --max-batches-per-run 26, --max-attempts 10, --wait-for-minutes 240
budget guard: used > 1500 — ТОЛЬКО pre. У post порог 1800 (2000 − reserve 200).
```
Опровергнуто 2026-09-03: «max 1 batch per run» и «guard при used > 1500» — верно
лишь для pre-фазы.

**Бэклог в этом файле не вести.** Он строится по статусным строкам и расходится с
данными: 30 дат против 28 у детектора, пересечение 11; 17 реальных дыр воркер не
видит, 19 нормальных качает зря. Прежний список из пяти дат был неверен: 05-24,
05-25, 05-27 по данным в порядке, у 05-27 пять статусных строк, а не «MISSING».
Актуальный список — только детектором: `python3 scripts/ozon_cpc_data_gap_report.py`.

`--ozon-recovery-current-day-only` (в коде есть, в команде Render НЕ включён) сужает
работу до хвоста вчерашней даты, без бэкфилла.

---

## 6. Известные поломки

| что | с какого | подробности |
|---|---|---|
| Органика не считается | 2026-05-21 | гейт требовал пустого исторического бэклога, чего не бывает. Снят в `34ea2e1`. Держать `--skip-organic`, пока не собран Selected CPO: иначе завышение ~на четверть |
| Остатки WB | 2026-07-14 | эндпоинт отключён, 404 с текстом. `wb_data_integrity.md` §3 |
| Заказы WB осыпаются | постоянно | `flag: 0` + upsert-замещение, недобор ~37 % (≈14 600 заказов). `wb_data_integrity.md` §1 |
| Восемь загрузчиков молча отдают неполное с кодом 0 | давно | `loader_partial_data_contract.md` |
| Selected CPO не собирается | 2026-05-21 | `ozon_selected_cpo_daily_rollout.md` |

### Флаги Selected CPO

```
ENABLE_OZON_SELECTED_CPO_DAILY        = true   ← в переменных Render
APPROVE_OZON_SELECTED_CPO_DAILY_WRITE = false
```
`ENABLE` = `true` как минимум с **2026-08-28** (дальше лог Render не хранится). Точную
дату включения установить не удалось; в git не включали — выставлен вручную в панели.
При `APPROVE = false` шаг читает source-таблицу, в Ozon не ходит и не пишет (два
заслона в коде). Переключает только владелец.

---

## 7. Telegram Gate

Ozon incomplete если: нет KPI/organic/ad rows ИЛИ partial_ads/pending_429/pending_quota.
WB живёт отдельно и из-за Ozon partial не отключается. Блокер
`ozon_daily_sku_organic_missing` горит **каждое утро с 2026-05-22**: таблица пуста, а
проверка — чистая функция от неё. Это не сигнал о конкретной ночи.

```bash
python alerts_telegram.py --dry-run --no-send --skip-snapshot --target-date YYYY-MM-DD
```

---

## 8. Golden Article

```
article:  F000283615    sku: 1300079194
product:  Серьги золотые 585 с танцующими бриллиантами KARATOV
COGS:     32 963 ₽  ← пока hardcode, нужен DB seed
```

Эталон 2026-05-16: orders 2 / 221 646 ₽ | CPC 3 369,84 ₽ (подтверждено по
`marketplace_expenses`) | selected CPO 22 047,30 ₽ | organic 221 646 ₽ |
net estimate 36 161,71 ₽ | total_order_TACOS 7,64 % | cpc_order_TACOS 1,01 %.

Selected CPO — суммы **по этому SKU**, не по дате (за 05-20 по SKU 56 431,50 при
125 772,90 по всей дате):
```
05-12: 11 286.30  05-13: 55 325.00  05-14: 0.00  05-15: 32 995.00
05-16: 22 047.30  05-20: 56 431.50  05-21: 33 858.90
```

---

## 9. Открытые треки

🔴 1. **Проверить, что `--skip-organic` стоит в команде Render** — в коде он есть, см. §0.
🔴 2. **Выбор дат у воркера** — свести к одному источнику; отменяет прежний календарь бэкфилла.
🔴 3. **Dynamic budget** — `min(активные × 240, 2000)`: это объяснение отказов, а не улучшение.
🔴 4. **WB `flag=1`** — остановить осыпание, затем восстановить историю.
🟡 5. **Остатки WB** — на `POST /api/analytics/v1/stocks-report/wb-warehouses` (ключ подходит).
🟡 6. **Selected CPO** — собрать, затем пересчитать органику за 2026-05-22 … сегодня.
🟡 7. **Контракт неполноты** для восьми молчунов.
🟡 8. **article_unit_costs** — миграция не применена; seed F000283615 / 32963.
🟡 9. Excel OOM (фильтр 90 дней); decision layer (убрать `--skip-decision`).
🟢 10. Organic reporting split; второй golden SKU.

**Закрыто:** concurrent job check (`wait_for_statistics_job_slot` + `externallist` перед
каждым submit); usage ledger (таблица есть, 9 180 строк); «2026-05-27 without status»
(строк пять); Smart CPC v2 (уже 554–602).

---

## 10. Ключевые файлы и документы

```
run_daily_pipeline.py    alerts_telegram.py    loaders/http_retry.py
loaders/ozon_performance_ads_loader.py    scripts/ozon_performance_recovery_worker.py
scripts/ozon_cpc_data_gap_report.py    export_management_excel.py ← OOM risk
sql/20260518_create_article_unit_costs.sql ← НЕ применён

docs/wb_data_integrity.md               осыпание заказов WB, мёртвые остатки, как чинить
docs/loader_partial_data_contract.md    восемь молчунов (файлы и строки), контракт
docs/ozon_selected_cpo_daily_rollout.md раскатка Selected CPO, состояние флагов
docs/ozon_cpc_backfill_plan.md          §2 отозван — образец пометки опровергнутого
docs/kpi_rebuild_20260903_result.md     пересчёт KPI и что он вскрыл
docs/incident_2026-09-02_{snapshot,followups}.md   инцидент и очередь после него
docs/ozon_organic_sales.md              формула органики, order_sku vs promoted_sku
docs/ozon_performance_limits.md         ← содержит опровергнутую «половину лимита»
```
