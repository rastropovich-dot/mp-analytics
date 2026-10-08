-- Курс 1С по металлам и доллару (сорок восьмая §1): история установок курса из чата «LL Курсы» (бот печатает «Курс 1С» с датой установки).
--
-- Зачем: себестоимость продажи на дату D = СС снимка 1С × (1 + k × Δ курса 1С металла с даты снимка до D) — решение владельца
-- 2026-10-07 вместо индекса 1,150 (loaders/unit_cost_history.rate_adjust). Курс на дату — последняя установка ≤ дате.
-- Единица хранения — установка курса: (metal, set_date). Правка задним числом (одна дата установки — два курса на разных картинках,
-- доллар 20.05.2026 75 → 74) хранится как последний виденный курс, прежний — в примечании плана загрузчика.
--
-- Применять по слову владельца через MCP (как сорок шестая). Посев: scripts/load_metal_rates_1c.py --apply
-- из data/ll_rates/ll_rates_1c_history.csv (scripts/ll_rates_ocr.py, сорок седьмая §1).

create table if not exists public.metal_rates_1c (
    metal       text          not null,   -- gold585 | silver925 | usd (проба 375 считается по золоту: 375/585 ровно, сорок седьмая §2б)
    set_date    date          not null,   -- дата установки курса в 1С (как печатает бот под «Курс 1С»)
    rate        numeric(12,2) not null,   -- курс 1С: ₽ за грамм (золото 585, серебро 925) или ₽ за доллар
    source      text          not null default 'll_bot',   -- откуда: картинки бота чата «LL Курсы»
    seen_from   date          not null,   -- первая картинка, на которой курс виден (бот печатает курс со следующего дня после установки)
    seen_to     date,                     -- последняя картинка, на которой курс ещё виден; у действующего курса — дата последней картинки,
                                          -- по ней читатель считает «курс устарел на N дней» (STALE_DAYS = 7)
    loaded_at   timestamptz   not null default now(),
    primary key (metal, set_date),
    constraint metal_rates_1c_metal check (metal in ('gold585', 'silver925', 'usd')),
    constraint metal_rates_1c_rate_positive check (rate > 0),
    constraint metal_rates_1c_seen_order check (seen_to is null or seen_to >= seen_from)
);

comment on table public.metal_rates_1c is
    'Курс 1С по металлам и доллару из чата «LL Курсы»: установка курса = строка (metal, set_date). Курс на дату — последняя установка ≤ дате. Пишет только scripts/load_metal_rates_1c.py --apply.';

create index if not exists idx_metal_rates_1c_lookup
    on public.metal_rates_1c (metal, set_date desc);
