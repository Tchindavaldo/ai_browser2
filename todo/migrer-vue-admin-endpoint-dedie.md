# TODO — Migrer la vue admin/debug vers un endpoint dédié (option 2)

> ⚠️ **PRIORITÉ HAUTE — l'app est désormais DÉPLOYÉE et PUBLIQUE sur Fly**
> (`https://mobilwallet-backend.fly.dev`). Les endpoints admin sont seulement
> *masqués* dans Swagger (`include_in_schema=False`), **pas protégés** : quiconque
> connaît l'URL accède à l'historique des transactions, aux traces IA, aux
> templates curl et aux payloads (via les routes admin ou `?debug=true`).
>
> **Décision retenue (option 1) :** protéger par une **clé admin statique**
> (`ADMIN_API_KEY` en secret Fly) + header `X-Admin-Key`, vérifiée par une
> dépendance FastAPI `require_admin`. C'est le mécanisme le plus simple et
> suffisant ici. À implémenter sur une branche dédiée.
>
> Étapes minimales (clé admin) :
> 1. Lire `ADMIN_API_KEY` dans `core/config.py` (secret Fly, jamais en dur).
> 2. Dépendance `require_admin` (compare le header `X-Admin-Key`, 401/403 sinon).
> 3. L'appliquer à TOUTES les routes admin (`/transactions*`, `/templates*`,
>    `/drive`) ET au paramètre `?debug=true` de `/pay`.
> 4. Documenter le security scheme `apiKey` (header) dans Swagger pour le tag
>    `admin`, puis régénérer (skill update-swagger).

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
