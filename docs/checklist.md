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

## Ключи с истечением (55-я, 2026-10-23)

Ключи с истечением: WB (JWT `exp` — читает `render_env_sync.py`), Ozon Seller (`POST /v1/roles` → `expires_at`;
с 03.09.2026 новые ключи живут 3 месяца, до того — 6) — проверка в §0 каждую неделю; за 14 дней до истечения —
новый ключ в `~/mp-analytics/.env` и на оба сервиса Render (`scripts/render_env_sync.py --key … --apply` по слову).
Прецедент: ключ Ozon истёк 22.10.2026 днём, ночь 23.10 прошла без Ozon (8 шагов на 403).
