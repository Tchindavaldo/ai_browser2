-- Migration 003 — table transaction_errors
-- Suivi détaillé des erreurs rencontrées lors d'une transaction, distinguant
-- le MOTEUR (browser/replay) et la SOURCE de l'erreur — crucial côté navigateur
-- où l'échec peut venir de l'IA, du navigateur lui-même, ou de la transaction :
--   source='ai'          : l'agent a mal raisonné / mal agi / n'a pas conclu
--   source='browser'     : form non chargé, iframe détachée, timeout Playwright
--   source='transaction' : le paiement a échoué (solde insuffisant, réseau, refus)
--   source='replay'      : échec du flux curl (réponse Flutterwave / code erreur)
-- Côté replay (déterministe) : en général 1 ligne par transaction échouée.
-- Côté navigateur : potentiellement plusieurs lignes (un signal par source/tour).
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists transaction_errors (
    id              bigint generated always as identity primary key,
    transaction_id  bigint      not null references transactions (id) on delete cascade,
    engine          text        not null,            -- 'browser' | 'replay'
    source          text        not null,            -- 'ai' | 'browser' | 'transaction' | 'replay'
    category        text,                             -- network_down, insufficient_funds, form_load, timeout, ...
    message         text,                             -- message clair (verdict IA ou message Flutterwave)
    detail          jsonb,                            -- error_signals (browser) / charge_response (replay)
    turn            integer,                          -- n° de tour si l'erreur vient d'un tour précis (browser)
    created_at      timestamptz not null default now()
);

create index if not exists transaction_errors_tx_idx
    on transaction_errors (transaction_id);

create index if not exists transaction_errors_cat_idx
    on transaction_errors (engine, source, category);
