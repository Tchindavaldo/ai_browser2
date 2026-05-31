-- Migration 001 — table transaction_traces
-- Trace tour-par-tour de l'agent IA (mode browser). Une ligne par tour, liée à
-- sa transaction. Permet de rejouer ce que l'agent a vu / pensé / fait
-- (logs console en direct + GET /transactions/{ref}/trace).
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists transaction_traces (
    id                bigint generated always as identity primary key,
    transaction_id    bigint      not null references transactions (id) on delete cascade,
    turn              integer     not null,
    url               text,
    elements          integer,                         -- nb d'éléments interactifs vus
    thought           text,                            -- raisonnement de l'agent ce tour-là
    actions           jsonb,                           -- liste des actions jouées
    objective_reached boolean     not null default false,
    error             text,                            -- renseigné si le tour a échoué (snapshot/LLM)
    created_at        timestamptz not null default now()
);

create index if not exists transaction_traces_tx_idx
    on transaction_traces (transaction_id, turn);
