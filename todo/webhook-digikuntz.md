# TODO — Webhook DigiKUNTZ (callback serveur-à-serveur)

## Contexte

Le statut final d'un paiement est aujourd'hui obtenu par **polling statut**
(`aggregators/digikuntz/status_poll.py` : `GET {base}/transaction?transactionId=`).
C'est le mécanisme fiable, qui marche en local (pas besoin d'URL publique).

DigiKUNTZ peut AUSSI notifier via **webhook** (callback) : il POST le résultat à
l'URL passée en `callbackUrl` à la création. Modèle de référence : MoobilPay
(`BACKEND/src/controllers/handlers/payment/webhookHandler.js`).

Le webhook ne fonctionne QUE si notre backend a une **URL publique joignable**
par DigiKUNTZ. En local (dev), ce n'est pas le cas -> on s'appuie sur le polling.
À implémenter quand le backend sera déployé (Fly.io / URL publique).

## Ce que DigiKUNTZ envoie (d'après MoobilPay)

- `POST {callbackUrl}` avec `{ id, status, data }` (status = payin_pending|payin_success|
  payin_error|payin_closed).
- DigiKUNTZ **retry** si on ne répond pas 200 vite -> répondre 200 immédiatement,
  traiter en arrière-plan.

## Tâches

- [ ] Migration : ajouter `transactions.provider_transaction_id` (l'`id` DigiKUNTZ)
      + index, pour retrouver la transaction depuis le payload webhook.
- [ ] Stocker `provider_transaction_id` à l'insert (`db.insert_pending`) — il est
      déjà disponible dans `PaymentResult.provider_transaction_id`.
- [ ] Endpoint `POST /webhook/digikuntz` (core/server.py) :
      - répondre 200 tout de suite ;
      - mapper le status (payin_* -> interne) ;
      - retrouver la transaction par `id` provider, MAJ status (idempotent sur
        statut terminal) ;
      - ne jamais throw (200 déjà envoyé).
- [ ] Config `DIGIKUNTZ_CALLBACK_URL` = URL publique réelle pointant vers cet
      endpoint (remplacer le faux `https://app.digikuntz.com/callback` actuel qui
      renvoie 404).
- [ ] Coordination polling ⇄ webhook : le premier qui obtient un statut terminal
      gagne ; l'autre est idempotent (comme paymentEventRegistry chez MoobilPay).
- [ ] Régénérer le swagger.

## Note

Tant que ce TODO n'est pas fait, `DIGIKUNTZ_CALLBACK_URL` peut rester tel quel :
le 404 du callback est sans conséquence puisque le verdict vient du polling.
