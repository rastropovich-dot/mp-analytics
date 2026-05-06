create table if not exists ozon_product_identity (
    identity_key text primary key,
    marketplace_code text not null default 'ozon',

    article text,
    offer_id text,
    product_id text,
    ozon_sku text,
    fbo_sku text,
    fbs_sku text,

    decision_marketplace_sku text,
    product_name text,
    visibility text,
    product_status text,
    archived boolean,
    has_fbo_stocks boolean,
    has_fbs_stocks boolean,

    source text,
    evidence jsonb,
    updated_at timestamptz not null default now()
);

create unique index if not exists ux_ozon_product_identity_product_id
on ozon_product_identity (marketplace_code, product_id)
where product_id is not null;

create index if not exists ix_ozon_product_identity_article
on ozon_product_identity (marketplace_code, article);

create index if not exists ix_ozon_product_identity_offer_id
on ozon_product_identity (marketplace_code, offer_id);
