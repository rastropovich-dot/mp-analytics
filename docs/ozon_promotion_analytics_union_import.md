# Ozon Promotion Analytics Union Import

## Current state

- The current database already stores:
  - `order_sku`
  - `promoted_sku`
  - `attribution_type`
- Clean Ozon organic decomposition is built only on:
  - `order_sku`
  - matched total source
  - `ad_orders_revenue <= total_orders_revenue`
- Marketing attribution can be analyzed separately by:
  - `promoted_sku`

## What is already true

- Current organic unknown count is `0`.
- Current unreconciled tail is explained by machine-readable statuses.
- We should not mix `promoted_sku` into clean organic decomposition by `order_sku`.

## Current gap

In the current API / CSV layer we do not have an explicit Union / associated projection for Ozon Promotion Analytics.

What we do have:
- direct attribution rows
- `order_sku`
- `promoted_sku`
- `attribution_type`

What we do not have as a separate modeled layer:
- Union tab projection
- associated / merged-card projection
- explicit promotion analytics report type split in DB

## Implication

If the business wants deeper closure for promotion analytics beyond current direct attribution:
- the next step is not to change the organic formula
- the next step is to import the native Ozon LK XLSX report

## Proposed future importer

`loaders/ozon_promotion_analytics_xlsx_importer.py`

Expected scope:
- read XLSX exported from Ozon LK
- parse `Statistics` tab
- parse `Union` tab
- persist a separate analytics layer for:
  - clean direct attribution
  - union / associated attribution
  - promoted-SKU marketing performance

## Guardrail

Until such importer exists:
- clean organic stays on `order_sku`
- unreconciled attribution stays separate
- promoted-SKU analytics stays a marketing view, not organic decomposition
