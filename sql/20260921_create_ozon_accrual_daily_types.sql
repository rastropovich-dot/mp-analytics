-- Леджер начислений Ozon по типам: «дата × тип начисления → сумма».
--
-- Зачем: лист по выкупам (scripts/report_ozon_month.py) собирался из двух
-- источников — статьи из marketplace_expenses, а типы 41 + 54 (реклама),
-- 1 (эквайринг), 51 (подписка Premium), 96 (сбор отзывов), 25 и 10
-- (компенсации) — из файлов сырья на диске: в базу 41 + 54 и 25 / 10 не
-- пишутся вовсе, а 1, 51, 96 растворены в статьях. marketplace_expenses под
-- это не переделываем (290 тысяч строк с ключом по статье).
--
-- Что пишется: одна строка на (дата начисления, тип). ВСЕ типы, без
-- классификации и без else: тип хранится числом, статья — дело читателя
-- (loaders/ozon_finance_accrual.TYPE_TO_EXPENSE). Нулевая сумма тоже пишется:
-- «тип был, но свернулся в ноль» отличается от «типа не было».
--
-- ЗНАК — как отдаёт Ozon: списание с продавца < 0, начисление продавцу > 0.
-- Никакого abs() и никакого «расход положителен»: знак меняет читатель.
-- (В marketplace_expenses знак перевёрнут — расход положителен. Не путать.)
--
-- Источник — тот же ответ /v1/finance/accrual/by-day, что загрузчик расходов
-- получает каждую ночь; ни одного дополнительного обращения к API. Комиссия
-- продажи (posting.products[].commission.sale_commission) — не услуга, type_id
-- у неё нет, в леджер не входит; она живёт в marketplace_buyouts.
--
-- Без SKU: лист дневной, реклама по SKU берётся из Performance API
-- (решение владельца 5 от 2026-09-19).
--
-- Применять руками (SQL-редактор / MCP) по слову владельца. Ночная запись —
-- loaders/ozon_expenses_loader.py; посев истории —
-- scripts/seed_ozon_accrual_daily_types.py.

create table if not exists public.ozon_accrual_daily_types (
    accrual_date  date          not null,   -- день начисления, как в by-day (поле date)
    type_id       integer       not null,   -- тип начисления Ozon
    type_name     text,                     -- имя из /v1/finance/accrual/types на момент записи; справочно
    amount        numeric(14,2) not null,   -- Σ accrued.amount СО ЗНАКОМ Ozon: списание < 0, начисление продавцу > 0
    lines         integer       not null,   -- сколько строк услуг легло в сумму
    loaded_at     timestamptz   not null default now(),
    primary key (accrual_date, type_id),
    constraint ozon_accrual_daily_types_lines check (lines > 0)
);

comment on table public.ozon_accrual_daily_types is
    'Начисления Ozon по дням и типам из accrual/by-day. amount — со знаком Ozon (списание < 0); статья по типу — у читателя.';

create index if not exists idx_ozon_accrual_daily_types_type
    on public.ozon_accrual_daily_types (type_id, accrual_date);
