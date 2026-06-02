# MobileWallet — backend

Backend d'agrégation de paiements **Mobile Money** (XAF). MobileWallet rassemble des
**agrégateurs** de paiement ; chacun est un module exposant deux capacités :

- **navigateur IA** — un agent Playwright + LLM pilote le checkout, exécute le paiement
  et **déduit** la requête `/charge` + `/verify` (le « curl replay »).
- **curl replay** — rejoue le paiement sans navigateur via un *template* stocké.

DigiKUNTZ est le premier agrégateur. Le navigateur IA est **générique** (mutualisé) ;
le curl replay est **propre à chaque agrégateur**.

> Pour une **vision 360** (carte de tous les fichiers, rôle de chaque module, flux
> d'un paiement, statuts, concurrence), voir **[`ARCHITECTURE.md`](ARCHITECTURE.md)**.

---

## Architecture

```
ai_browser2/
├── main.py                     # lanceur : importe core.server:app, lance uvicorn
├── core/                       # MOTEUR GÉNÉRIQUE (partagé)
│   ├── server.py               #   FastAPI : endpoints + lifespan + routing
│   ├── base.py                 #   interface Aggregator (ABC) + dataclasses
│   ├── registry.py             #   registre nom -> classe d'agrégateur
│   ├── config.py               #   settings centralisés (.env)
│   ├── db.py                   #   couche Supabase (async, optionnelle)
│   ├── browser.py              #   navigateur IA (Playwright, capture, matchers)
│   ├── browser_runner.py       #   orchestration navigateur générique
│   ├── reasoning_loop.py       #   boucle IA (snapshot -> LLM -> actions)
│   ├── llm_client.py           #   client DeepSeek
│   ├── classifier.py           #   classification de statut (mots-clés)
│   └── crypto/                 #   encrypt.js + cryptico_py.py
├── aggregators/                # UN DOSSIER PAR AGRÉGATEUR
│   └── digikuntz/
│       ├── aggregator.py       #   DigikuntzAggregator (implémente l'ABC, s'enregistre)
│       ├── browser_flow.py     #   mode navigateur (objectif IA, watch USSD, verdict)
│       └── replay_flow.py      #   mode curl replay (steps + interpret)
├── schema/supabase.sql         # tables transactions + curl_templates
├── .env.example                # clés de configuration
└── requirements.txt
```

**3 couches** : `server.py` (API/routing, agnostique) → `base.py` (contrat `Aggregator`) →
`aggregators/<nom>/` (logique métier). La persistance (`db.py`) est appelée par la couche API.

---

## Installation & lancement

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # navigateur pour le mode browser
cp .env.example .env                 # puis remplir les valeurs

./start.sh                           # démarre l'API (+ navigateur au boot)
./dev.sh                             # dev : hot-reload sur changement de .py
```

> Les scripts utilisent le venv automatiquement. Équivalents longs :
> `venv/bin/python main.py` et `RELOAD=1 venv/bin/python main.py`.

> Le hot-reload (`RELOAD=1`) surveille le **code** (`.py`), pas le `.env`. Après avoir
> modifié `.env` (ex: ajout Supabase), il faut **couper et relancer** le serveur.

Variables d'environnement (cf. `.env.example`) : `DIGIKUNTZ_*`, `FLW_*`, `DEEPSEEK_API_KEY`,
`SUPABASE_URL`/`SUPABASE_KEY`, `HEADLESS` (1 en prod), `PORT`, `HOST`.

> Sans Supabase configuré, l'API fonctionne quand même : la persistance devient un no-op
> (pas d'historique ni de template stocké).

---

## Documentation interactive (Swagger)

FastAPI génère automatiquement la doc OpenAPI :

- **Swagger UI** : `http://localhost:7332/docs`
- **ReDoc** : `http://localhost:7332/redoc`
- **Schéma brut** : `http://localhost:7332/openapi.json`

---

## Endpoints

**Endpoints CLIENT** (documentés dans Swagger) :

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/health` | statut + agrégateurs |
| GET | `/aggregators` | modules disponibles + réseaux supportés |
| POST | `/pay` | exécuter un paiement (aggregator + mode) |
| POST | `/transactions/{tx_id}/cancel` | débloquer une transaction `pending` |
| GET/PUT | `/config/max-tabs` | seuil d'onglets par navigateur (concurrence) |

**Endpoints ADMIN/DEBUG** (masqués de Swagger ; outils d'audit pour le backend) :
`GET /transactions`, `GET /status/{ref}`, `GET /transactions/{ref}/trace`,
`GET /transactions/{ref}/errors`, `GET|POST /aggregators/{name}/template`,
`POST /drive`, `POST /test-llm`. *(À terme : endpoint admin protégé par auth — cf.
`todo/migrer-vue-admin-endpoint-dedie.md`.)*

> **Vue client vs admin** : `/pay` renvoie par défaut une **vue minimale**
> (`success`, `status`, `message`, `transaction_id`, `code`). L'admin peut ajouter
> `?debug=true` (non documenté dans Swagger) pour obtenir tout le détail technique
> (charge/verify, curl déduit, tokens, trace…).

### `POST /pay`

```jsonc
{
  "amount": 25,                 // XAF
  "phone": "696080087",
  "network": "Orangemoney",     // ou "MTN"
  "email": "client@example.com",
  "aggregator": "digikuntz",
  "mode": "auto",               // "auto" | "browser" | "replay"
  "fallback_browser": true      // mode auto : bascule navigateur si replay non concluant
}
```

**Modes :**
- `auto` (défaut) — tente le **replay** ; si non concluant (pas de template / `failed` /
  `unknown`), bascule sur le **navigateur** quand `fallback_browser=true`.
- `browser` — flux IA complet ; **déduit et persiste** le template curl.
- `replay` — rejoue via le template stocké. **409** si aucun template (lancer `browser` d'abord).

**Codes d'erreur :** `404` agrégateur inconnu · `400` mode invalide · `422` réseau non
supporté (renvoie la liste exacte attendue) · `409` replay sans template · `502` replay
échoué et fallback désactivé.

**Réseaux supportés** : propres à chaque agrégateur. `GET /aggregators` les expose :

```json
{"aggregators":[{"name":"digikuntz","supported_networks":["Orangemoney","MTN"]}]}
```

Un `network` invalide renvoie `422` avec la liste exacte ; les variantes tolérées
(`orange` → `Orangemoney`) sont normalisées automatiquement.

Exemple :

```bash
curl -X POST localhost:7332/pay -H 'content-type: application/json' -d '{
  "amount":25,"phone":"696080087","network":"Orangemoney",
  "email":"client@example.com","mode":"browser"
}'
```

---

## Persistance (Supabase)

Créer les tables via `schema/supabase.sql` puis renseigner `SUPABASE_URL`/`SUPABASE_KEY`.

- `transactions` — audit d'une tentative. Insérée en **`pending`** dès le départ, puis
  **mise à jour** (`status`/`success`/`message`) au verdict final. Un paiement ne peut pas
  être lancé sur un numéro qui a déjà une transaction `pending` (→ **409 `pending_exists`**).
- `curl_templates` — templates de replay **versionnés par agrégateur** (jamais mélangés).
  Le mode browser **ajoute** une nouvelle version **uniquement si la recette a changé**
  (sinon rien) ; la précédente est désactivée mais **conservée en historique**. Exactement
  une ligne `is_active=true` par agrégateur — c'est celle que le mode replay recharge.

Les appels Supabase (synchrones) sont exécutés via `asyncio.to_thread` pour ne pas bloquer
la boucle asynchrone (ni le navigateur unique).

### Template manuel

Amorcer/corriger le template de replay sans passer par le mode browser :

```bash
# Lire le template actif
curl localhost:7332/aggregators/digikuntz/template

