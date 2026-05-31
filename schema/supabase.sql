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

-- Versioned per-aggregator curl-replay templates, deduced by the browser mode.
-- We NEVER overwrite: a new version is appended only when the deduced template
-- differs from the current active one. Rows are grouped per `aggregator`
-- (never mixed); exactly one row per aggregator has is_active = true (the one
-- the replay mode uses). Older versions are kept as history.
create table if not exists curl_templates (
    id          bigint generated always as identity primary key,
    aggregator  text        not null,
    template    jsonb       not null,
    is_active   boolean     not null default true,
    created_at  timestamptz not null default now()
);

create index if not exists curl_templates_aggregator_idx on curl_templates (aggregator);
-- At most one active template per aggregator.
create unique index if not exists curl_templates_active_idx
    on curl_templates (aggregator)
    where is_active;

-- Per-turn AI reasoning trace for a browser-mode payment. One row per turn,
-- linked to its transaction. Lets you replay exactly what the agent saw,
-- thought and did (live console + GET /transactions/{ref}/trace).
create table if not exists transaction_traces (
    id              bigint generated always as identity primary key,
    transaction_id  bigint      not null references transactions (id) on delete cascade,
    turn            integer     not null,
    url             text,
    elements        integer,                         -- nb of interactive elements seen
    thought         text,                            -- the agent's reasoning that turn
    actions         jsonb,                           -- list of action labels taken
    objective_reached boolean   not null default false,
    error           text,                            -- set if the turn failed (snapshot/LLM)
    created_at      timestamptz not null default now()
);

create index if not exists transaction_traces_tx_idx
    on transaction_traces (transaction_id, turn);
