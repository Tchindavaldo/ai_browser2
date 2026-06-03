-- 006_ussd_sent_at.sql — horodate l'instant où l'USSD est envoyé au client.
--
-- C'est le moment où Flutterwave déclenche le push USSD (réponse /charge avec
-- flw_ref + chargeResponseCode 02). La fenêtre opérateur (Orange/MTN) court À
-- PARTIR DE CET INSTANT : la garde anti-doublon /pay calcule le délai restant
-- ("Réessayez dans X") = retry_window(réseau) − (now − ussd_sent_at).
-- Complète cancelled_at (005), qui reste pour l'audit du moment du verdict.
-- Idempotent.

alter table transactions
    add column if not exists ussd_sent_at timestamptz;
