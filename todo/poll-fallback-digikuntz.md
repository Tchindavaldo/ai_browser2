# TODO — Fallback statut via API DigiKUNTZ après échec du polling

## Contexte
Le polling `/verify` de Flutterwave (`step4_poll_verify` dans `replay.py`) tourne
au maximum **17 min** (horloge réelle), soit le temps observé avant que
l'opérateur (Orange/MTN) annule lui-même la transaction non validée (~15-16 min
constatés via SMS) + 1 min de marge.

## À faire
Après les 17 min sans verdict final, deux cas DISTINCTS (drapeau
`got_any_status` renvoyé par `step4_poll_verify`) :

- **Cas A — `got_any_status == True`** : on a bien reçu des statuts (restés
  `pending`). Le délai opérateur (~15-16 min) est dépassé → la transaction est
  **annulée par l'opérateur**. PAS besoin d'appeler DigiKUNTZ : on conclut
  directement `cancelled`. (Déjà géré dans `main()`.)

- **Cas B — `got_any_status == False`** : tous les `/verify` ont timeout/échoué,
  Flutterwave était injoignable, on ne sait RIEN. C'est le SEUL cas où il faut
  **interroger l'API DigiKUNTZ** pour récupérer le vrai statut final de la
  transaction (via le `txRef` / `transactionRef`).

=> Ce TODO ne concerne donc QUE le cas B.

## Pourquoi
Flutterwave peut devenir injoignable alors que la transaction a été traitée côté
DigiKUNTZ. DigiKUNTZ est la source de vérité de notre côté : c'est notre
plateforme marchand, donc son statut prime quand Flutterwave ne répond plus.

## Pistes d'implémentation (à préciser)
- Trouver / confirmer l'endpoint DigiKUNTZ de consultation de statut d'une
  transaction (probablement `GET {DIGIKUNTZ_BASE}/transaction/{txRef}` ou
  équivalent — à vérifier dans la doc/API).
- Headers d'auth: `x-user-id` + `x-secret-key` (déjà utilisés à l'étape 1).
- Mapper le statut DigiKUNTZ vers nos statuts clairs (successful/failed/pending).
