# ARCHITECTURE — MobileWallet backend (vision 360)

> **À lire en début de session** pour connaître le projet sans tout parcourir.
> Si tu modifies la structure (nouveau fichier/module/endpoint/table) ou un flux,
> **mets ce fichier à jour** (ainsi que le README) avant de clore le travail.

## En une phrase

Backend FastAPI qui exécute des paiements **Mobile Money** (XAF, Cameroun) via des
**agrégateurs** (DigiKUNTZ aujourd'hui). Deux moteurs par agrégateur : un **navigateur
IA** (Playwright + LLM DeepSeek qui pilote le checkout Flutterwave) et un **curl replay**
(rejoue la requête déduite, sans navigateur).

## Principe directeur (NON négociable — cf. CLAUDE.md)

**L'IA décide, le code informe.** Le backend fournit à l'IA un snapshot fidèle + des
outils + un objectif ; il ne décide JAMAIS à sa place qu'un paiement a réussi/échoué.
Seule exception : les faits opérateur universels et mécaniques (timeout 17 min → `expired`).

---

## Carte des fichiers

```
ai_browser2/
├── main.py                       Lanceur uvicorn (importe core.server:app). RELOAD=1 = hot-reload.
├── start.sh / dev.sh             Démarrage prod (sans reload) / dev (avec reload).
│                                 ⚠️ Tester /pay avec start.sh : le hot-reload coupe les requêtes longues.
│
├── core/                         === MOTEUR GÉNÉRIQUE (agnostique de l'agrégateur) ===
│   ├── server.py                 FastAPI : endpoints, lifespan (pool navigateur + LLM), routing /pay,
│   │                             garde anti-doublon par numéro, vue client (PayResponse) vs
│   │                             admin (PayResponseDebug via ?debug=true), transform 503 upstream.
│   ├── base.py                   Contrat `Aggregator` (ABC) + dataclasses PaymentRequest /
│   │                             PaymentResult (porte error_code, curl_template, errors…) / CurlTemplate.
│   ├── registry.py               Registre nom -> instance d'agrégateur (register / get / names).
│   ├── config.py                 settings centralisés depuis .env (DigiKUNTZ, FLW, LLM, Supabase,
│   │                             retry_window_s=1020s, max_tabs_per_browser).
│   ├── db.py                     Couche Supabase async (no-op si non configuré) : transactions,
│   │                             curl_templates, transaction_traces, transaction_errors, app_settings.
│   ├── browser.py                BrowserSession (1 transaction = 1 contexte isolé : page, capture
│   │                             réseau, frame active, snapshot, actions, wait_for_page_change) +
│   │                             BrowserController (POOL : acquire/release_session, N sessions par
│   │                             Chrome puis 2e Chrome). CapturedRequest, DomSnapshot.
│   ├── browser_runner.py         Orchestration navigateur générique : acquiert une session, crée
│   │                             la transaction, navigue, lance ReasoningLoop, délègue le verdict à
│   │                             l'agrégateur, capture charge/verify, construit le curl_template.
│   ├── reasoning_loop.py         Boucle IA : snapshot -> prompt -> LLM -> actions. Gère await_change
│   │                             (attente passive bornée par le TEMPS, pas par max_turns), plafond
│   │                             17min (deadline_exceeded injecté dans le header).
│   ├── llm_client.py             Client DeepSeek (LlmClient.send).
│   ├── classifier.py             Classification statut par mots-clés (apprend de nouveaux mots).
│   ├── error_tracking.py         build_errors() : dérive les lignes transaction_errors (engine + source).
│   ├── upstream_errors.py        Détection panne amont -> codes network_unavailable /
│   │                             operator_unavailable + messages FR (réponse 503 propre au client).
│   └── crypto/                   encrypt.js (cryptico via Node) + cryptico_py.py — chiffrement charge.
│
├── aggregators/                  === UN DOSSIER PAR AGRÉGATEUR ===
│   └── digikuntz/
│       ├── aggregator.py         DigikuntzAggregator : implémente l'ABC, s'enregistre, replay (steps
│       │                         + ReplayConfig par appel), extract_curl_template, matchers.
│       ├── browser_flow.py       Mode navigateur : browser_objective (prompt FR), decide_browser_outcome
│       │                         (recueille le verdict de l'IA ; expired au timeout), _interpret/_friendly.
│       └── replay_flow.py        Mode curl replay : step1..step4 (create/init/charge/poll verify),
│                                 ReplayConfig (config par appel, pas de globals mutés), interpret_*.
│
├── schema/
│   ├── supabase.sql              Schéma de base : transactions, curl_templates.
│   └── migrations/               Évolutions idempotentes, une par fichier :
│       ├── 001_transaction_traces.sql   Trace tour-par-tour de l'IA.
│       ├── 002_app_settings.sql         Réglages clé/valeur (ex. max_tabs_per_browser).
│       └── 003_transaction_errors.sql   Erreurs détaillées par moteur + source.
│
├── docs/openapi.json             Swagger versionné (régénérer via scripts/dump_openapi.py).
├── scripts/dump_openapi.py       Dump du schéma OpenAPI.
├── todo/                         Chantiers planifiés (migration vue admin, fallback poll DigiKUNTZ).
├── README.md                     Doc d'usage (install, endpoints, modes, persistance).
├── ARCHITECTURE.md               CE fichier (vision 360).
└── CLAUDE.md                     Règles projet (branches, philosophie IA, secrets, swagger, BD).
```

---

## Le flux d'un paiement `POST /pay`

1. **server.py** valide (agrégateur, réseau), applique la **garde anti-doublon** par numéro :
   - dernière transaction `pending` → 409 (confirmer/annuler) ;
   - dernière `cancelled` < 17min → 409 retry_too_soon ;
   - `failed`/`expired`/panne → relançable tout de suite.
2. Insère la transaction `pending`, dispatch selon `mode` (auto/browser/replay) vers
   l'agrégateur (`pay_via_browser` ou `replay`).
3. **browser** : `run_browser_flow` acquiert une **session isolée**, crée la transaction
   DigiKUNTZ, ouvre le checkout Flutterwave, lance la **boucle IA** (remplir + Payer), puis
   l'IA **reste dans la boucle** pour attendre la validation USSD (`await_change`, bornée
   par 17min) et conclut elle-même. `decide_browser_outcome` recueille ce verdict.
