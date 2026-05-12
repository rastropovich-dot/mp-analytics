# Ozon Selected CPO Daily Rollout

## 1. Current status

Current selected CPO status:

- source is confirmed
- source write is idempotent
- downstream write is idempotent
- daily hook is implemented and gated
- Render configuration is unchanged

Confirmed local daily-style dry-run for `2026-05-06`:

- `source_rows = 3`
- `source_sum = 25841.80`
- `marketplace_expenses_rows = 3`
- `marketplace_expenses_sum = 25841.80`
- `ad_attribution_rows = 3`
- `ad_attribution_sum = 25841.80`
- `totals_match = true`
- `db_writes = 0`

Current code state:

- commit `60b57ff` added the gated daily integration
- commit `c70c547` changed daily dry-run to use the already-loaded source table instead of calling the API

## 2. Feature flags

Two flags control daily selected CPO behavior:

- `ENABLE_OZON_SELECTED_CPO_DAILY=false` by default
- `APPROVE_OZON_SELECTED_CPO_DAILY_WRITE=false` by default

Meaning:

- if `ENABLE_OZON_SELECTED_CPO_DAILY=false`, selected CPO daily step is skipped
- if `ENABLE_OZON_SELECTED_CPO_DAILY=true` but `APPROVE_OZON_SELECTED_CPO_DAILY_WRITE=false`, selected CPO daily step can run only in dry-run style and must not write DB
- only when **both** are `true` may the daily selected CPO write path run

## 3. Safe rollout sequence

Recommended rollout:

1. keep both flags `false`
2. run a local daily-style dry-run
3. enable only:
   - `ENABLE_OZON_SELECTED_CPO_DAILY=true`
   - keep `APPROVE_OZON_SELECTED_CPO_DAILY_WRITE=false`
4. verify dry-run summary and confirm:
   - no writes
   - totals match
   - selected CPO rows/sums are stable
5. only after that enable:
   - `APPROVE_OZON_SELECTED_CPO_DAILY_WRITE=true`
6. monitor the next D-1 selected CPO totals in:
   - source table
   - `marketplace_expenses`
   - `ozon_daily_sku_ad_attribution`

The intended rollout order is:

- dry-run first
- controlled write second
- routine production daily only after the first controlled production result is verified

## 4. Rollback

Rollback sequence:

1. set `APPROVE_OZON_SELECTED_CPO_DAILY_WRITE=false`
2. then set `ENABLE_OZON_SELECTED_CPO_DAILY=false`

Important:

- no Render schedule change is required for rollback
- the existing daily pipeline step remains the same
- selected CPO is only an internal gated sub-stage inside the existing Ozon Performance step

## 5. Expected downstream identities

Expected selected CPO downstream mapping:

### `marketplace_expenses`

- `expense_type = advertising_order_selected_cpo`

### `ozon_daily_sku_ad_attribution`

- `ad_source = cpo_selected_products`
- `attribution_type = direct`
- `campaign_id = ''`

These identities must stay distinct from existing all-products CPO.

## 6. What must not change

The following must remain unchanged:

- `advertising_order_5` remains all-products CPO
- `ad_source = cpo` remains all-products CPO
- `statistics/json` is not used for selected CPO
- Render schedule remains unchanged

Also:

- selected CPO must not be merged into all-products CPO
- selected CPO must not reuse `advertising_order_5`
- selected CPO must not reuse `ad_source = cpo`

## 7. Operational note

The selected CPO daily hook is safe to keep in code with flags off.

That gives a clean operational posture:

- code path exists
- local dry-run can be verified
- production behavior stays unchanged until both flags are explicitly enabled
