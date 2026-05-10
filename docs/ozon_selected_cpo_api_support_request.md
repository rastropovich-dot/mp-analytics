# Ozon Selected CPO API Discovery Result

## Status

**API found and confirmed.**

Selected CPO for Ozon `Оплата за заказ: выбранные товары` is available through:

- `source_report = search_promo_organisation_orders`
- `promotion_type = cpo_selected_products`
- `scope = organisation`

This report is organisation-level. It does **not** require `campaignId` and current
campaign-filtered payload probes did not bind `campaignId` in request echo.

## Confirmed endpoint contract

### Submit

`POST https://api-performance.ozon.ru/api/client/statistic/orders/generate`

Payload:

```json
{
  "from": "<Moscow D-1 start converted to UTC>",
  "to": "<Moscow D-1 end converted to UTC>"
}
```

Example for `2026-05-06` MSK:

```json
{
  "from": "2026-05-05T21:00:00Z",
  "to": "2026-05-06T20:59:59Z"
}
```

### Status

Working async backend:

`GET https://api-performance.ozon.ru/api/client/statistics/{UUID}`

### Download

Working async backend:

`GET https://api-performance.ozon.ru/api/client/statistics/report?UUID={UUID}`

## Important namespace detail

Singular status/download paths return `404`:

- `/api/client/statistic/{UUID}`
- `/api/client/statistic/report?UUID=...`

Plural status/download paths are the working async backend:

- `/api/client/statistics/{UUID}`
- `/api/client/statistics/report?UUID=...`

## Confirmed report kind

- `kind = SEARCH_PROMO_ORGANISATION_ORDERS`

This is the confirmed selected CPO source family.

## Confirmed CSV structure

Columns:

- `Дата`
- `ID заказа`
- `Номер заказа`
- `SKU`
- `SKU продвигаемого товара`
- `Артикул`
- `Источник заказов`
- `Название товара`
- `Количество`
- `Стоимость продажи, ₽`
- `Стоимость, ₽`
- `Ставка, %`
- `Ставка, ₽`
- `Расход, ₽`

The file contains:

- data rows
- one total row with `Дата = Всего`

The total row must be excluded from analytical `spend_sum`.

## Confirmed reconciliation for `2026-05-06`

- expected missing selected CPO = `25 841.80`
- report data-only `spend_sum = 25 841.80`
- report total-row `spend_sum = 25 841.80`
- raw sum including total row = `51 683.60`
- diff vs expected missing selected CPO = `0.00`

So the selected CPO layer is confirmed by API.

## Campaign filter note

- no `campaignId` is required for the working organisation-level report
- campaign-filtered payload shapes created UUIDs but did not bind `campaignId`
- request echo stayed at:
  - `campaignId = "0"`
  - or default `1970` payload fields for invalid shapes

Current safe interpretation:

- source is organisation-wide SEARCH_PROMO selected CPO orders
- this source should be classified by endpoint + report kind + reconciliation result
- it must **not** be classified as CPC even if some rows contain human-readable
  `Источник заказов = Кампания за клики`

## Implementation guidance

Use:

- `source_report = search_promo_organisation_orders`
- `promotion_type = cpo_selected_products`
- `scope = organisation`
- `campaign_filter_supported = false`

Do **not** classify this report by the `Источник заказов` column.

## What is no longer needed for this layer

- XLSX importer is **not needed** for this selected CPO layer
- Playwright automation is **not needed** for this selected CPO layer
- Ozon support request is **no longer needed for source discovery**

## Current limit

DB load is **not implemented yet**. It requires a separate approved task.

