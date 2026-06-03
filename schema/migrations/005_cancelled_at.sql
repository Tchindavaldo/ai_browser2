-- 005_cancelled_at.sql — horodate le passage d'une transaction à 'cancelled'.
--
-- La garde anti-doublon /pay bloque un nouveau paiement sur un numéro dont la
-- dernière transaction est 'cancelled', pendant la fenêtre opérateur (Orange/MTN).
-- Le délai restant ("Réessayez dans X") se compte À PARTIR DU PASSAGE À cancelled
-- (et non de la création), d'où cette colonne. Idempotent.

alter table transactions
    add column if not exists cancelled_at timestamptz;
