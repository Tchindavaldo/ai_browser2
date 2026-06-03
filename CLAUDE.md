# Consignes projet — MobileWallet backend

Ce fichier est **versionné** : ses règles s'appliquent automatiquement sur tout
PC où le projet est cloné/pull, dans n'importe quelle session Claude Code.

## À lire en DÉBUT de session (OBLIGATOIRE)

Lis **`ARCHITECTURE.md`** (à la racine) avant de travailler : il donne une vision
360 du projet (carte des fichiers, rôle de chaque module, flux d'un paiement,
statuts, concurrence). Ça évite de devoir parcourir tout l'arbre pour comprendre.

**Tenir à jour :** dès qu'un travail modifie la structure (nouveau fichier,
module, endpoint, table, flux) ou rend une description obsolète, **mets à jour
`ARCHITECTURE.md` ET `README.md`** avant de clore — au même titre que le code.

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

**Procédure systématique (OBLIGATOIRE) dès qu'on me demande de résoudre un bug
ou un problème :**
1. Créer une branche dédiée (`debug/<sujet>`) **depuis la branche d'où vient le
   problème** (la feature/branche concernée), jamais depuis `main`.
2. Y travailler **séparément** (la résolution reste isolée sur cette branche).
3. **Tester / faire valider** la correction (par l'utilisateur, tests live inclus)
   AVANT tout merge.
4. **Merger seulement après validation** dans la branche d'origine.
Ne jamais coder un correctif directement sur la branche d'origine ni merger sans
validation explicite.

## Philosophie agent IA (OBLIGATOIRE)

**L'IA a le contrôle total — rien n'est décidé en dur dans le code.**

Le rôle du backend est de fournir à l'IA :
- Un snapshot fidèle de ce qu'elle voit (DOM, URL, éléments)
- Les outils pour agir (click, fill, select, reload, wait)
- Un objectif clair en langage naturel

**Ce que le code ne doit JAMAIS faire :**
- Décider à la place de l'IA qu'un paiement a réussi ou échoué
- Interrompre la boucle parce qu'une URL a changé (c'est à l'IA de voir et décider)
- Interpréter un résultat avant que l'IA l'ait lu à l'écran
- Forcer `objective_reached=true` ou `success=True` depuis le code

Si la page redirige, l'IA le voit dans le snapshot (URL + contenu) et conclut
elle-même. Le code fournit l'information, l'IA juge.

### Règle de travail (OBLIGATOIRE pour l'agent qui code)

Cette règle vaut **pour TOUT le code**, pas seulement la boucle principale —
y compris les phases « après que l'IA a conclu » (watch USSD, polling, outcome,
classification de résultat).

**Interdits (= « code en dur qui décide »), à traquer et supprimer :**
- `if status == ...: break` / `result.final_status = ...` posé par le CODE pour
  conclure un paiement (succès/échec/annulé/pending/timeout).
- `classifier.classify(...)` ou `_interpret(...)` dont le CODE exploite le
  résultat pour décider/arrêter, au lieu de redonner la main à l'IA.
- Sortir d'une boucle d'attente parce qu'un texte/URL a changé : c'est à l'IA de
  lire et décider si c'est fini.
- Tout verdict (cancelled au timeout, pending, etc.) écrit par le code.

**À la place :** le code OBSERVE (la page a-t-elle bougé ? une requête est-elle
partie ? combien de temps écoulé ?) et INJECTE ces faits dans le prochain tour
de l'IA (snapshot/header). L'IA lit et décide via `objective_reached` +
`objective_result`. Le code n'agit jamais sur un verdict qu'il a déduit lui-même.

**Procédure quand tu travailles sur un fichier :** si tu croises un endroit où
le code prend une décision qui revient à l'IA, REMPLACE-le par le pattern
« le code informe, l'IA décide » (réintégrer la phase dans la boucle de
raisonnement), et signale-le. Ne jamais laisser ni réintroduire ce genre de code.

**Seule exception : les FAITS opérateur universels et mécaniques** (pas des
interprétations). Ex. le délai de validation USSD de 17 min : une fois écoulé,
la demande a expiré côté opérateur — c'est mécanique, sans ambiguïté, donc le
code peut le constater et poser le statut `expired` sans passer par l'IA. Ces
faits ne sont JAMAIS des verdicts de paiement (succès/échec/refus) : ceux-là
restent toujours à l'IA. En cas de doute, c'est l'IA qui tranche.

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
