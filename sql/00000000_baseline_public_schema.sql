-- базовый снимок схемы public: сгенерировано scripts/db_schema_snapshot.py из снимка karatov 2026-10-07T21:16:32+00:00
-- Объекты, которых датированные файлы sql/ не создают (созданы в панели до 2026-05-05 + дрейф). Применять ПЕРВЫМ, до датированных;
-- транзакцию открывает scripts/db_migrate.py (файл за файлом вместе со строкой schema_migrations).

create sequence if not exists public."ad_costs_id_seq";
create sequence if not exists public."alerts_id_seq";
create sequence if not exists public."daily_marketplace_kpi_id_seq";
create sequence if not exists public."daily_sku_kpi_id_seq";
create sequence if not exists public."intraday_snapshots_id_seq";
create sequence if not exists public."marketplace_buyouts_id_seq";
create sequence if not exists public."marketplace_expenses_id_seq";
create sequence if not exists public."marketplace_orders_analytics_id_seq";
create sequence if not exists public."marketplace_orders_id_seq";
create sequence if not exists public."marketplace_returns_id_seq";
create sequence if not exists public."marketplaces_id_seq";
create sequence if not exists public."prices_daily_id_seq";
create sequence if not exists public."sku_catalog_id_seq";
create sequence if not exists public."sku_costs_id_seq";
create sequence if not exists public."stock_daily_id_seq";

create table if not exists public."ad_costs" (
    "id" bigint default nextval('ad_costs_id_seq'::regclass) not null,
    "ad_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text,
    "article" text,
    "campaign_id" text,
    "campaign_name" text,
    "ad_type" text,
    "impressions" numeric default 0,
    "clicks" numeric default 0,
    "ad_spend" numeric default 0,
    "ad_orders_qty" numeric default 0,
    "ad_orders_amount" numeric default 0,
    "created_at" timestamp with time zone default now(),
    constraint "ad_costs_pkey" PRIMARY KEY (id),
    constraint "ad_costs_ad_date_marketplace_code_marketplace_sku_campaign__key" UNIQUE (ad_date, marketplace_code, marketplace_sku, campaign_id)
);

create table if not exists public."alerts" (
    "id" bigint default nextval('alerts_id_seq'::regclass) not null,
    "alert_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text,
    "article" text,
    "alert_type" text not null,
    "severity" text not null,
    "metric_name" text,
    "metric_value" numeric,
    "threshold_value" numeric,
    "message" text not null,
    "is_resolved" boolean default false,
    "created_at" timestamp with time zone default now(),
    constraint "alerts_pkey" PRIMARY KEY (id)
);

create table if not exists public."daily_marketplace_kpi" (
    "id" bigint default nextval('daily_marketplace_kpi_id_seq'::regclass) not null,
    "kpi_date" date not null,
    "marketplace_code" text not null,
    "orders_qty" numeric default 0,
    "orders_amount_seller" numeric default 0,
    "buyouts_qty" numeric default 0,
    "buyouts_amount_seller" numeric default 0,
    "buyout_rate" numeric default 0,
    "ad_spend" numeric default 0,
    "ad_share_of_orders" numeric default 0,
    "roas" numeric default 0,
    "commission_amount" numeric default 0,
    "logistics_amount" numeric default 0,
    "other_expenses_amount" numeric default 0,
    "gross_margin_amount" numeric default 0,
    "gross_margin_percent" numeric default 0,
    "ebitda_amount" numeric default 0,
    "ebitda_percent" numeric default 0,
    "created_at" timestamp with time zone default now(),
    "ad_orders_qty" numeric default 0 not null,
    "ad_orders_revenue" numeric default 0 not null,
    "organic_orders_qty" numeric default 0 not null,
    "organic_orders_revenue" numeric default 0 not null,
    "ad_share_orders" numeric,
    "ad_share_revenue" numeric,
    "buyout_rate_source" text,
    constraint "daily_marketplace_kpi_pkey" PRIMARY KEY (id),
    constraint "daily_marketplace_kpi_kpi_date_marketplace_code_key" UNIQUE (kpi_date, marketplace_code)
);

create table if not exists public."daily_sku_kpi" (
    "id" bigint default nextval('daily_sku_kpi_id_seq'::regclass) not null,
    "kpi_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "product_name" text,
    "orders_qty" numeric default 0,
    "orders_amount_seller" numeric default 0,
    "buyouts_qty" numeric default 0,
    "buyouts_amount_seller" numeric default 0,
    "buyout_rate" numeric default 0,
    "ad_spend" numeric default 0,
    "ad_share_of_orders" numeric default 0,
    "roas" numeric default 0,
    "commission_amount" numeric default 0,
    "logistics_amount" numeric default 0,
    "other_expenses_amount" numeric default 0,
    "product_cost" numeric default 0,
    "gross_margin_amount" numeric default 0,
    "gross_margin_percent" numeric default 0,
    "ebitda_amount" numeric default 0,
    "ebitda_percent" numeric default 0,
    "created_at" timestamp with time zone default now(),
    "ad_orders_qty" numeric default 0 not null,
    "ad_orders_revenue" numeric default 0 not null,
    "organic_orders_qty" numeric default 0 not null,
    "organic_orders_revenue" numeric default 0 not null,
    "ad_share_orders" numeric,
    "ad_share_revenue" numeric,
    "buyout_rate_source" text,
    constraint "daily_sku_kpi_pkey" PRIMARY KEY (id),
    constraint "daily_sku_kpi_kpi_date_marketplace_code_marketplace_sku_key" UNIQUE (kpi_date, marketplace_code, marketplace_sku)
);

