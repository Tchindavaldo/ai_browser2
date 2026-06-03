-- 008_settled_by.sql — trace QUI a posé le verdict final d'une transaction.
--
-- Deux mécanismes settlent le même paiement après l'USSD :
--   'polling' : la boucle verify dans /pay (update_transaction) ;
--   'webhook' : le callback DigiKUNTZ (update_status_by_provider_id).
-- Le premier qui obtient un verdict terminal gagne. Cette colonne dit lequel —
-- utile pour diagnostiquer si le webhook arrive réellement. NULL tant que non
-- settlé (pending). Idempotent.

alter table transactions
    add column if not exists settled_by text;   -- 'polling' | 'webhook'
