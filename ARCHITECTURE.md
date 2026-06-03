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
Seule exception : les faits opérateur universels et mécaniques (un échec verify après USSD → `cancelled`).

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
│   │                             retry_window_* par réseau, max_tabs_per_browser).
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
│   │                             garde-fou BROWSER_LOOP_MAX_S (deadline_exceeded dans le header).
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
│       ├── browser_flow.py       Mode navigateur : browser_objective (prompt FR — le navigateur
│       │                         S'ARRÊTE dès l'USSD demandé), decide_browser_outcome (pose
│       │                         poll_after_close, tab fermée), finalize_after_close (polling
│       │                         verify HORS navigateur), _interpret/_friendly.
│       ├── status_poll.py        Polling statut DigiKUNTZ (GET {base}/transaction?transactionId=) :
│       │                         source de vérité du verdict après USSD. payin_* -> interne, poll
│       │                         jusqu'à un verdict terminal (sans timeout : l'opérateur tranche toujours). 0 token.
│       └── replay_flow.py        Mode curl replay : step1..step4 (create/init/charge/poll verify),
│                                 ReplayConfig (config par appel, pas de globals mutés), interpret_*.
│
├── schema/
│   ├── supabase.sql              Schéma de base : transactions, curl_templates.
│   └── migrations/               Évolutions idempotentes, une par fichier :
│       ├── 001_transaction_traces.sql   Trace tour-par-tour de l'IA.
│       ├── 002_app_settings.sql         Réglages clé/valeur (ex. max_tabs_per_browser).
│       ├── 003_transaction_errors.sql   Erreurs détaillées par moteur + source.
│       ├── 004_provider_transaction_id.sql  Id provider (webhook/polling).
│       ├── 005_cancelled_at.sql        Horodate le passage à 'cancelled' (audit).
│       └── 006_ussd_sent_at.sql        Horodate l'envoi USSD (base du calcul anti-doublon).
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
   - dernière `cancelled` dans le délai anti-doublon → 409 retry_too_soon ;
   - `failed`/panne → relançable tout de suite.
2. Insère la transaction `pending`, dispatch selon `mode` (auto/browser/replay) vers
   l'agrégateur (`pay_via_browser` ou `replay`).
3. **browser** : `run_browser_flow` acquiert une **session isolée**, crée la transaction
   DigiKUNTZ, ouvre le checkout Flutterwave, lance la **boucle IA** (remplir + Payer). Dès
   que l'USSD est demandé au client, **le navigateur S'ARRÊTE** (conclut `ussd_sent`, ~6
   tours — pas de surveillance coûteuse de l'écran). `decide_browser_outcome` extrait alors
   les params verify (flw_ref/modalauditid) et les pose sur `result.poll_after_close` SANS
   poller : **la tab est fermée immédiatement** (`release_session`). Ce n'est qu'APRÈS, hors
   session, que `run_browser_flow` appelle `finalize_after_close` -> **polling verify
   Flutterwave** (`status_poll.py`) en HTTP pur, jusqu'au verdict terminal (sans timeout :
   échec après USSD -> `cancelled`). Aucun navigateur n'est tenu pendant l'attente ; le polling ne
   coûte aucun token.
   *(Webhook serveur possible quand le backend a une URL publique — cf.
   `todo/webhook-digikuntz.md`.)*
4. **replay** : `step1..step4` rejouent charge + poll verify (sans timeout), `ReplayConfig` par appel.
5. **finally** : settle la transaction en BD (+ trace + errors), construit la réponse.
   - panne amont (API/opérateur) → **503** `{code, message}` (vue uniforme client) ;
   - sinon **vue client** minimale (success/status/message/transaction_id/code) ; `?debug=true`
     ajoute le détail technique (admin).

## Statuts finaux possibles

- `successful` — payé.
- `failed` — échec AVANT l'USSD (solde insuffisant au /charge, etc.). Relançable.
- `cancelled` — échec APRÈS l'USSD = refus/non-validation, par l'utilisateur OU
  l'opérateur (un solde insuffisant ne déclenche jamais d'USSD). Le polling tourne
  sans timeout jusqu'à ce verdict (l'opérateur tranche toujours). **Bloque** le
  numéro pendant le délai anti-doublon.
- `pending` — en cours.
- `network_unavailable` / `operator_unavailable` — pannes amont → **503**.

(Plus de statut `expired` : le polling n'a plus de timeout, il attend le verdict
terminal de l'opérateur, qui devient `cancelled` en cas d'échec après USSD.)

**Délai anti-doublon DÉPEND du réseau**, réglable par env
(`settings.retry_window_for(network)`, env `RETRY_WINDOW_ORANGE_S`/`_MTN_S`).
Sert UNIQUEMENT à la garde anti-doublon — il ne borne plus le polling. Le temps
restant ("Réessayez dans X") se calcule depuis l'ENVOI de l'USSD :
`X = retry_window(réseau) − (now − ussd_sent_at)` (la fenêtre opérateur court à
partir du push USSD). Fallbacks si la colonne manque : `cancelled_at` (moment du
verdict) puis `created_at`. Seuls `pending` et `cancelled` bloquent un nouveau
paiement.

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
