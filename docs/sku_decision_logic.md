# SKU Decision Logic

## Goal

The daily decision layer prepares one D-1 management surface per Ozon SKU so we can later make price and ads decisions from a consistent dataset instead of from fragmented reports.

## Why this is separate from auto-actions

- The pipeline only builds data and dry-run recommendations.
- It does **not** change prices.
- It does **not** change Ozon bids or campaign settings.
- Recommendations stay human-reviewed until the data quality and business rules are stable enough for automation.

## Daily source priority

For `sku_decision_daily_input` we use:

1. `daily_sku_kpi` for yesterday's operational SKU metrics
2. `ozon_daily_sku_organic` for ad-attributed vs organic revenue
3. `ozon_performance_daily_load_status` for CPC/CPO completeness
4. `stock_daily` for current stock snapshot if available
5. recent `marketplace_orders` as a price proxy if no dedicated live price source exists

## Why daily-yesterday matters

- The Ozon Performance CPC export is limited by `2000 campaigns/day`.
- Current campaign count is about `785`, so a one-day D-1 load fits comfortably into the limit.
- A rolling 30-day CPC window belongs to backfill/historical analysis, not to the morning production load.

## Expected revenue and margin model

The first MVP uses a conservative management model:

- `buyout_rate_rolling_14d = sum(buyouts_qty) / sum(orders_qty)` for the trailing 14 days
- `buyout_rate_rolling_30d = sum(buyouts_qty) / sum(orders_qty)` for the trailing 30 days
- `expected_revenue_after_buyout = orders_revenue * chosen_buyout_rate`

The chosen buyout rate is:

- rolling 14d if it exists
- otherwise rolling 30d

Expected margin after ads is a **contribution-style** estimate:

- start from `expected_revenue_after_buyout`
- apply rolling 30d platform expense rates:
  - commission
  - logistics
  - other marketplace expenses
- subtract current `ad_spend`

So:

- `expected_margin_after_ads` is useful for management prioritization
- but it is **not** a full accounting margin if cost of goods is not loaded into the model

## Recommendation-only actions

The dry-run recommendation script can label SKUs as:

- `hold`
- `increase_ads`
- `decrease_ads`
- `increase_price`
- `decrease_price`
- `stop_ads`
- `watch`

## MVP rules

### increase_ads

- buyout rate is above the threshold
- expected margin after ads is positive
- ROAS is above target
- stock is sufficient
- `data_quality_status = ok`

### decrease_ads

- buyout rate is weak
- ads are expensive relative to attributed revenue
- or expected margin after ads is negative/near-zero

### stop_ads

- ad spend exists
- expected margin after ads is non-positive

### increase_price

- order volume is meaningful
- ad share of revenue is high or margin is squeezed
- buyout rate is still healthy
- stock is not critically low
- recommendation is still limited to a small step, typically `2-5%`

### decrease_price

- demand is weak
- stock is comfortable
- data quality is good enough
- there is no sign that more ads alone would fix the issue

### hold

- partial attribution
- missing total source
- missing buyout rate
- low data volume
- stock risk

## Data quality gating

Recommendations should not be treated as strong actions when:

- `partial_ads`
- `partial_quota`
- `missing_total`
- `ad_attribution_without_total`
- `ad_revenue_exceed_total`
- missing buyout history
- missing stock

In those cases the recommendation layer stays conservative and usually returns `hold` or `watch`.
