# Ozon Selected CPO API Support Request

## Status update

Ozon support has now confirmed the official direction:

- for `Оплата за заказ: выбранные товары` use Performance API methods:
  - `SearchPromoOrdersReportSubmitRequest`
  - `SearchPromoProductsReportSubmitRequest`
- `POST /api/client/statistics/json` must not be used for `Оплата за заказ`
- `all_sku_promo/orders` and `all_sku_promo/products` remain sources only for
  `Оплата за заказ (все товары)`

This changes the implementation plan:
- primary path = `SEARCH_PROMO` reporting API
- fallback path = LK export / XLSX importer
- Playwright automation remains fallback only

## Case

We need an automatic API source for Ozon Performance `CPO "selected products"` statistics.

Observed campaign:
- `campaign_id = 4471285`
- `advObjectType = SEARCH_PROMO`
- `paymentType = CPO`
- `title = "Оплата за заказ: выбранные товары"`

Observed API behavior:
- `POST /api/client/statistics/json` for this campaign and `2026-05-06..2026-05-06` returns:
  - `400`
  - `generation of this type of report is forbidden for the transferred list of campaigns`
- `GET /api/client/statistics/all_sku_promo/orders/generate` returns only:
  - `Оплата за заказ (все товары). Отчёт по заказам`
- `GET /api/client/statistics/all_sku_promo/products/generate` returns only:
  - `Оплата за заказ (все товары). Отчёт по товарам`

Business reconciliation for `2026-05-06`:
- LK `CPO "selected products"` spend = `25 841.80 RUB`
- LK `CPO "all products"` spend = `178 449.50 RUB`
- LK `CPC total` = `54 881.60 RUB`
- API/database currently load:
  - CPC = `54 881.59 RUB`
  - CPO all products = `178 449.50 RUB`
- Missing layer:
  - `CPO "selected products" = 25 841.80 RUB`

## Support-confirmed implementation direction

Use `SEARCH_PROMO` reporting methods instead of `statistics/json`:

1. `SearchPromoOrdersReportSubmitRequest`
2. `SearchPromoProductsReportSubmitRequest`

Practical meaning:
- `statistics/json` failure for campaign `4471285` was expected for this campaign type
- `all_sku_promo/*` and `search_promo/*` are separate source families
- XLSX / LK automation should be kept only as fallback if SEARCH_PROMO API still
  proves insufficient in live dry-runs

## Questions to Ozon Support

1. Which official API endpoint should be used to download statistics for `Оплата за заказ: выбранные товары`?
2. Is `SEARCH_PROMO / CPO` campaign type supported through Ozon Performance API?
3. If `statistics/json` is forbidden for such campaigns, which API report should be used instead?
4. If there is no API path, can you confirm that `selected products CPO` is available only through LK export (XLSX/CSV)?

## Evidence to attach

- campaign metadata:
  - `campaign_id = 4471285`
  - `advObjectType = SEARCH_PROMO`
  - `paymentType = CPO`
  - `title = "Оплата за заказ: выбранные товары"`
- `statistics/json` error text:
  - `generation of this type of report is forbidden for the transferred list of campaigns`
- `all_sku_promo/orders` and `all_sku_promo/products` both resolve to `все товары`
- LK screenshot or values for `2026-05-06` with `25 841.80 RUB`

## Short message for support

We found an Ozon Performance campaign `4471285` with `advObjectType=SEARCH_PROMO`, `paymentType=CPO`, title `Оплата за заказ: выбранные товары`. For `2026-05-06`, LK shows `25 841.80 RUB` spend for this selected-products CPO layer. However, `POST /api/client/statistics/json` returns `400: generation of this type of report is forbidden for the transferred list of campaigns`, while `all_sku_promo/orders` and `all_sku_promo/products` both return only `Оплата за заказ (все товары)`. Please confirm which official API/report should be used for `selected products CPO`, or whether it is available only via LK export.
