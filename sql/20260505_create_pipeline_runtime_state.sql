create table if not exists public.pipeline_runtime_state (
    state_key text primary key,
    state_type text not null,
    account_signature text null,
    payload jsonb not null,
    expires_at timestamptz null,
    updated_at timestamptz not null default now()
);

create index if not exists idx_pipeline_runtime_state_type_account
    on public.pipeline_runtime_state (state_type, account_signature);

create index if not exists idx_pipeline_runtime_state_expires_at
    on public.pipeline_runtime_state (expires_at)
    where expires_at is not null;
