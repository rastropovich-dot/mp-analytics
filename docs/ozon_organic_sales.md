# Ozon SKU Organic Sales

## Что считается

В Ozon API нет готового поля `organic sales` по SKU и дню, поэтому в проекте используется расчет:

- `organic_orders_qty = max(total_orders_qty - ad_orders_qty, 0)`
- `organic_orders_revenue = max(total_orders_revenue - ad_orders_revenue, 0)`

Где:

- `total_orders_qty` и `total_orders_revenue` — все заказы по SKU за день;
- `ad_orders_qty` и `ad_orders_revenue` — рекламно-атрибутированные заказы и выручка по SKU за день;
- отрицательная органика не допускается и обрезается до `0` с warning.

## Источники данных

### Total orders / revenue

Приоритет total source теперь такой:

1. `ozon_daily_sku_total_orders` c `total_revenue_source = seller_analytics`
2. fallback на `marketplace_orders`

`ozon_daily_sku_total_orders` наполняется отдельным loader:

- `loaders/ozon_sku_total_analytics_loader.py`

Fallback `marketplace_orders` остается на случай, если seller analytics за дату еще не загружен:

- таблица `marketplace_orders`
- загрузчики:
  - `loaders/ozon_fbs_orders_loader.py`
  - `loaders/ozon_fbo_orders_loader.py`

Для fallback используются:

- `orders_qty`
- `orders_amount_seller`

Это проектная метрика, но для точной reconciliation organic рекомендуемый source — именно `seller_analytics`.

### Ad-attributed orders / revenue

Источник — Ozon Performance reports:

- CPC: `POST /api/client/statistics/json`
- CPO: `GET /api/client/statistics/all_sku_promo/orders/generate`

Из этих отчетов проект парсит SKU/day attribution и сохраняет в:

- `ozon_daily_sku_ad_attribution`

Для CPO project теперь хранит два SKU-измерения:

- `order_sku` — фактически заказанный SKU из колонки `SKU`;
- `promoted_sku` — продвигаемый SKU из колонки `SKU продвигаемого товара`.

Текущая версия не пишет ad-attributed продажи в `marketplace_expenses`. Там остаются только расходы.

Текущая версия использует те поля заказов/выручки, которые отдает сам Ozon report. Это важно:

- рекламные расходы (`ad_spend`, `advertising_clicks`, `advertising_order_5`, ...) не равны рекламным продажам;
- органика считается только из `total - ad_attributed`, а не из `total - ad_spend`.

## Ограничения

- заказы не равны выкупам: organic calculation строится на заказах, а не на реализации;
- расходы на рекламу не равны рекламным продажам;
- таблица attribution поддерживает раздельное хранение:
  - `ad_source`: `cpc` / `cpo`
  - `attribution_type`: `direct` / `associated` / `union` / `unknown`
- organic по SKU считается по `order_sku`, то есть по фактически заказанному товару;
- `promoted_sku` используется только для отдельной рекламной аналитики и диагностики;
- `promoted_sku` и `order_sku` нельзя смешивать в одной organic-формуле без отдельного решения;
- в первой версии расчет `organic` использует только `attribution_type = direct`;
- если в Ozon report позже появятся отдельные associated/union поля, их можно сохранять отдельно без изменения формулы MVP;
- если `ad_orders_* > total_orders_*`, органика режется до `0`, а в `warning` пишется:
  - `ad_orders_exceed_total`
  - `ad_revenue_exceed_total`
- если total source отсутствует, строка получает `calculation_status = missing_total`;
- если ad attribution отсутствует в день, где уже есть advertising expenses, строка получает `calculation_status = missing_ad_attribution`.

## Таблицы

### `ozon_daily_sku_ad_attribution`

Хранит рекламно-атрибутированные метрики по `sale_date + sku + ad_source + attribution_type + campaign_id`.

- `ad_source`: `cpc` / `cpo`
- `attribution_type`: `direct` / `associated` / `union` / `unknown`
- `campaign_id` хранится, если Ozon report его отдает
- `marketplace_sku` и `order_sku` для organic-расчета — это ordered SKU
- `promoted_sku` хранится отдельно для CPO, чтобы не терять связь с продвигаемым товаром

### `ozon_daily_sku_organic`

Хранит итоговый расчет:

- `total_orders_qty`
- `total_orders_revenue`
- `ad_orders_qty`
- `ad_orders_revenue`
- `organic_orders_qty`
- `organic_orders_revenue`
- `ad_share_orders`
- `ad_share_revenue`
- `calculation_status`
- `warning`

### `ozon_daily_sku_total_orders`

Хранит daily total source по SKU:

- `sale_date`
- `marketplace_sku`
- `total_orders_qty`
- `total_orders_revenue`
- `total_revenue_source`

## Как запускать

Обычный расчет из БД:

```bash
python3 reports_ozon_sku_organic.py --date 2026-04-02 --from-db-only
```

Диапазон:

```bash
python3 reports_ozon_sku_organic.py --date-from 2026-04-01 --date-to 2026-04-07 --from-db-only
```

Загрузка total source из Seller Analytics:

```bash
python3 loaders/ozon_sku_total_analytics_loader.py --date 2026-04-02 --dry-run --debug-sample
```

Тестовый прогон без записи:

```bash
python3 reports_ozon_sku_organic.py --date 2026-04-02 --from-db-only --dry-run --debug-sample
```

## Интерпретация полей

- `total_orders_qty` / `total_orders_revenue` — вся дневная база заказов по SKU;
- `total_revenue_source` в organic-расчете не хранится, но внутри расчета используется приоритет:
  - `seller_analytics`
  - `marketplace_orders`
- `ad_orders_qty` / `ad_orders_revenue` — attributed-to-ads часть;
- `organic_orders_qty` / `organic_orders_revenue` — расчетная органика;
- `ad_share_orders` / `ad_share_revenue` — доля ads в total;
- `calculation_status`:
  - `ok`
  - `missing_total`
  - `missing_ad_attribution`
- `warning` — нефатальные аномалии расчета.
