# MP Analytics — Project Context for Claude Code

> Этот файл — единый источник правды для всех AI-сессий.
> Обновлять после каждой значимой сессии.
> Последнее обновление: 2026-05-23

---

## 1. Цель проекта

Управленческий инструмент для маркетплейсов Ozon и WB.

Каждый день система:
1. Собирает данные по заказам, выкупам, расходам, рекламе, остаткам
2. Проверяет полноту слоёв (completeness gate)
3. Не строит ложные отчёты по неполным данным
4. Считает organic / KPI / decision layer
5. Строит diagnostic-рекомендации по рекламе
6. Готовится к будущей автоматизации ставок/цен

**Auto-actions выключены.** Только отчёт + рекомендации + ручное подтверждение.

---

## 2. Инфраструктура

| Компонент | Детали |
|---|---|
| БД | Supabase (PostgreSQL) |
| Хостинг pipeline | Render (cron job) |
| Уведомления | Telegram |
| Репозиторий | github.com/rastropovich-dot/mp-analytics |
| Основная ветка | main |

### Render расписание

```
03:10 UTC (06:10 МСК) — daily pipeline
python3 run_daily_pipeline.py --skip-telegram --skip-excel --skip-decision

07:30 UTC (10:30 МСК) — Telegram report
python3 alerts_telegram.py
```

---

## 3. Hard Constraints (всегда соблюдать)

```
- НЕ запускать full pipeline без явного подтверждения
- НЕ трогать /api/client/statistics/json без подтверждения
- НЕ отправлять Telegram без --dry-run проверки
- НЕ менять Render schedule без подтверждения
- НЕ включать auto-actions
- НЕ запускать WB loaders отдельно
- НЕ писать в marketplace_expenses / ozon_daily_sku_ad_attribution без dry-run
- НЕ делать live API calls без plan-only проверки
- stop on first 429, не retry storm
- db_writes = 0 в любом dry-run/plan-only
```

---

## 4. Архитектура данных

### Основные таблицы

```
marketplace_orders          — заказы Ozon FBS/FBO + WB
marketplace_buyouts         — выкупы/реализация
marketplace_expenses        — расходы (комиссия, логистика, реклама)
stock_daily                 — остатки по складам
sku_catalog                 — каталог товаров

ozon_daily_sku_total_orders — аналитика продаж Ozon (Seller API)
ozon_daily_sku_ad_attribution — рекламная атрибуция по SKU
ozon_daily_sku_organic       — органические продажи
ozon_organic_reconciliation_issues — хвост органики

ozon_search_promo_selected_cpo_orders — selected CPO (SEARCH_PROMO)
ozon_product_identity                 — master identity layer

daily_sku_kpi               — KPI по SKU за день
daily_marketplace_kpi       — KPI по маркетплейсу за день
sku_decision_daily_input    — decision layer для автоматизации

pipeline_runtime_state      — состояние pipeline (jobs, cooldowns, progress)
ozon_performance_daily_load_status — статус загрузки Performance API

article_unit_costs          — себестоимость по артикулам (MIGRATION PENDING)
```

### Expense types в marketplace_expenses

```
advertising_clicks          — CPC реклама
advertising_order_5         — CPO "Все товары" 5%
advertising_order_10        — CPO "Все товары" 10%
advertising_order_selected_cpo — CPO "Выбранные товары"
commission                  — комиссия Ozon
logistics                   — логистика
other                       — прочее
```

---

## 5. Ozon Performance API — ключевые endpoints

### CPC (статистика по кампаниям)
```
POST /api/client/statistics/json
→ только для CPC и медийной рекламы
→ лимит: 2000 campaign units/day на уровне seller account
→ сброс: 00:00 UTC
```

### CPO "Все товары"
```
GET /api/client/statistics/all_sku_promo/orders/generate
→ organisation-level, без campaign filter
→ НЕ входит в лимит 2000
```

### Selected CPO "Выбранные товары" (НАЙДЕН!)
```
POST /api/client/statistic/orders/generate   ← singular "statistic"!
payload: {"from": "...", "to": "..."}        ← без campaignId
status:  GET /api/client/statistics/{UUID}
download: GET /api/client/statistics/report?UUID=...
kind: SEARCH_PROMO_ORGANISATION_ORDERS
→ НЕ входит в лимит 2000
→ organisation-level отчёт
→ исключать строку "Всего" при парсинге
```

### Campaign metadata
```
GET /api/client/campaign?advObjectType=SKU|BANNER|SEARCH_PROMO
→ НЕ входит в лимит 2000
```

---

## 6. Ozon Performance — статусы и recovery

### Статусы CPC
```
success          — все кампании загружены
partial_ads      — часть кампаний не загружена
pending_429      — остановлен из-за rate limit
pending_backfill — ждёт дозагрузки
```

