-- Migration 002 — table app_settings (réglages clé/valeur modifiables via API)
-- Stocke des paramètres d'exécution ajustables sans redéploiement (ex.
-- max_tabs_per_browser : nb max d'onglets par Chrome avant d'en lancer un autre).
-- La valeur fait foi au boot ; l'env ne sert que de défaut si la clé est absente.
-- Idempotent : exécutable sans risque même si déjà appliquée.

create table if not exists app_settings (
    key         text        primary key,
    value       text        not null,
    updated_at  timestamptz not null default now()
);

-- Valeur initiale (no-op si déjà présente).
insert into app_settings (key, value)
values ('max_tabs_per_browser', '20')
on conflict (key) do nothing;
