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

## Почему органики нет в проде: флаг `--skip-organic`

Записано 2026-09-14. Не чинить без отдельного решения владельца.

Живая команда cron-задачи Render содержит `--skip-organic` (снята через
API 2026-09-14). Шаг «Ozon: расчет organic sales по SKU» в проде
**не запускается вовсе**. Это и есть действующая причина того, что
`ozon_daily_sku_organic` пуста за всё после 2026-05-21 и блокер
`ozon_daily_sku_organic_missing` горит каждое утро.

**Откуда флаг.** Коммит `6a9705b` от 2026-09-03 («Turn the organic step
off deliberately instead of by accident») добавил его в код и в CLAUDE.md;
в команду Render его выставил владелец руками тогда же. В git команды
Render нет, логи Render хранятся с 08-28 и смену команды не показывают,
события сервиса фиксируют только билды и запуски — точнее «2026-09-03,
после деплоя `6a9705b` в 17:24 UTC» установить нельзя. Мотив по коммиту:
до этого шаг молчал случайно — гейт `ozon_downstream_allowed` закрывался
от любого хвоста дневного сбора, а в чистую ночь шаг запустился бы и
записал органику, завышенную примерно на четверть: Selected CPO не
собирался с 2026-05-21, а `organic = total_orders − ad_attributed`.
Первая запись заодно сняла бы утренний блокер ровно тогда, когда он
нужнее всего.

**Как это соотносится с записанной причиной.** В `STATUS.md` и в
CLAUDE.md §6 причиной названа «гейт требовал пустого исторического
бэклога, снят в `34ea2e1`». Это причина **до 2026-09-03**: гейт снят
в тот день, и с тех пор шаг держит только флаг. С 09-03 органика не
считается **намеренно**, а не из-за поломки; формулировка в STATUS.md
устарела.

**Условие снятия**, записанное в help флага: «пока Selected CPO не
собран; убрать в том же изменении, что начинает его собирать». Selected
CPO собран 2026-09-05 (104 даты, `ozon_selected_cpo_daily_rollout.md`) —
условие выполнено, флаг стоит девять дней сверх него. Снятие — отдельное
решение: вместе с ним нужен пересчёт 2026-05-22 … сегодня (CLAUDE.md
§9-6) и разбор трёх определений рекламных заказов (§9-3), иначе органика
сразу поедет на неверной базе.

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
