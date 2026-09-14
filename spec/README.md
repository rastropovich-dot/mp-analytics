# Спецификации Ozon API

OpenAPI-спеки, снятые из браузера 2026-09-13/14. `docs.ozon.ru` блокирует
машинные запросы — как обновлять, описано в `docs/ozon_spec_refresh.md`.

Спека говорит, что метод обещает. Как он ведёт себя на самом деле —
в `knowledge/telegram/`.

## Наши

Только эти два относятся к работе кабинета продавца. Всё остальное ниже —
чужие роли, держим для справки.

    ozon-seller.json         466 методов, 2154 схемы, v2.1
                             api-seller.ozon.ru
                             заказы, финансы, товары, отчёты, аналитика

    ozon-performance.json     47 методов,   97 схем,  v2.0
                             api-performance.ozon.ru
                             реклама: кампании, ставки, статистика

## Возможно пригодятся

    ozon-retail.json          27 методов, v1.0.0, retail-api.ozon.ru
                              API поставщика Ozon Retail — когда Ozon
                              покупает товар у поставщика. Проверить, имеет
                              ли отношение к нашим выкупам

    ozon-ord.json             45 методов, v1.0.0, api-ord.ozon.ru
                              ОРД — маркировка рекламы. Понадобится, если
                              появится внешняя реклама (у нас есть тип
                              external_promo)

    ozon-fbp.json             23 метода,  v1.2.1, api-fbp.ozon.ru
                              Fulfillment by Partner

## Чужие роли

Перевозчики, логистические операторы, партнёрские сервисы. К продавцу
отношения не имеют.

    ozon-rocket.json                     40 методов  Ozon Rocket
    ozon-ilp.json                        44 метода   API перевозчика
    ozon-logistic-platform.json          47 методов  логистическая платформа
    ozon-logistic-platform-export.json   28 методов  она же, экспорт
    ozon-transport.json                  10 методов  ТК API
    ozon-global-returns.json             16 методов  возвраты Ozon Global
    ozon-comms.json                       2 метода   API партнёра
    ozon-broker.json                      8 методов  Broker API
    ozon-flex.json                        6 методов  Flex API
    ozon-egift-card.json                  6 методов  подарочные сертификаты

## Осторожно: не боевые адреса

У трёх спек в `servers` стоит тестовый контур, не продакшен:

    ozon-rocket.json        api-stg.ozonru.me
    ozon-flex.json          flex-api.stg.a.o3.ru
    ozon-egift-card.json    egift-card-b2b-api-sandbox.stg.a.o3.ru

Если когда-нибудь дойдёт до их использования — адрес брать не из спеки.