create table if not exists public."intraday_snapshots" (
    "id" bigint default nextval('intraday_snapshots_id_seq'::regclass) not null,
    "snapshot_ts" timestamp with time zone default now() not null,
    "snapshot_date" date not null,
    "snapshot_hour" integer not null,
    "marketplace_code" text not null,
    "data_date" date not null,
    "orders_qty" numeric default 0,
    "orders_amount_seller" numeric default 0,
    "buyouts_qty" numeric default 0,
    "buyouts_amount_seller" numeric default 0,
    "created_at" timestamp with time zone default now() not null,
    constraint "intraday_snapshots_pkey" PRIMARY KEY (id)
);

create table if not exists public."marketplace_buyouts" (
    "id" bigint default nextval('marketplace_buyouts_id_seq'::regclass) not null,
    "buyout_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "product_name" text,
    "buyouts_qty" numeric default 0,
    "buyouts_amount_buyer" numeric default 0,
    "buyouts_amount_seller" numeric default 0,
    "revenue_after_commission_vat" numeric default 0,
    "commission_amount" numeric default 0,
    "vat_amount" numeric default 0,
    "created_at" timestamp with time zone default now(),
    "buyouts_units" integer,
    "bonus_amount" numeric,
    "coinvestment_amount" numeric,
    constraint "marketplace_buyouts_pkey" PRIMARY KEY (id),
    constraint "marketplace_buyouts_buyout_date_marketplace_code_marketplac_key" UNIQUE (buyout_date, marketplace_code, marketplace_sku)
);

create table if not exists public."marketplace_expenses" (
    "id" bigint default nextval('marketplace_expenses_id_seq'::regclass) not null,
    "expense_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text,
    "article" text,
    "expense_type" text not null,
    "expense_amount" numeric default 0,
    "created_at" timestamp with time zone default now(),
    constraint "marketplace_expenses_pkey" PRIMARY KEY (id)
);

create table if not exists public."marketplace_orders" (
    "id" bigint default nextval('marketplace_orders_id_seq'::regclass) not null,
    "order_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "product_name" text,
    "orders_qty" numeric default 0,
    "orders_amount_buyer" numeric,
    "orders_amount_seller" numeric default 0,
    "created_at" timestamp with time zone default now(),
    "order_schema" text,
    "cancelled_orders_qty" numeric default 0 not null,
    "cancelled_orders_amount_buyer" numeric,
    "cancelled_orders_amount_seller" numeric default 0 not null,
    "observed_at" timestamp with time zone,
    constraint "marketplace_orders_pkey" PRIMARY KEY (id),
    constraint "marketplace_orders_cancelled_nonneg" CHECK (((cancelled_orders_qty >= (0)::numeric) AND (cancelled_orders_amount_buyer >= (0)::numeric) AND (cancelled_orders_amount_seller >= (0)::numeric)))
);

create table if not exists public."marketplace_orders_analytics" (
    "id" bigint default nextval('marketplace_orders_analytics_id_seq'::regclass) not null,
    "order_date" date not null,
    "marketplace_code" text not null,
    "orders_qty" numeric default 0,
    "orders_amount" numeric default 0,
    "source" text not null,
    "loaded_at" timestamp with time zone default now(),
    "created_at" timestamp with time zone default now(),
    constraint "marketplace_orders_analytics_pkey" PRIMARY KEY (id),
    constraint "marketplace_orders_analytics_order_date_marketplace_code_so_key" UNIQUE (order_date, marketplace_code, source)
);

create table if not exists public."marketplace_returns" (
    "id" bigint default nextval('marketplace_returns_id_seq'::regclass) not null,
    "return_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "product_name" text,
    "returns_qty" numeric default 0,
    "returns_amount" numeric default 0,
    "reason" text,
    "created_at" timestamp with time zone default now(),
    constraint "marketplace_returns_pkey" PRIMARY KEY (id)
);

create table if not exists public."marketplaces" (
    "id" bigint default nextval('marketplaces_id_seq'::regclass) not null,
    "code" text not null,
    "name" text not null,
    constraint "marketplaces_pkey" PRIMARY KEY (id),
    constraint "marketplaces_code_key" UNIQUE (code)
);

create table if not exists public."prices_daily" (
    "id" bigint default nextval('prices_daily_id_seq'::regclass) not null,
    "price_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "product_name" text,
    "seller_price" numeric,
    "buyer_price" numeric,
    "discount_percent" numeric,
    "coinvest_percent" numeric,
    "min_price" numeric,
    "created_at" timestamp with time zone default now(),
    constraint "prices_daily_pkey" PRIMARY KEY (id),
    constraint "prices_daily_price_date_marketplace_code_marketplace_sku_key" UNIQUE (price_date, marketplace_code, marketplace_sku)
);

