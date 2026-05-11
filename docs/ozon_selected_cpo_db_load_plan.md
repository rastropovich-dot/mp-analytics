# Ozon Selected CPO DB Load Plan

## Current state

Selected CPO API source is confirmed:

- `source_report = search_promo_organisation_orders`
- `promotion_type = cpo_selected_products`
- `scope = organisation`
- `kind = SEARCH_PROMO_ORGANISATION_ORDERS`

The loader can already:

- submit the organisation-wide report
- poll and download it
- parse semicolon CSV with preamble/header detection
- exclude the total row `Всего` from analytical spend
- produce normalized order-level rows

## Why a dedicated source table is needed

### Why not `marketplace_expenses`

`marketplace_expenses` aggregates ad spend by:

- `expense_date`
- `marketplace_code`
- `marketplace_sku`
- `expense_type`

That is too coarse for selected CPO source evidence.

It also has no approved distinct selected-CPO category yet, so writing there now would risk
mixing:

- all-products CPO
- selected-products CPO

### Why not `ozon_daily_sku_ad_attribution`

`ozon_daily_sku_ad_attribution` already stores `order_sku` and `promoted_sku`, but its current
primary key is:

- `sale_date`
- `marketplace_code`
- `marketplace_sku`
- `ad_source`
- `attribution_type`
- `campaign_id`

That key is too coarse for selected CPO source rows and cannot safely separate:

- `cpo_all_products`
- `cpo_selected_products`

Organisation-level SEARCH_PROMO rows also have no stable `campaign_id` for idempotency.

## Dedicated target

Use a dedicated source/evidence table:

- `ozon_search_promo_selected_cpo_orders`

This table stores order-level selected CPO evidence before any future rollup into:

- `marketplace_expenses`
- `ozon_daily_sku_ad_attribution`

## Idempotency key

Stable unique key:

- `sale_date`
- `marketplace_code`
- `source_report`
- `promotion_type`
- `order_id`
- `posting_number`
- `ordered_sku`
- `promoted_sku`

Important:

- `source_uuid` is stored as evidence, but **not** included in the unique key
- report UUID changes across reruns, so it is not stable enough for idempotency

## Stored fields

Required source row fields:

- `sale_date`
- `marketplace_code`
- `order_id`
- `posting_number`
- `ordered_sku`
- `promoted_sku`
- `attribution_sku`
- `attribution_sku_basis`
- `offer_id`
- `promoted_article`
- `order_source_raw`
- `product_name`
- `quantity`
- `sale_amount`
- `item_amount`
- `bid_percent`
- `bid_amount`
- `spend`
- `source_report`
- `promotion_type`
- `scope`
- `source_kind`
- `source_uuid`
- `raw_row`

## Current loader behavior

### `plan_only=True`

- no HTTP
- no credentials required
- no DB writes
- returns target table + mapping + idempotency design

### `dry_run=True, write=False`

- may perform HTTP if explicitly called
- parses report
- prepares normalized rows
- prepares source-table rows
- prepares `would_write` summary
- does not write DB

### `write=True`

Still guarded by default:

- requires explicit `schema_applied=True`
- requires explicit injected DB client
- still not used in live execution in current task

This is intentional. Migration must be applied in a separate approved step before any real write.

## Next future step

1. Apply migration for `ozon_search_promo_selected_cpo_orders` in a controlled DB task.
2. Run one-day controlled live write for a confirmed date.
3. Validate idempotent rerun behavior.
4. Only after that, design rollup into reporting tables with approved selected-vs-all separation rules.