# Ajouter/mettre à jour (append-si-différent ; force=true pour forcer une version)
curl -X POST localhost:7332/aggregators/digikuntz/template \
  -H 'content-type: application/json' -d '{
    "charge_url": "https://api.ravepay.co/flwv3-pug/getpaidx/api/charge?use_polling=1",
    "verify_url": "https://api.ravepay.co/flwv3-pug/getpaidx/api/verify/mpesa",
    "public_key_rsa": "<clé RSA cryptico>",
    "flw_pub_key": "FLWPUBK-...-X",
    "headers": {"content-type": "application/json", "x-flw-lang": "FR"},
    "force": false
  }'
```

---

## Ajouter un agrégateur

1. Créer `aggregators/<nom>/`.
2. Implémenter une classe héritant de `core.base.Aggregator` (les méthodes abstraites :
   `create_transaction`, `browser_objective`, `decide_browser_outcome`, `network_label`,
   `charge_request_matcher`, `verify_request_matcher`, `checkout_url_predicate`,
   `extract_curl_template`, `interpret_status`, `replay`, `pay_via_browser`).
3. S'enregistrer à l'import : `register("<nom>", MonAggregator)`.
4. L'importer depuis `aggregators/<nom>/__init__.py`.

Aucune modification de `core/` n'est nécessaire — le navigateur IA générique et le routing
fonctionnent par contrat.

---

## Tests manuels

`POST /pay` déclenche une **vraie transaction** (et un prompt USSD `#150*50#` sur le
téléphone). Utiliser un petit montant et un numéro contrôlé.

> **Tester avec `./start.sh`** (sans hot-reload). Le `./dev.sh` recharge sur tout
> changement de `.py` et **coupe les requêtes `/pay` longues** (l'IA attend la
> validation USSD jusqu'à 17 min) — Postman resterait en attente sans réponse.

**Comportement clé des deux moteurs (aligné)** :
- Après le clic Payer, l'**IA reste active** et attend la validation USSD jusqu'à
  **17 min** (le délai opérateur). Elle ne conclut que sur un vrai résultat lu à
  l'écran ; le code n'interprète pas à sa place.
- Statuts finaux : `successful`, `failed` (refus / solde insuffisant),
  `cancelled` (refus USSD), **`expired`** (17 min écoulées → **relançable tout de
  suite**), `pending`. Pannes amont → **503** `network_unavailable` (API) ou
  `operator_unavailable` (réseau opérateur dérangé).
- Garde anti-doublon : seuls `pending` et `cancelled` bloquent un nouveau paiement
  sur le même numéro ; `failed`/`expired`/pannes sont **immédiatement relançables**.
- Replay : retry ×3 sur le charge, polling `/verify` borné à 17 min (horloge réelle).
