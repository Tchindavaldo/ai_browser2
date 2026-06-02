-- Migration 004 — colonne transactions.provider_transaction_id
-- L'identifiant de transaction côté provider (DigiKUNTZ `id`, distinct du
-- transaction_ref). Sert au webhook (callback serveur DigiKUNTZ) à retrouver la
-- transaction depuis le payload {id, status, data}, et au polling statut.
-- Idempotent : exécutable sans risque même si déjà appliquée.

alter table transactions
    add column if not exists provider_transaction_id text;

create index if not exists transactions_provider_id_idx
    on transactions (provider_transaction_id);
