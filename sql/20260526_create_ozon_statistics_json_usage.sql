CREATE TABLE IF NOT EXISTS ozon_performance_statistics_json_usage (
    id bigserial PRIMARY KEY,
    event_at timestamptz NOT NULL DEFAULT now(),
    marketplace_code text NOT NULL DEFAULT 'ozon',
    target_date date,
    load_date date,
    mode text,
    batch_index int,
    campaign_units int,
    campaign_ids jsonb,
    http_status int,
    response_kind text,
    retry_after_seconds int,
    account_signature text,
    report_uuid text,
    raw_error_preview text,
    created_at timestamptz NOT NULL DEFAULT now()
);
