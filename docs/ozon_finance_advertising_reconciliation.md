# Ozon Finance Advertising Reconciliation

## Purpose

Use Seller Finance API only as a reconciliation layer for total advertising spend, not as a source of SKU-level advertising attribution.

## Current code behavior

Current loaders:
- `/Users/mihaileliseev/mp-analytics/loaders/ozon_finance_transactions_loader.py`
- `/Users/mihaileliseev/mp-analytics/loaders/ozon_expenses_loader.py`

Current finance expense logic:
- advertising operations are recognized
- but they are intentionally skipped before writing to `marketplace_expenses`

Recognized finance advertising operation types:
- `OperationMarketplaceCostPerClick`
- `OperationPromotionWithCostPerOrder`
- `MarketplaceMarketingActionCostOperation`

Reason:
- avoid double counting against Ozon Performance API spend already loaded into:
  - `marketplace_expenses`
  - `ozon_daily_sku_ad_attribution`

## Guardrails

Finance API can be used for:
- total spend reconciliation
- checking missing spend buckets
- explaining residual gap between LK Finance and Performance-based ad layer

Finance API must **not** be used for:
- `ad_orders_qty`
- `ad_orders_revenue`
- SKU/campaign attribution logic

## Reconciliation targets

For `2026-05-06`:
- Finance UI `Продвижение и реклама` = `261 637 RUB`
- current DB advertising = `233 331.09 RUB`
- missing selected CPO = `25 841.80 RUB`
- residual after adding selected CPO = about `2 464.11 RUB`

Finance may explain:
- selected CPO spend as posting-level or operation-level fact
- residual gap caused by date semantics / tax semantics / finance-only marketing operations

Finance cannot explain:
- which SKU received attributed orders or revenue

## Proposed staging table

Use a dedicated raw reconciliation layer:
- `ozon_finance_advertising_reconciliation_raw`

This table should store raw finance advertising transactions without mixing them into `marketplace_expenses`.

## Safe workflow

1. load raw finance transactions into staging
2. classify advertising rows by `operation_type` and `service_name`
3. compare finance total vs:
   - Performance API CPC
   - CPO all products
   - future selected CPO source
4. keep residual as reconciliation difference

## Why not write finance advertising directly into marketplace_expenses

Because that would:
- double count against Performance API spend
- blur spend reconciliation with attribution
- make Telegram / KPI reporting unstable

Finance should remain a control layer, not a direct replacement for Performance reporting.
