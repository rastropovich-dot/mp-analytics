# Спецификации Wildberries API

OpenAPI-спеки Wildberries, 15 файлов, формат YAML, OpenAPI 3.0.x.

Первоисточник — портал `dev.wildberries.ru`, файлы отдаются по адресам вида

    https://dev.wildberries.ru/api/swagger/yaml/ru/<файл>.yaml?region=ru

(например `…/ru/12-reports.yaml?region=ru`). Портал закрыт антиботом WBAAS
для машинных запросов из облаков и CI; с обычной машины через браузер или
`curl` обычно отдаёт. Этот набор снят **не напрямую**, а из открытого
зеркала `github.com/eslazarev/wildberries-sdk` (коммит `a2c3436`,
2026-09-19 07:23 UTC), которое качает те же адреса headful-Chrome и
перекладывает файлы как есть. Проверка на месте: скачать 12-reports.yaml с
`dev.wildberries.ru` по адресу выше и сравнить с копией отсюда — `diff`
должен быть пуст; если нет — заменить все файлы прямыми и переписать
эту строку. **Проверено 2026-09-22 16:1x UTC:** `12-reports.yaml` и
`13-finances.yaml` сняты с `dev.wildberries.ru` по адресам выше (из встроенного
браузера: `curl` с Мака получает 498 от антибота даже с браузерными
заголовками) — sha256 совпали с копиями отсюда байт в байт
(`5024a559…7566b`, `1df88047…fdf24`), diff пуст, набор доверенный.
Рядом `CHANGELOG-mirror.md` — журнал изменений спек, который
ведёт зеркало (последняя запись 2026.09.19).

Спека говорит, что метод обещает. Как он ведёт себя на самом деле — чат
разработчиков WB, когда появится в `knowledge/`.

## Наши

    12-reports.yaml          statistics-api: /api/v1/supplier/orders, /api/v1/supplier/sales (flag=0/1),
                             /api/v1/supplier/stocks (отключён), paid_storage, acceptance_report,
                             warehouse_remains, deductions
    11-analytics.yaml        seller-analytics-api: stocks-report/wb-warehouses (замена остатков),
                             sales-funnel, nm-report, order-feed
    13-finances.yaml         finance-api: /api/finance/v1/sales-reports/detailed — детализации отчётов
                             реализации за период (данные с 2024-01-29, лимит 1 запрос/мин),
                             acquiring/* — издержки на приём платежей, account/balance, documents.
                             Прежнего /api/v5/supplier/reportDetailByPeriod в спеке НЕТ.
    08-promotion.yaml        advert-api: кампании, /adv/v3/fullstats и /adv/v1/stats — статистика рекламы,
                             /adv/v1/upd — история затрат, budget, payments
    10-rates.yaml            тарифы: комиссии, логистика, хранение (common-api)
    01-general.yaml          общее: ping, новости, seller-info

## Остальное

    02-items.yaml            карточки товаров, цены и скидки
    03-orders-fbs.yaml       сборочные задания FBS (marketplace-api)
    04-orders-dbw.yaml, 05-dbs.yaml, 05-orders-dbs.yaml, 06-in-store-pickup.yaml, 07-orders-fbw.yaml
                             прочие схемы доставки и поставки
    09-communications.yaml   отзывы, вопросы, чат с покупателями
    14-wbd.yaml              Wildberries Цифровой

## Сводка

    файл                     openapi  путей  название                     серверы (без sandbox)               sha256[:12]
    01-general.yaml          3.0.1      10  Общее                        common-api.wildberries.ru, feedbacks-api.wildberries.ru, use 897265cea893
    02-items.yaml            3.0.1      45  Работа с товарами            content-api.wildberries.ru, discounts-prices-api.wildberries f97acd4ec63c
    03-orders-fbs.yaml       3.0.1      39  Заказы FBS                   marketplace-api.wildberries.ru                               c03edac93d07
    04-orders-dbw.yaml       3.0.1      16  Заказы DBW                   marketplace-api.wildberries.ru                               e258ca076fa8
    05-dbs.yaml              3.0.1      21  DBS                          marketplace-api.wildberries.ru                               ada367f30ee7
    05-orders-dbs.yaml       3.0.1      20  Заказы DBS                   marketplace-api.wildberries.ru                               bffe0e945f60
    06-in-store-pickup.yaml  3.0.1      18  Самовывоз                    marketplace-api.wildberries.ru                               ac46771f1626
    07-orders-fbw.yaml       3.0.1      11  Поставки FBW                 supplies-api.wildberries.ru                                  58de3c549187
    08-promotion.yaml        3.0.1      39  Маркетинг и продвижение      advert-api.wildberries.ru, advert-media-api.wildberries.ru,  faa53197204d
    09-communications.yaml   3.0.1      21  Общение с покупателями       buyer-chat-api.wildberries.ru, feedbacks-api.wildberries.ru, c085dd2fb294
    10-rates.yaml            3.0.0       5  Тарифы                       common-api.wildberries.ru                                    f4e2fec178ff
    11-analytics.yaml        3.0.1      19  Аналитика и данные           seller-analytics-api.wildberries.ru                          07126ee3ae68
    12-reports.yaml          3.0.1      23  Отчёты                       seller-analytics-api.wildberries.ru, statistics-api.wildberr 5024a5599373
    13-finances.yaml         3.0.1      11  Документы и бухгалтерия      documents-api.wildberries.ru, finance-api.wildberries.ru     1df8804772fc
    14-wbd.yaml              3.0.1      19  Wildberries Цифровой         devapi-digital.wildberries.ru                                b8dc1eea23c1
