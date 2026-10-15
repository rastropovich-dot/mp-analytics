-- Платное хранение WB по артикулам и дням (WB-18 §2): свёртка строк отчёта seller-analytics-api /api/v1/paid_storage.
--
-- Зачем: владелец хочет разрез «хранение × SKU × день» (10-12); в отчёте реализации хранение лежит дневным итогом без nmId
-- (paid_storage строки «Хранение»). Разведка 2026-10-15 (окно 10-07 … 10-14, 35 021 строка): Σ warehousePrice по дням = paid_storage
-- отчёта реализации за ту же дату до копейки (±0,01 на 3 днях из 8 — округление), без сдвига даты и без НДС.
--
-- Зерно: у строк отчёта нет естественного уникального ключа — на (date, nmId, chrtId, officeId, giId, calcType, barcode) 823 ключа
-- с повторами (тарифные части с разными warehousePrice / barcodesCount) и 261 полный дубль, и день сходится с отчётом реализации
-- только вместе с ними. Поэтому строка таблицы — СВЁРТКА по ключу: Σ warehouse_price, Σ barcodes_count, Σ loyalty_discount,
-- rows_count (сколько строк отчёта свёрнуто); повтор окна (upsert) даёт те же строки. Сырьё — data/wb_paid_storage_raw/<от>_<до>.json.
--
-- Применять только через scripts/db_migrate.py (правило владельца 2026-10-11): план → --apply по слову → --cabinet all.
-- Пишет только loaders/wb_paid_storage_loader.py (ночной шаг «WB: платное хранение», нефатальный, окно 8 дней).

create table if not exists public.wb_paid_storage_daily (
    day                 date            not null,   -- date отчёта: день расчёта (или перерасчёта) хранения
    nm_id               bigint          not null,   -- nmId — артикул WB
    chrt_id             bigint          not null,   -- chrtId — размер артикула
    office_id           integer         not null default 0,    -- officeId склада (сейчас у WB всегда 0)
    gi_id               bigint          not null default 0,    -- giId — поставка (0 — без поставки)
    calc_type           text            not null default '',   -- calcType: «короба: товары свыше базы» / «… ниже базы» / «Сторнирование хранения по складу»
    barcode             text            not null default '',   -- баркод
    size                text,                       -- techSize
    vendor_code         text,                       -- артикул продавца (ключ WB в 1С — без размера)
    subject             text,
    brand               text,
    warehouse           text,                       -- название склада (сейчас «Склад WB РФ»)
    volume              numeric(12,3),              -- объём товара, л
    warehouse_coef      numeric(10,4),              -- коэффициент хранения
    log_warehouse_coef  numeric(10,4),              -- коэффициент логистики и хранения
    warehouse_price     numeric(14,5)   not null,   -- Σ warehousePrice строк ключа, ₽ (сторно — со знаком минус)
    barcodes_count      integer         not null default 0,    -- Σ barcodesCount — штук на тарификации за сутки
    loyalty_discount    numeric(14,5)   not null default 0,    -- Σ loyaltyDiscount, ₽
    rows_count          integer         not null default 1,    -- сколько строк отчёта свёрнуто в эту
    original_date       date,                       -- originalDate: дата первоначального расчёта при перерасчёте
    tariff_fix_date     date,                       -- tariffFixDate
    tariff_lower_date   date,                       -- tariffLowerDate
    task_id             text,                       -- taskId задания, из которого строка
    observed_at         timestamptz     not null,   -- время сбора окна
    loaded_at           timestamptz     not null default now(),
    primary key (day, nm_id, chrt_id, office_id, gi_id, calc_type, barcode)
);

create index if not exists idx_wb_paid_storage_daily_day on public.wb_paid_storage_daily (day);
create index if not exists idx_wb_paid_storage_daily_nm_day on public.wb_paid_storage_daily (nm_id, day);

comment on table public.wb_paid_storage_daily is 'WB платное хранение по дням и артикулам: свёртка строк /api/v1/paid_storage по ключу (день, nmId, chrtId, склад, поставка, способ расчёта, баркод); Σ дня = paid_storage отчёта реализации (WB-18)';
