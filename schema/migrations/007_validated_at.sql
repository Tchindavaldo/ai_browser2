-- 007_validated_at.sql — horodate la VALIDATION de l'USSD par l'utilisateur.
--
-- C'est l'instant où le polling verify détecte status=successful (code 00) : le
-- client vient d'autoriser le paiement sur son téléphone. La garde anti-doublon
-- /pay bloque alors un NOUVEAU paiement sur le même numéro pendant la fenêtre
-- réseau (retry_window : Orange 16min / MTN 1min) à partir de cet instant —
-- message "Vous avez récemment effectué un paiement, réessayez dans X".
-- Idempotent.

alter table transactions
    add column if not exists validated_at timestamptz;