### Recovery worker
```
scripts/ozon_performance_recovery_worker.py
→ запускается ПЕРВЫМ шагом в daily pipeline
→ находит partial/pending CPC даты
→ max 1 batch per run (по умолчанию)
→ budget guard: skip если today used > 1500 units
→ recovery budget = min(200, remaining - 200)
→ stop on first 429
→ write только с --write --approve-recovery-worker-write
```

### CPC backfill
```
python3 loaders/ozon_performance_ads_loader.py \
  --mode cpc-backfill \
  --date YYYY-MM-DD \
  --allow-recovery-worker-before-daily-status \
  --dry-run
```

---

## 7. Telegram completeness gate

Ozon считается incomplete если:
```
- нет daily_marketplace_kpi
- нет daily_sku_kpi
- нет organic rows
- нет ad rows
- ИЛИ: ozon_performance_daily_load_status показывает:
  - run_status = partial_ads
  - cpc_status in (pending_429, pending_backfill, pending_quota)
  - cpc_pending_campaigns > 0
```

При incomplete → placeholder вместо Ozon summary:
```
Ozon вчера: данные неполные, управленческий вывод не строим.
Причины: ozon_performance_partial_ads, ...
```

Safe preview:
```bash
python alerts_telegram.py --dry-run --no-send --skip-snapshot --target-date YYYY-MM-DD
```

---

## 8. Stock — текущий статус

```
stock_ok = 392
stock_out = 66
stock_from_identity_evidence = 44  ← fallback, не clean
real missing_stock = 67
unknown = 0
explainability = 100%
```

`stock_from_identity_evidence` — данные на момент identity loader run,
НЕ исторический снимок. Quality warning, aggressive actions не открывает.

---

## 9. Organic — текущий статус

За 2026-05-05:
```
total rows = 1076
clean = 993
issue rows = 83
unknown = 0
unreconciled_revenue = 342 750 ₽
```

Классификация хвоста:
```
missing_total_order_sku_absent = 55
order_vs_promoted_sku_mismatch = 9
possible_date_semantics = 9
ad_revenue_exceed_total = 7
promoted_sku_present_but_order_sku_absent = 3
```

Формула: `organic = total_orders_revenue - ad_attributed_revenue`
Union/associated — отсутствует в API, нужен XLSX из ЛК (не приоритет).

---

## 10. Golden Article — эталонный SKU

```
marketplace_code = ozon
article          = F000283615
marketplace_sku  = 1300079194
product_name     = Серьги золотые 585 с танцующими бриллиантами KARATOV
COGS             = 32 963 ₽/unit
currency         = RUB
```

### Эталонная дата: 2026-05-16

```
orders = 2 / 221 646 ₽
buyouts = 2 / 223 513 ₽
CPC spend = 3 369.84 ₽
selected CPO = 22 047.30 ₽  ← сходится с ЛК точно
organic_revenue = 221 646 ₽
decision = ready / ok / clean

net estimate = 36 161.71 ₽
total_order_TACOS = 7.64%
cpc_order_TACOS = 1.01%
selected_cpo_order_TACOS = 6.63%
buyout_TACOS = 11.37%
```

### CPC кампании

```
24375352 | TOP_PROMOTION       | primary   | CVR 0.73% | ROAS 49x | keep/cautious_increase
24375331 | SEARCH_AND_CATEGORY | secondary | CVR 0.33% | ROAS 51x | hold_watch
```

Вывод: CPC здоровый. Selected CPO — главное давление. Не резать CPC из-за selected CPO.

### Три уровня готовности даты

```
Data loaded:       orders/buyouts/expenses/ads/organic/KPI — можно смотреть
Diagnostic-ready:  + sku_decision_daily_input + data_quality ok — для rule engine
Cabinet-ready:     + selected CPO сверен + COGS из БД + нет partial_ads — эталон
```

---

## 11. COGS / article_unit_costs

```
sql/20260518_create_article_unit_costs.sql  ← MIGRATION NOT APPLIED YET
load_article_unit_costs(...)                ← в diagnostic rule
```

Lookup priority:
```
1. article_unit_costs (DB)
2. CLI --cogs
3. KNOWN_SKU_COGS (hardcode)
4. missing → cogs_missing = true
```

**Следующий шаг:** применить миграцию + seed F000283615 в Supabase SQL Editor.

Seed данные:
```sql
INSERT INTO article_unit_costs
  (marketplace_code, article, unit_cost, currency, cost_source, valid_from, comment)
VALUES
  ('ozon', 'F000283615', 32963, 'RUB', 'manual', '2026-01-01',
   'Initial known COGS for SKU 1300079194');
```

---

## 12. Ad Diagnostic Rule

```
reports_ozon_ad_diagnostic_rule.py
```

