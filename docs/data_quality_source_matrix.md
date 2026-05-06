# Data Quality Source Matrix

## 1. `marketplace_orders`

- Grain: one marketplace order row.
- Main keys:
  - `marketplace_sku`
  - `article`
- Date semantics:
  - business date comes from `order_date`
  - ingestion/runtime timestamp comes from `created_at`
- Revenue meaning:
  - `orders_amount_seller` is seller-side order revenue
- Limits:
  - operational order feed, not a guaranteed analytics cube
  - may miss rows that are present in marketplace analytics or promotion attribution
  - identity is centered around decision SKU and article, not Ozon product id

## 2. `ozon_daily_sku_total_orders`

- Source: Ozon Seller Analytics `/v1/analytics/data`
- Grain: `sale_date + marketplace_sku`
- Main keys:
  - `marketplace_sku`
- Revenue meaning:
  - `total_orders_revenue`
- Limits:
  - daily analytics cube, not a promotion attribution source
  - article can be missing or incomplete for some SKU rows
  - this is the preferred total source for clean Ozon organic decomposition

## 3. `ozon_daily_sku_ad_attribution`

- Source: Ozon Performance / Promotion reports
- Grain:
  - `sale_date + marketplace_sku + ad_source`
  - row may also carry `order_sku`, `promoted_sku`, and attribution metadata
- Main keys:
  - `marketplace_sku` (current order-side storage key)
  - `order_sku`
  - `promoted_sku`
  - `article`
- Revenue meaning:
  - `ad_orders_revenue` is ad-attributed revenue
- Limits:
  - order SKU and promoted SKU are different business entities and must not be merged blindly
  - direct attribution can be clean; promoted / union / associated semantics may require separate treatment
  - this source is not the source of truth for total sales

## 4. `stock_daily`

- Source: Ozon stock API
- Grain:
  - latest available snapshot row by product / warehouse / stock type
- Main keys:
  - `product_id`
  - `stock_marketplace_sku`
  - `decision_marketplace_sku`
  - `article`
- Quantity semantics:
  - `stock_qty`
  - `available_qty`
  - `reserved_qty`
- Limits:
  - stock source is product-centric, not decision-SKU-native
  - `stock_daily.marketplace_sku` historically stored stock-side product identity, not decision SKU
  - `0 stock` means `stock_out`; `NULL stock` means missing stock mapping or source gap

## 5. `sku_catalog`

- Reference catalog for product identity.
- Main keys:
  - `product_id`
  - `marketplace_sku`
  - `article`
  - `product_name`
- Meaning:
  - `product_id` is Ozon product-side identity
  - `marketplace_sku` is not guaranteed to equal every stock-side or promotion-side identifier
  - article is often the safest bridge between stock and decision tables, but not always present

## 6. `daily_sku_kpi` / `sku_decision_daily_input`

- Grain:
  - `kpi_date + marketplace_sku`
- Purpose:
  - management KPI layer and decision surface
- Clean fields:
  - order and buyout metrics from KPI layer
  - stock only when stock identity is resolved
  - organic/ad metrics only when reconciliation is clean
- Partial fields:
  - any row with `partial_ads`
  - any row with `missing_stock`
  - any row with non-clean organic reconciliation

## Why IDs cannot be collapsed into one `marketplace_sku`

- `decision marketplace_sku` in order/KPI logic
- `product_id` in stock API logic
- `order_sku` in ad attribution logic
- `promoted_sku` in promotion logic

These identifiers overlap in some cases, but they are not universally interchangeable. Data quality closure requires explicit mapping, not a global assumption that every field called `sku` is the same entity.
