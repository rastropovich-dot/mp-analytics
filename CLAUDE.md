# MP Analytics — Project Context for Claude Code

> Единый источник правды для всех AI-сессий.
> Последнее обновление: 2026-05-28

---

## 1. Цель проекта

Управленческий инструмент для маркетплейсов Ozon и WB. Auto-actions выключены.

---

## 2. Инфраструктура

| Компонент | Детали |
|---|---|
| БД | Supabase (PostgreSQL) |
| Хостинг | Render (cron job) |
| Уведомления | Telegram |
| Репозиторий | github.com/rastropovich-dot/mp-analytics / main |

### Render расписание
```
03:10 UTC — daily pipeline
python3 run_daily_pipeline.py --skip-telegram --skip-excel --skip-decision \
  --ozon-campaign-selection smart_recent_active \
  --ozon-recent-activity-days 7 \
  --ozon-dormant-probe-size 100 \
  --ozon-max-daily-cpc-units 1000 \
  --ozon-allow-staged-cpc-partial

07:30 UTC — Telegram
python3 alerts_telegram.py
```

Rollback: `python3 run_daily_pipeline.py --skip-telegram --skip-excel --skip-decision`

---

## 3. Hard Constraints

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

## 4. Ozon Performance API

### Официальные лимиты
```
Одновременных выгрузок с аккаунта:     1  ← КРИТИЧНО
Одновременных выгрузок по организации: 5
Выгрузок за 24 часа:                   min(активные_кампании × 240, 2000)
Максимум кампаний в отчёте:            10
```
После cooldown успех НЕ гарантирован (подтверждено поддержкой).

### Endpoints
```
CPC статистика:
POST /api/client/statistics/json
→ лимит динамический, concurrent = 1

CPO "Все товары":
GET /api/client/statistics/all_sku_promo/orders/generate
→ НЕ входит в лимит

Selected CPO "Выбранные товары":
POST /api/client/statistic/orders/generate  ← singular!
payload: {"from": "...", "to": "..."}
kind: SEARCH_PROMO_ORGANISATION_ORDERS
→ НЕ входит в лимит
```

### 429 классификация
```
retryable_throttle       — ждём Retry-After, продолжаем
daily_quota_exhausted    — body "Превышен дневной лимит"
                           → стоп до quota reset (00:00 UTC)
```

### CPC Selection modes
```
complete              — все CPC кампании (~1675 units)
smart_recent_active   — активные за N дней (~1206 units, -28%)
dormant_probe         — выборка спящих (100 units)
```

---

## 5. Recovery Worker

```
scripts/ozon_performance_recovery_worker.py

pre phase:  первый шаг pipeline (до WB)
post phase: после Ozon Performance daily
            --wait-for-minutes 240 (relative deadline)
max 1 batch per run
budget guard: skip если used > 1500
при daily_quota_exhausted: controlled stop
write: --write --approve-recovery-worker-write
```

### Текущий backlog (приоритет)
```
2026-05-27: progress есть (batch 68/132), status row MISSING → patch needed
2026-05-26: partial_quota, next_batch=16, ~1075 pending
2026-05-25: partial_quota, next_batch=139, 285 pending
2026-05-24: partial_quota, ~20 completed / 1303 pending
2026-05-23: partial_quota, ~680 completed / 643 pending
```

---

## 6. Telegram Gate

Ozon incomplete если: нет KPI/organic/ad rows ИЛИ partial_ads/pending_429/pending_quota.
WB reporting живёт отдельно, НЕ отключается из-за Ozon partial.

```bash
python alerts_telegram.py --dry-run --no-send --skip-snapshot --target-date YYYY-MM-DD
```

---

## 7. Golden Article

```
article:  F000283615
sku:      1300079194
product:  Серьги золотые 585 с танцующими бриллиантами KARATOV
COGS:     32 963 ₽  ← пока hardcode, нужен DB seed
```

### Эталонная дата: 2026-05-16
```
orders = 2 / 221 646 ₽ | CPC = 3 369.84 ₽ | selected CPO = 22 047.30 ₽
organic = 221 646 ₽ | net estimate = 36 161.71 ₽
total_order_TACOS = 7.64% | cpc_order_TACOS = 1.01%
```

### Selected CPO materialized
```
2026-05-12: 11 286.30  2026-05-15: 32 995.00  2026-05-20: 56 431.50
2026-05-13: 55 325.00  2026-05-16: 22 047.30  2026-05-21: 33 858.90
2026-05-14: 0.00
```

### Forecast economics (добавлен слой)
```
reports_sku_order_forecast_economics.py
expected_fin_result = (orders_revenue × buyout_rate - costs) - total_ad_spend
```

---

## 8. Открытые треки

### 🔴 Срочные
1. **2026-05-27 without status** — patch: `recoverable_progress_without_status`
2. **Concurrent job check** — перед submit проверять externallist (concurrent=1)
3. **Dynamic budget** — считать min(active_campaigns × 240, 2000)

### 🟡 Важные
4. **article_unit_costs migration** — применить + seed F000283615 / 32963
5. **Excel OOM** — фильтр 90 дней для daily_sku_kpi / marketplace_expenses / organic
6. **Selected CPO в daily pipeline** — автоматизировать
7. **Persistent usage ledger** — sql/20260526_create_ozon_statistics_json_usage.sql
8. **Decision layer** — убрать --skip-decision, нужен lightweight mode

### 🟢 Низкий приоритет
9. Smart CPC v2 — ужесточить до 800-1000 units
10. Organic reporting split — три витрины
11. Stock daily persistence — 47 SKU gap
12. Второй golden SKU

---

## 9. Ключевые файлы

```
run_daily_pipeline.py
alerts_telegram.py
loaders/ozon_performance_ads_loader.py
scripts/ozon_performance_recovery_worker.py
reports_ozon_ad_diagnostic_rule.py
reports_sku_order_forecast_economics.py
export_management_excel.py                 ← OOM risk
sql/20260518_create_article_unit_costs.sql ← PENDING
sql/20260526_create_ozon_statistics_json_usage.sql ← PENDING
```

---

## 10. Последние коммиты

```
8bce2f3 Use relative deadline for Ozon post recovery
c9267a4 Classify Ozon CPC daily quota exhaustion
a91719c Add smart staged Ozon CPC loading
ea1f8dc Allow recovery worker before CPC backfill window
287be85 Use exact runtime progress key for CPC recovery
54a3ac5 Normalize runtime cooldown timestamps
bd1046c Make runtime state read sync non-fatal
73d2468 Make Ozon runtime state cleanup non-fatal
9444436 Add wait-and-resume Ozon CPC recovery
c8e1a5b Fix recovery worker pre-daily guard and Telegram completeness filter
369bcf9 Add CPC recovery worker to daily pipeline
```