create table if not exists public."sku_catalog" (
    "id" bigint default nextval('sku_catalog_id_seq'::regclass) not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "barcode" text,
    "product_name" text,
    "category" text,
    "brand" text,
    "metal" text,
    "stones" text,
    "created_at" timestamp with time zone default now(),
    "updated_at" timestamp with time zone default now(),
    constraint "sku_catalog_pkey" PRIMARY KEY (id),
    constraint "sku_catalog_marketplace_code_marketplace_sku_key" UNIQUE (marketplace_code, marketplace_sku)
);

create table if not exists public."sku_costs" (
    "id" bigint default nextval('sku_costs_id_seq'::regclass) not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "cost_date" date not null,
    "product_cost" numeric,
    "cost_percent_of_min_price" numeric,
    "created_at" timestamp with time zone default now(),
    constraint "sku_costs_pkey" PRIMARY KEY (id),
    constraint "sku_costs_marketplace_code_marketplace_sku_cost_date_key" UNIQUE (marketplace_code, marketplace_sku, cost_date)
);

create table if not exists public."stock_daily" (
    "id" bigint default nextval('stock_daily_id_seq'::regclass) not null,
    "stock_date" date not null,
    "marketplace_code" text not null,
    "marketplace_sku" text not null,
    "article" text,
    "product_name" text,
    "warehouse_name" text,
    "stock_qty" numeric default 0,
    "reserved_qty" numeric default 0,
    "available_qty" numeric default 0,
    "created_at" timestamp with time zone default now(),
    "product_id" text,
    "stock_marketplace_sku" text,
    "decision_marketplace_sku" text,
    "stock_identity_status" text,
    constraint "stock_daily_pkey" PRIMARY KEY (id),
    constraint "stock_daily_stock_date_marketplace_code_marketplace_sku_war_key" UNIQUE (stock_date, marketplace_code, marketplace_sku, warehouse_name)
);
create UNIQUE index if not exists intraday_snapshots_unique_idx ON public.intraday_snapshots USING btree (snapshot_date, snapshot_hour, marketplace_code, data_date);
create UNIQUE index if not exists marketplace_expenses_unique_idx ON public.marketplace_expenses USING btree (expense_date, marketplace_code, marketplace_sku, expense_type);
create UNIQUE index if not exists marketplace_orders_unique_idx ON public.marketplace_orders USING btree (order_date, marketplace_code, marketplace_sku, order_schema);

create or replace view public."v_ozon_orders_daily_by_schema" as
 SELECT order_date,
    order_schema,
    sum(orders_qty) AS orders_qty,
    round(sum(orders_amount_seller), 0) AS orders_amount_seller
   FROM marketplace_orders
  WHERE (marketplace_code = 'ozon'::text)
  GROUP BY order_date, order_schema;
alter table public."ad_costs" enable row level security;
alter table public."alerts" enable row level security;
alter table public."daily_marketplace_kpi" enable row level security;
alter table public."daily_sku_kpi" enable row level security;
alter table public."marketplace_buyouts" enable row level security;
alter table public."marketplace_expenses" enable row level security;
alter table public."marketplace_orders" enable row level security;
alter table public."marketplace_returns" enable row level security;
alter table public."marketplaces" enable row level security;
alter table public."prices_daily" enable row level security;
alter table public."sku_catalog" enable row level security;
alter table public."sku_costs" enable row level security;
alter table public."stock_daily" enable row level security;
grant DELETE, INSERT, SELECT, UPDATE on public."ad_costs" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."ad_costs" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."ad_costs" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."alerts" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."alerts" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."alerts" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."daily_marketplace_kpi" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."daily_marketplace_kpi" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."daily_marketplace_kpi" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."daily_sku_kpi" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."daily_sku_kpi" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."daily_sku_kpi" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."intraday_snapshots" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."intraday_snapshots" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."intraday_snapshots" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_buyouts" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_buyouts" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_buyouts" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_expenses" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_expenses" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_expenses" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_orders" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_orders" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_orders" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_orders_analytics" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_orders_analytics" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_orders_analytics" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_returns" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_returns" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplace_returns" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplaces" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplaces" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."marketplaces" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."prices_daily" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."prices_daily" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."prices_daily" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."sku_catalog" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."sku_catalog" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."sku_catalog" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."sku_costs" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."sku_costs" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."sku_costs" to service_role;
grant DELETE, INSERT, SELECT, UPDATE on public."stock_daily" to anon;
grant DELETE, INSERT, SELECT, UPDATE on public."stock_daily" to authenticated;
grant DELETE, INSERT, SELECT, UPDATE on public."stock_daily" to service_role;

insert into public."marketplaces" ("id", "code", "name") values (1, 'ozon', 'Ozon') on conflict do nothing;
insert into public."marketplaces" ("id", "code", "name") values (2, 'wb', 'Wildberries') on conflict do nothing;
insert into public."marketplaces" ("id", "code", "name") values (3, 'yandex', 'Yandex Market') on conflict do nothing;
