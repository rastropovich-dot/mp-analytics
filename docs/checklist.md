# Ozon Performance Acceptance Checklist

## Mixed CPC+CPO Acceptance

Status: CLOSED

Acceptance test for date `2026-04-02` is accepted as passed.

- CPC success
- CPO success
- `run_status = success`
- `advertising_clicks = 2429.59`
- `advertising_order_5 = 120962.0`
- `total_advertising_sum = 123391.59`
- `raw advertising expenses = build_kpi().ad_spend = 123391.59`
- second run `row_count` unchanged: `272`
- second run sum unchanged: `123391.59`
- `dupe_keys = 0`
- job cache confirmed on second run

Notes:

- First full write-run for `2026-04-02` wrote both CPC and CPO expenses successfully.
- Second full write-run for the same date reused cached UUIDs for both `statistics/json` and `all_sku_promo/orders`.
- Mixed CPC+CPO acceptance item is closed.

## CPO «Все» (all_sku_promo/orders) отдаёт `state=ERROR` на стороне Ozon

Status: OPEN (РБХ-2, окно 2026-08-23 … 2026-10-21; найдено 2026-10-23)

Признак: `POST /api/client/statistics/all_sku_promo/orders/generate` принимает задание, опрос `/api/client/statistics/{UUID}`
отдаёт `state=ERROR` с текстом Ozon «failed to create report: failed to get rows: failed to check if statistics result is
ready: code: 47, message: Received from prodch-adv-events-N-z50x.h.o3.ru:9000. DB::Exception: Missing columns:
'bd_ord_contract' …» — ошибка их ClickHouse, не нашего запроса: 2026-10-23 09:22 … 09:42 UTC четыре раза подряд, и за 60
дней, и за один день (`--mode cpo-report-check --date 2026-10-20`), на трёх разных узлах. Отчёты `statistics/json` (CPC)
при этом снимаются нормально. Не наш дефект; в чат разработчиков Ozon не писать без слова владельца.

Что делать:

1. В `--mode full` за окно истории CPC пишется без CPO флагом `--skip-cpo` (только full; `overall_status = partial_ads`,
   `cpo.status = skipped / skip_cpo_flag`). В ночном `daily-yesterday` флага нет и быть не должно: отказ CPO роняет шаг,
   утро показывает `ozon_ads_layer_missing` — это и есть сигнал.
2. Через сутки повторить пробу: `MP_CABINET=<кабинет> venv/bin/python3 loaders/ozon_performance_ads_loader.py
   --mode cpo-report-check --date <D>` — 1 выгрузка, без записи. `state=OK` — отчёт ожил.
3. При успехе — добор CPO за окно: сброс ключа `cpc_progress:<hash>` в `pipeline_runtime_state` со снимком в
   `data/<кабинет>/snapshots/` (иначе прогон считает CPC «завершённым» и пишет 0 CPC-строк), затем `--mode full
   --date-from … --date-to …` **без** `--skip-cpo`: пачки CPC возьмутся из кэша заданий Ozon (ключи `jobs:`; submit 0,
   пока кэш жив), CPO запросится заново; после — витрины `reports_daily_sku_kpi.py` → `reports_daily_marketplace_kpi.py`.
   Запасной путь без кэша: `scripts/ozon_cpo_one_day.py --date D --fetch --write --approve-cpo-write` по дням окна
   (по выгрузке на день).
4. Арбитр после добора — по типам порознь: тип 54 леджера = CPO «Все» до копейки (у РБХ-1 57 дат из 59, две — на 29,90 и
   62,70), тип 41 ≥ клики (разница — кампании не-SKU типов, которых загрузчик не листает; не стоп).

## Ключи с истечением (55-я, 2026-10-23)

Ключи с истечением: WB (JWT `exp` — читает `render_env_sync.py`), Ozon Seller (`POST /v1/roles` → `expires_at`;
с 03.09.2026 новые ключи живут 3 месяца, до того — 6) — проверка в §0 каждую неделю; за 14 дней до истечения —
новый ключ в `~/mp-analytics/.env` и на оба сервиса Render (`scripts/render_env_sync.py --key … --apply` по слову).
Прецедент: ключ Ozon истёк 22.10.2026 днём, ночь 23.10 прошла без Ozon (8 шагов на 403).
