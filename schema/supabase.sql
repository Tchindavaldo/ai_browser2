-- MobileWallet backend — Supabase schema.
-- Run in the Supabase SQL editor (or via migration) before enabling persistence.

-- Per-payment audit trail.
create table if not exists transactions (
    id              bigint generated always as identity primary key,
    aggregator      text        not null,
    mode            text        not null,            -- 'browser' | 'replay'
    amount          integer     not null,
    phone           text        not null,
    network         text,
    email           text,
    transaction_ref text,
    status          text,                            -- successful|failed|cancelled|pending|unknown
    message         text,
    success         boolean     not null default false,
    charge_response text,
    error_signals   jsonb,
    created_at      timestamptz not null default now()
);

create index if not exists transactions_ref_idx on transactions (transaction_ref);
create index if not exists transactions_created_idx on transactions (created_at desc);

-- Fast lookup for the "one pending payment per number" guard.
create index if not exists transactions_pending_idx
    on transactions (aggregator, phone)
    where status = 'pending';

-- Reusable per-aggregator curl-replay template, deduced by the browser mode.
create table if not exists curl_templates (
    id          bigint generated always as identity primary key,
    aggregator  text        not null unique,
    template    jsonb       not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create or replace function set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists curl_templates_updated_at on curl_templates;
create trigger curl_templates_updated_at
    before update on curl_templates
    for each row execute function set_updated_at();
