# MobileWallet — backend

Backend d'agrégation de paiements **Mobile Money** (XAF). MobileWallet rassemble des
**agrégateurs** de paiement ; chacun est un module exposant deux capacités :

- **navigateur IA** — un agent Playwright + LLM pilote le checkout, exécute le paiement
  et **déduit** la requête `/charge` + `/verify` (le « curl replay »).
- **curl replay** — rejoue le paiement sans navigateur via un *template* stocké.

DigiKUNTZ est le premier agrégateur. Le navigateur IA est **générique** (mutualisé) ;
le curl replay est **propre à chaque agrégateur**.

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

python main.py                       # démarre l'API (+ navigateur au boot)
```

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

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/health` | statut + agrégateurs |
| GET | `/aggregators` | modules disponibles |
| POST | `/pay` | exécuter un paiement (aggregator + mode) |
| GET | `/transactions?aggregator=&limit=` | historique (Supabase) |
| GET | `/status/{transaction_ref}` | statut d'une transaction |
| POST | `/drive` | pilotage libre du navigateur (dev) |
| POST | `/test-llm` | ping du LLM (dev) |

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
- `curl_templates` — template de replay réutilisable, **un par agrégateur** (déduit en
  mode browser, rechargé en mode replay).

Les appels Supabase (synchrones) sont exécutés via `asyncio.to_thread` pour ne pas bloquer
la boucle asynchrone (ni le navigateur unique).

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
téléphone). Utiliser un petit montant et un numéro contrôlé. Le mode replay reproduit le
comportement réglé : retry ×3 sur le charge, polling `/verify` borné à **17 min** (horloge
réelle), messages clairs « annulée par l'opérateur » / « échoué après USSD ».
