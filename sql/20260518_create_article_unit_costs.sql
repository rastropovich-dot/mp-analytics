create table if not exists public.article_unit_costs (
    id bigserial primary key,
    marketplace_code text not null,
    article text not null,
    unit_cost numeric not null,
    currency text not null default 'RUB',
    cost_source text not null default 'manual',
    valid_from date not null,
    valid_to date null,
    comment text null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists idx_article_unit_costs_marketplace_article_valid_from
    on public.article_unit_costs (marketplace_code, article, valid_from);

create index if not exists idx_article_unit_costs_lookup
    on public.article_unit_costs (marketplace_code, article, valid_from desc, valid_to);