Использование:
```bash
python reports_ozon_ad_diagnostic_rule.py \
  --marketplace-code ozon \
  --sku 1300079194 \
  --date 2026-05-16 \
  --campaign-id 24375331 \
  --campaign-id 24375352 \
  --dry-run
```

Результаты: GREEN / YELLOW / RED
`live_action_allowed = false` всегда сейчас.

Batch top-20 показал:
```
GREEN = 0
YELLOW = 11
RED = 9
cogs_missing = 19  ← главный bottleneck
```

---

## 13. Открытые треки (приоритет по убыванию)

### 🔴 Критические

1. **Excel OOM** — pipeline падает без `--skip-excel`
   - Render Starter 512MB не хватает
   - Виновник: `export_management_excel.py` (36909 строк без фильтра)
   - Fix: ограничить данные последними 90 днями
   - Файл: `export_management_excel.py`

2. **article_unit_costs migration** — COGS нет в БД
   - Применить: `sql/20260518_create_article_unit_costs.sql` в Supabase
   - Seed: F000283615 / 32963 / RUB / 2026-01-01

### 🟡 Важные

3. **Selected CPO в daily pipeline** — загружается вручную
   - Loader готов: `fetch_search_promo_orders_csv()`
   - Нужно добавить в `run_daily_pipeline.py`
   - Endpoint: `POST /api/client/statistic/orders/generate`

4. **Decision layer (`--skip-decision`)** — пропускается в nightly
   - Broad mode тяжёлый для Render
   - Нужен lightweight targeted mode для daily

5. **Stock daily persistence** — 47 SKU есть в stock API но нет в stock_daily
   - Это coverage gap в stock loader, не identity gap

### 🟢 Низкий приоритет

6. **Organic reporting split** — 342 750 ₽ unreconciled
   - Нужны три витрины: clean organic / marketing attribution / unreconciled
   - Формулу не трогать

7. **Второй golden SKU** — для валидации правил на проблемном товаре
   - Нужен SKU с: высокий CTR/низкий CVR, плохой buyout rate, или selected CPO mismatch

---

## 14. Последние коммиты (хронология)

```
c8e1a5b Fix recovery worker pre-daily guard and Telegram completeness filter
369bcf9 Add CPC recovery worker to daily pipeline
03371d4 Resume pending daily CPC progress in backfill mode
f30be8a Block Telegram Ozon summary on partial ads
d083684 Add Ozon Performance recovery worker
f5c0a3b Add safe Telegram alert preview mode
47e1bb9 Add selected CPO schema target
465c668 Implement guarded selected CPO DB load path
70ee7f1 Add SEARCH_PROMO selected CPO loader plan
016d5db Add inspect-only importer for Ozon selected CPO XLSX
ebd3fbf Add selected CPO LK automation design
5c9b3e1 Add finance advertising reconciliation design
7abd763 Add Ozon selected CPO API support package
e908c29 Add stock API evidence rerun mode
728109b Use Ozon product identity stock evidence as stock fallback
b487b9d Add Ozon product identity loader plan
2cb2481 Add memory optimization to Excel export
5c332a4 Add CPO products report type CLI flag
121a7c7 Add article unit cost lookup for ad diagnostics
```

---

## 15. Ключевые файлы проекта

```
run_daily_pipeline.py                          — orchestrator
alerts_telegram.py                             — Telegram report

loaders/ozon_performance_ads_loader.py         — CPC/CPO/selected CPO
loaders/ozon_stocks_loader.py                  — остатки Ozon
loaders/ozon_product_identity_loader.py        — master identity
loaders/ozon_selected_cpo_xlsx_importer.py     — XLSX fallback (skeleton)

scripts/ozon_performance_recovery_worker.py    — CPC self-healing
scripts/ozon_search_promo_submit_probe.py      — selected CPO discovery

reports_ozon_ad_diagnostic_rule.py            — рекламная диагностика
reports_ozon_sku_organic.py                   — органика
reports_sku_decision_daily_input.py           — decision layer
reports_stock_data_quality_issues.py          — quality stock

export_management_excel.py                    — Excel export (OOM risk)

sql/20260518_create_article_unit_costs.sql    — PENDING MIGRATION
sql/20260511_create_ozon_search_promo_selected_cpo_orders.sql — applied
sql/20260506_create_ozon_product_identity.sql — applied

docs/ozon_selected_cpo_api_support_request.md
docs/ozon_finance_advertising_reconciliation.md
docs/ozon_promotion_analytics_union_import.md
```

---

## 16. Как продолжить работу

При старте новой Claude Code сессии:

```
Прочитай CLAUDE.md и продолжим работу.
Текущий приоритет: [указать из раздела 13]
Hard constraints из раздела 3 всегда соблюдать.
```

При старте GPT сессии — вставить содержимое этого файла в начало.
