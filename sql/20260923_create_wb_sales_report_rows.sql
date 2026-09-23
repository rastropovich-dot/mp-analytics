-- Детализация отчётов реализации Wildberries: одна строка на операцию (rrdId).
--
-- Зачем: правая часть листа «WB - месяц» (комиссия факт, логистика, эквайринг,
-- хранение, штрафы, возмещения и их НДС) живёт только в
-- POST finance-api /api/finance/v1/sales-reports/detailed; в marketplace_buyouts
-- лежит один оборот, в marketplace_expenses по WB — ноль строк (2026-09-22).
-- Карта колонок листа на поля отчёта — docs/wb_report_model.md.
--
-- Зерно — строка операции (rrdId), как отдаёт WB: ни агрегатов, ни классификации.
-- Проверено 2026-09-23: у закрытой недели rrdId и значения одни и те же в
-- недельном (period=weekly) и в ежедневных (period=daily) отчётах — 5 979 из
-- 5 979 строк 09-01…09-07; повторный съём через сутки — те же строки, поле в
-- поле. Незакрытая неделя отдаётся только ежедневными отчётами (день D создаётся
-- D+1), поэтому загрузчик всегда ходит с period=daily.
--
-- Деньги — как в ответе, без округления (numeric без точности): vw у WB несёт
-- 16 знаков после запятой, retailPriceWithDisc — целые рубли. Знак — как в
-- ответе (возврат — отдельная операция «Возврат» с положительными суммами;
-- оборот нетто = Продажа − Возврат, это дело читателя).
--
-- Применять руками (SQL-редактор / MCP) по слову владельца. Ночная запись —
-- loaders/wb_sales_report_loader.py; история с 2026-02-01 —
-- scripts/wb_sales_report_backfill.py (только по слову, после --plan).

create table if not exists public.wb_sales_report_rows (
    rrd_id                   bigint        primary key,          -- rrdId — ID строки отчёта, стабилен между daily и weekly
    report_id                bigint        not null,             -- reportId ежедневного отчёта (6338020260921 = день 2026-09-21)
    report_period            text          not null default 'daily',
    report_date_from         date          not null,             -- dateFrom / dateTo отчёта
    report_date_to           date          not null,
    report_create_date       date,                               -- createDate — когда отчёт сформирован (обычно день + 1)
    rr_date                  date          not null,             -- rrDate — «Дата операции»; у владельца это «Дата реализации»
    sale_dt                  timestamptz,                        -- saleDt — дата и время продажи
    order_dt                 timestamptz,                        -- orderDt — дата и время заказа
    nm_id                    bigint,                             -- nmId — артикул WB
    vendor_code              text,                               -- vendorCode — наш артикул, WB отдаёт в нижнем регистре
    tech_size                text,                               -- techSize — размер ('0' у безразмерных)
    srid                     text,                               -- srid — ID заказа (тот же, что в supplier/orders и sales)
    shk_id                   bigint,                             -- shkId — штрихкод единицы
    doc_type                 text,                               -- docTypeName: Продажа / Возврат / ''
    seller_oper_name         text          not null,             -- sellerOperName — «Обоснование для оплаты»
    bonus_type_name          text,                               -- bonusTypeName — вид доставки / штрафа / корректировки
    office_name              text,                               -- officeName — склад
    quantity                 integer,                            -- quantity
    retail_price_with_disc   numeric,                            -- retailPriceWithDisc — цена продавца со скидкой (оборот листа)
    retail_amount            numeric,                            -- retailAmount — что заплатил покупатель
    for_pay                  numeric,                            -- forPay — к перечислению продавцу
    ppvz_sales_commission    numeric,                            -- ppvzSalesCommission
    commission_percent       numeric,                            -- commissionPercent
    ppvz_reward              numeric,                            -- ppvzReward — возмещение за выдачу и возврат на ПВЗ
    rebill_logistic_cost     numeric,                            -- rebillLogisticCost — возмещение издержек перемещения
    delivery_service         numeric,                            -- deliveryService — услуги по доставке (логистика листа)
    delivery_amount          integer,                            -- deliveryAmount — количество доставок
    return_amount            integer,                            -- returnAmount — количество возвратов
    acquiring_fee            numeric,                            -- acquiringFee — эквайринг
    acquiring_percent        numeric,                            -- acquiringPercent
    paid_storage             numeric,                            -- paidStorage — хранение
    penalty                  numeric,                            -- penalty — штрафы
    additional_payment       numeric,                            -- additionalPayment — корректировка ВВ
    deduction                numeric,                            -- deduction — удержания
    paid_acceptance          numeric,                            -- paidAcceptance — операции на приёмке
    vw                       numeric,                            -- vw — вознаграждение WB без НДС
    vw_nds                   numeric,                            -- vwNds — НДС с вознаграждения
    cashback_discount        numeric,                            -- cashbackDiscount — компенсация скидки по лояльности (доход)
    cashback_amount          numeric,                            -- cashbackAmount
    cashback_commission_change numeric,                          -- cashbackCommissionChange
    observed_at              timestamptz   not null,             -- момент сбора, подтвердившего строку
    loaded_at                timestamptz   not null default now()
);

comment on table public.wb_sales_report_rows is
    'Детализация отчётов реализации WB (finance-api sales-reports/detailed, period=daily), зерно rrdId. Суммы и знак — как в ответе WB. Лист: docs/wb_report_model.md.';

create index if not exists idx_wb_sales_report_rows_rr_date
    on public.wb_sales_report_rows (rr_date);

create index if not exists idx_wb_sales_report_rows_rr_date_nm
    on public.wb_sales_report_rows (rr_date, nm_id);

create index if not exists idx_wb_sales_report_rows_report
    on public.wb_sales_report_rows (report_id);
