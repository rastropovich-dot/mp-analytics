# `/v1/finance/realization/by-day`: что даёт, границы, схема, сбор

Написано 2026-09-14 по живым вызовам (спека + опыт), см. `docs/outbox.md`,
седьмая задача. Таблица `ozon_realization_by_day` создаётся миграцией
`sql/20260915_create_ozon_realization_by_day.sql`, собирает
`scripts/load_ozon_realization_by_day.py`.

## Зачем

Единственный наш источник, где есть:

- **штуки** (`quantity`), а не позиции начислений — accrual/by-day количества
  не отдаёт, и маржа 2026-09-14 считалась по позициям с оговоркой ±3 %;
- разбивка **`bonus`, `bank_coinvestment`, `standard_fee`, `compensation`,
  `stars`, `pick_up_point_coinvestment`** — ни в accrual, ни в старом
  transaction/list её нет;
- продажи и возвраты **раздельно** (`delivery_commission` / `return_commission`).

Тождества, снятые на 31.08 (210 блоков, 0 расхождений):

```
amount = price_per_instance × quantity
total  = amount + bonus + bank_coinvestment + stars + pick_up_point_coinvestment
         + compensation − standard_fee − commission
seller_price_per_instance = (amount + bonus + bank_coinvestment) / quantity   (у продаж)
amount + bonus + bank_coinvestment, нетто продажи − возвраты
       = выручка выкупов из accrual (marketplace_buyouts) за день:
         31.08 — 4 123 667,00 = 4 123 667,00
bonus нетто за 31.08 = 2 060 322,35 — число из кабинета
```

Скрипт проверяет первые два тождества на каждой записи и падает при
расхождении; последнее печатает сверкой по датам после записи.

## Границы — опытом, не только спекой

Спека: «не более чем за 32 календарных дня от текущей даты», отчёт за день
готов не сразу. Пробы 2026-09-14 (день 09-14 по МСК):

```
2026-08-12   400 code 3  the requested date must be no earlier than 32 days before the current day
2026-08-13   400 code 3  то же — хотя 09-14 − 32 = 08-13; граница на день строже текста
2026-08-14   200         286 строк
2026-08-31   200         209 строк, 173 SKU
2026-09-13   200         253 строки, 207 SKU
2026-09-14   404 code 5  Report for the requested date not found — день не закрыт
```

**Доступно 31 дата: 08-14 … 09-13.** Каждые сутки в 00:00 самая ранняя дата
пропадает навсегда. Ничего старше в этом методе нет: март–июль этим
источником не закрыть никогда.

## Лимиты — что известно, чего нет

В спеке у метода ни слова о лимитах; пометка «не расходует лимиты» в
seller-спеке стоит у одного метода (`/v1/delivery/check`), у этого — нет,
значит «неизвестно», а не «бесплатно». В ответах есть заголовок
`ratelimit-remaining`, других заголовков лимита нет:

```
вызов 1 → 44,  вызов 2 (через 2 с) → 43,  вызов 3 (через 2 с) → 41,
вызов 4 (через минуту) → 40,  повтор через 65 с → 46,
через 15 минут без наших вызовов → 35
```

Счётчик не восполняется посекундно, но и не только наш: за 15 минут без
вызовов он упал с 46 до 35 — окно и потолок неизвестны, счётчик общий на
аккаунт. 31 запрос с паузой 1,5 с укладывается в любое разумное окно;
на 429 скрипт ждёт 60 с, до трёх попыток, число пауз печатает. Один
вызов — 0,1 … 0,8 с, 100 … 150 КБ.

## Схема — `ozon_realization_by_day`

Единица хранения — **одна сторона одной строки отчёта**:

```
ключ (realization_date, row_number, side)     side: 'sale' | 'return'
sku, offer_id, offer_id_norm (lower), barcode, product_name
seller_price_per_instance, commission_ratio
price_per_instance, quantity, amount, bonus, commission, compensation,
standard_fee, bank_coinvestment, stars, pick_up_point_coinvestment, total
raw_row jsonb (строка ответа целиком), source, fetched_at, loaded_at
```

Почему не по SKU: один SKU бывает в отчёте несколько раз с разной ценой
или долей комиссии (31.08 — 173 SKU на 209 строк, до 4 строк на SKU).
Почему `row_number` — стабилен только внутри одного ответа, поэтому
**дата при записи перезаписывается целиком** (delete + insert), а не
upsert по номеру. Почему две записи вместо двух блоков в одной: суммы по
продажам и возвратам считаются фильтром, а не арифметикой над парами
колонок; из 209 строк 31.08 обе стороны заполнены у одной.

## Сбор

```
venv/bin/python3 scripts/load_ozon_realization_by_day.py            # авто-окно, без записи: снять в файлы и показать
venv/bin/python3 scripts/load_ozon_realization_by_day.py --apply    # записать из файлов
venv/bin/python3 scripts/load_ozon_realization_by_day.py --check-only
```

Сырые ответы — в `data/ozon_realization_by_day/<дата>.json` (в `.gitignore`),
`--apply` читает их и не ходит в API повторно; `--refetch` — снять заново.
Печатает: по датам статус, строк, записей, штуки и суммы; итог — дат,
записей, обращений, 429, время, `ratelimit-remaining`; после записи —
сверку с выкупами по множествам SKU и по тождеству выручки.

Дальше: включить в ночной прогон шагом «вчера» (одна дата, одно
обращение) — отдельное решение, после первого сбора.
