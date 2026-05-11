# Ozon Selected CPO Downstream Mapping

## 1. Current confirmed source

Confirmed selected CPO source:

- `source_table = public.ozon_search_promo_selected_cpo_orders`
- `source_report = search_promo_organisation_orders`
- `promotion_type = cpo_selected_products`
- `scope = organisation`
- `kind = SEARCH_PROMO_ORGANISATION_ORDERS`

Confirmed source state for `2026-05-06`:

- `source rows = 3`
- `sum(spend) = 25841.80`
- source rerun is idempotent
- total row `Всего` is excluded from stored source rows and analytical spend

Confirmed current downstream state:

- `marketplace_expenses` has no distinct selected-CPO `expense_type`
- `ozon_daily_sku_ad_attribution` already stores all-products CPO as:
  - `ad_source = cpo`
  - `attribution_type = direct`
  - `campaign_id = ''`
- selected CPO is not explicitly represented downstream yet

## 2. SKU basis decision

Recommended downstream SKU basis:

- use `ordered_sku` as downstream `marketplace_sku` / attribution SKU
- keep `promoted_sku` only as source evidence in `public.ozon_search_promo_selected_cpo_orders`

Reason:

- existing project convention for `ozon_daily_sku_ad_attribution` uses ordered SKU as the main SKU
- `reports_ozon_sku_organic.py` groups ad-attributed metrics by `marketplace_sku`, which currently means ordered SKU
- one confirmed selected CPO row proves the two SKU dimensions can differ:
  - `ordered_sku = 1620655754`
  - `promoted_sku = 1300079194`

So both fields must be preserved, but downstream financial and attribution rollups should use:

- `ordered_sku` for accounting and SKU-day aggregation
- `promoted_sku` for ad evidence / diagnostics

## 3. `marketplace_expenses` future mapping

Recommended future mapping:

- `expense_date = sale_date`
- `marketplace_code = 'ozon'`
- `marketplace_sku = ordered_sku`
- `expense_type = advertising_order_selected_cpo`
- `expense_amount = sum(spend)` grouped by:
  - `sale_date`
  - `ordered_sku`

Source:

- `public.ozon_search_promo_selected_cpo_orders`

Aggregation basis:

- data rows only
- total row `Всего` excluded

Do **not** use:

- `advertising_order_5`

Reason:

- `advertising_order_5` already represents CPO `Все товары`
- selected CPO `Выбранные товары` must remain separate
- mixing them would break management reporting and future reconciliation

Expected future `marketplace_expenses` rollup for `2026-05-06`:

- `1300079194 -> 11164.50`
- `1499239951 -> 4032.10`
- `1620655754 -> 10645.20`
- `total -> 25841.80`

Compatibility note:

- this new `expense_type` will be included automatically by current KPI/report code that uses `expense_type.startswith("advertising")`
- however, presentation/report code that explicitly prints only:
  - `advertising_clicks`
  - `advertising_order_5`
  - `advertising_other`
  will need a later update if selected CPO must appear as a separate visible line

## 4. `ozon_daily_sku_ad_attribution` future mapping

Current all-products CPO identity:

- `ad_source = cpo`
- `attribution_type = direct`
- `campaign_id = ''`

Selected CPO must **not** be written with the same identity.

Recommended future selected CPO identity:

- `ad_source = cpo_selected_products`
- `attribution_type = direct`
- `campaign_id = ''`

Why this is the safer exact value:

- `promotion_type` is already standardized as `cpo_selected_products`
- this keeps selected CPO naming aligned across source and downstream layers
- `attribution_type` should stay `direct`, because in this project it already means attribution semantics:
  - `direct`
  - `associated`
  - `union`
  - `unknown`
- `reports_ozon_sku_organic.py` currently filters `attribution_type = direct`, so changing `attribution_type` for selected CPO would create avoidable downstream blind spots
- current code has no hard equality checks that require `ad_source` to be only `cpc` or `cpo`; a new `ad_source` is safer than overloading `attribution_type`

Recommended future selected CPO attribution mapping:

- `sale_date = sale_date`
- `marketplace_code = 'ozon'`
- `marketplace_sku = ordered_sku`
- `ad_source = cpo_selected_products`
- `attribution_type = direct`
- `campaign_id = ''`
- `ad_spend = sum(spend)` grouped by:
  - `sale_date`
  - `ordered_sku`
  - `ad_source`
  - `attribution_type`
  - `campaign_id`

Important distinction:

- all-products CPO stays:
  - `ad_source = cpo`
  - `attribution_type = direct`
  - `campaign_id = ''`
- selected-products CPO becomes:
  - `ad_source = cpo_selected_products`
  - `attribution_type = direct`
  - `campaign_id = ''`

This cleanly separates:

- CPO all-products
- CPO selected-products
- CPC
- other advertising layers

## 5. Idempotency

Existing source-table idempotency:

- `sale_date`
- `marketplace_code`
- `source_report`
- `promotion_type`
- `order_id`
- `posting_number`
- `ordered_sku`
- `promoted_sku`

Future downstream idempotency:

### `marketplace_expenses`

Existing conflict key:

- `expense_date`
- `marketplace_code`
- `marketplace_sku`
- `expense_type`

That is safe for selected CPO if and only if:

- `expense_type = advertising_order_selected_cpo`

### `ozon_daily_sku_ad_attribution`

Existing primary key:

- `sale_date`
- `marketplace_code`
- `marketplace_sku`
- `ad_source`
- `attribution_type`
- `campaign_id`

That is safe for selected CPO if and only if it uses a distinct downstream identity:

- `ad_source = cpo_selected_products`
- `attribution_type = direct`
- `campaign_id = ''`

This avoids collision with all-products CPO rows already stored as:

- `ad_source = cpo`
- `attribution_type = direct`
- `campaign_id = ''`

## 6. Write ordering for a future approved task

Future downstream write should be:

1. read `public.ozon_search_promo_selected_cpo_orders`
2. verify source sum for the date
3. aggregate by `sale_date + ordered_sku`
4. upsert `marketplace_expenses` with:
   - `expense_type = advertising_order_selected_cpo`
5. upsert `ozon_daily_sku_ad_attribution` with:
   - `ad_source = cpo_selected_products`
   - `attribution_type = direct`
   - `campaign_id = ''`
6. verify totals in both downstream tables

No API calls are needed for that downstream task if the source table is already loaded.

## 7. Blockers and decisions needed

Approved mapping decisions still needed before downstream write:

1. confirm exact `marketplace_expenses.expense_type`
   - recommended: `advertising_order_selected_cpo`
2. confirm exact attribution identity
   - recommended:
     - `ad_source = cpo_selected_products`
     - `attribution_type = direct`
     - `campaign_id = ''`
3. confirm whether management-facing exports should show selected CPO separately from:
   - `advertising_order_5`
4. confirm whether Telegram reports should remain unchanged until a later reporting task

Recommended answer set:

- `marketplace_expenses.expense_type = advertising_order_selected_cpo`
- `ozon_daily_sku_ad_attribution.ad_source = cpo_selected_products`
- `ozon_daily_sku_ad_attribution.attribution_type = direct`
- keep Telegram and presentation exports unchanged until a later explicit reporting task

## 8. Explicit non-goals of this document

This document does **not** approve or perform:

- API calls
- source-table reload
- source-table rerun
- downstream writes
- migrations
- pipeline runs
- Telegram changes
- Render changes

It only defines the downstream mapping proposal from the already confirmed selected CPO source layer.
