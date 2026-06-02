# TODO — Migrer la vue admin/debug vers un endpoint dédié (option 2)

## Contexte

Les endpoints exposent par défaut une **vue client minimale** (success, status,
message, transaction_id, code). Le détail technique (charge_url, plaintext,
curl_replay, verify_*, captured_requests, error_signals, turns, tokens, trace…)
est réservé à l'**admin/dev backend**.

**État actuel (intérim)** : le détail admin est obtenu via le paramètre
`?debug=true` sur les endpoints. Ce paramètre est **exclu du schéma OpenAPI**
(`include_in_schema=False`) donc invisible dans Swagger pour le dev client, mais
exploitable par l'admin qui l'ajoute manuellement à l'URL.

## Pourquoi migrer

`?debug=true` est un compromis :
- pas de vraie protection (n'importe qui qui connaît le paramètre y accède) ;
- mélange client et admin sur la même route ;
- Swagger ne peut pas masquer un paramètre selon le rôle (schéma statique).

## Cible (option 2)

- `/pay` (et les autres) ne renvoient **QUE** la vue client, point.
- Le détail technique se consulte via un **endpoint admin dédié et protégé** :
  - ex. `GET /admin/transactions/{ref}` (détail complet : trace, capture,
    curl déduit, error_signals…) — la trace et la capture sont déjà en BD
    (tables `transactions`, `transaction_traces`, `transaction_errors`).
  - protégé par **auth** (clé API admin / header `X-Admin-Key`, ou JWT rôle admin).
- Supprimer le paramètre `?debug=true` une fois la migration faite.
- Swagger : grouper ces routes sous un tag `admin` ; documenter l'auth requise
  (security scheme) pour que l'admin authentifié voie les params dans `/docs`.

## Tâches

- [ ] Choisir le mécanisme d'auth admin (clé statique env `ADMIN_API_KEY` la plus simple).
- [ ] Ajouter une dépendance FastAPI `require_admin` (vérifie le header/clé).
- [ ] Créer `GET /admin/transactions/{ref}` (détail complet depuis la BD).
- [ ] Brancher la vue détaillée dessus (réutiliser le modèle PayResponseDebug).
- [ ] Retirer le paramètre `?debug=true` des endpoints publics.
- [ ] Security scheme OpenAPI pour le tag `admin`.
- [ ] Régénérer le swagger (skill update-swagger).