4. **replay** : `step1..step4` rejouent charge + poll verify (17min), `ReplayConfig` par appel.
5. **finally** : settle la transaction en BD (+ trace + errors), construit la réponse.
   - panne amont (API/opérateur) → **503** `{code, message}` (vue uniforme client) ;
   - sinon **vue client** minimale (success/status/message/transaction_id/code) ; `?debug=true`
     ajoute le détail technique (admin).

## Statuts finaux possibles

`successful` · `failed` (refus/solde insuffisant) · `cancelled` (USSD refusé) ·
`expired` (17min écoulées, relançable) · `pending` (en cours) ·
`network_unavailable` / `operator_unavailable` (pannes amont → 503).

## Concurrence

- **Navigateur** : 1 `BrowserSession` = 1 `BrowserContext` isolé (cookies/onglets séparés) ;
  fermer une session n'affecte jamais les autres. Pool : `max_tabs` sessions par Chrome,
  puis un nouveau Chrome. Seuil réglable (env `MAX_TABS_PER_BROWSER` + BD app_settings +
  API `PUT /config/max-tabs`).
- **Replay** : `ReplayConfig` construit par appel (aucun global de module muté) → N replays
  parallèles sans interférence.

## Points d'entrée de code utiles

- Ajouter un agrégateur → `core/base.py` (l'ABC) + `aggregators/<nom>/`.
- Toucher la décision de l'IA → `core/reasoning_loop.py` + `aggregators/*/browser_flow.py`
  (objectif + decide_browser_outcome). **Ne jamais y remettre de "code qui décide".**
- Toucher la persistance → `core/db.py` (+ une migration dans `schema/migrations/`).
- Toucher les endpoints/modèles → `core/server.py` puis régénérer `docs/openapi.json`.
