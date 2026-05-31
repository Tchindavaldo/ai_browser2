# Consignes projet — MobileWallet backend

Ce fichier est **versionné** : ses règles s'appliquent automatiquement sur tout
PC où le projet est cloné/pull, dans n'importe quelle session Claude Code.

## Convention de branches (OBLIGATOIRE)

Toujours préfixer les branches selon leur nature :

- `debug/<sujet>` — **investigation/résolution d'un bug précis**. Une branche par
  bug. Ex: `debug/browser-form-load`, `debug/browser-network-load`.
- `feature/<sujet>` — nouvelle fonctionnalité ou durcissement d'un module.
  Ex: `feature/browser-engine-hardening`, `feature/curl-replay-hardening`.
- `backup/<sujet>` — sauvegarde d'un état (ne pas y travailler).

Règle: **tout travail de debug commence sur une branche `debug/`**, créée depuis
la branche de feature concernée (pas depuis `main`), pour hériter de son
instrumentation. Ne jamais débuguer directement sur `main` ni sur une `feature/`.

## Tests live (réseau)

Les appels réels (DigiKUNTZ, Flutterwave) doivent être lancés **depuis la machine
de l'utilisateur**, pas depuis l'environnement de l'agent (réseau différent qui
renvoie 502 sur DigiKUNTZ et stalle les assets Flutterwave). L'agent prépare les
commandes `curl`, l'utilisateur les exécute et rapporte le résultat.

## Secrets

`.env` est gitignoré et ne doit JAMAIS être commité. Ne pas hardcoder de secret
dans le code ; tout passe par `core/config.py` (lecture d'environnement).

## Swagger

Après toute modif des endpoints/modèles dans `core/server.py`, régénérer la doc :
`venv/bin/python scripts/dump_openapi.py` (ou la skill `update-swagger`).

## Base de données

- `schema/supabase.sql` = **schéma de base** (tables initiales). Ne pas y empiler
  les évolutions.
- Chaque **évolution de schéma** (nouvelle table, colonne, index) va dans son
  **propre fichier dédié** : `schema/migrations/NNN_description.sql`, numéroté et
  **idempotent** (`if not exists`). Une migration = un fichier.
- L'utilisateur applique les migrations côté Supabase. Ne jamais modifier une
  ligne de la base sans accord explicite de l'utilisateur.
