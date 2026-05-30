"""Curl replay: pay via Flutterwave API directly without browser.

Flow:
1. Create digikuntz transaction → get txRef + amount
2. Initialize checkout → get modalauditid
3. Encrypt payload with RSA (cryptico format)
4. POST /charge → get flw_ref
5. Poll /verify/mpesa → get final status
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import uuid

import httpx

# ---------------------------------------------------------------------------
# Flutterwave response → message clair
# Basé sur les vraies réponses capturées dans nos tests.
# ---------------------------------------------------------------------------

# chargeResponseCode → (statut, message)
CHARGE_CODES = {
    "00": ("successful", "Paiement validé avec succès."),
    "02": ("pending", "En attente de validation USSD par l'utilisateur."),
    "R1": ("failed", None),  # None = dépend du contexte (réseau / refus)
    "RR": ("failed", None),
    "XX": ("failed", None),
}

# data.code / status → (statut, message)
FLW_CODES = {
    "FLW_ERR": ("failed", None),
}

# Flutterwave data.status → (statut, message)
FLW_STATUSES = {
    "successful": ("successful", "Paiement validé avec succès."),
    "success-completed": ("successful", "Paiement validé avec succès."),
    "failed": ("failed", None),
    "cancelled": ("cancelled", "Paiement annulé par l'utilisateur."),
    "success-pending-validation": ("pending", "En attente de validation USSD."),
}


def alt_network(network: str) -> str:
    n = (network or "").lower()
    if "orange" in n:
        return "MTN"
    if "mtn" in n:
        return "Orange"
    return "un autre réseau"


def network_label(network: str) -> str:
    n = (network or "").lower()
    if "orange" in n:
        return "Orange Money"
    if "mtn" in n:
        return "MTN Mobile Money"
    return network or "ce réseau"


def interpret_charge(resp: dict, network: str) -> tuple[str, str] | None:
    """Interpret a /charge or ping_url response.

    Returns (status, message_clair) or None if no definitive verdict.
    At the charge stage, USSD hasn't been sent yet, so any failure = network.
    """
    # Top-level error
    if resp.get("status") == "error":
        data = resp.get("data", {})
        if isinstance(data, dict):
            code = data.get("code", "")
            err_tx = data.get("err_tx", {})
            rc = err_tx.get("chargeResponseCode", "") if isinstance(err_tx, dict) else ""
            flw_msg = resp.get("message", "")
            # Build the clear message
            msg = (f"Le réseau {network_label(network)} est actuellement dérangé. "
                   f"Veuillez réessayer avec {alt_network(network)}.")
            detail = f" [code={code}, chargeResponseCode={rc}, message={flw_msg}]"
            return "failed", msg + detail
    return None


def interpret_verify(resp: dict, network: str) -> tuple[str, str] | None:
    """Interpret a /verify/mpesa response.

    At verify stage, USSD WAS sent, so a failure = user refused / insufficient funds.
    """
    data = resp.get("data", {})
    if not isinstance(data, dict):
        return None

    status = data.get("status", "")
    rc = data.get("chargeResponseCode", "")

    # Check chargeResponseCode first (most precise)
    if rc in CHARGE_CODES:
        st, msg = CHARGE_CODES[rc]
        if st == "successful":
            return "successful", msg
        if st == "failed":
            return "failed", ("Paiement échoué après le USSD "
                              "(refus de l'utilisateur ou solde insuffisant).")
        # pending → continue polling
        return None

    # Check data.status
    if status in FLW_STATUSES:
        st, msg = FLW_STATUSES[status]
        if st in ("successful", "cancelled"):
            return st, msg
        if st == "failed":
            return "failed", ("Paiement échoué après le USSD "
                              "(refus de l'utilisateur ou solde insuffisant).")
        # pending → continue
        return None

    return None


def interpret_ping(ping_data: dict, network: str) -> tuple[str, str] | None:
    """Interpret a ping_url response (used for 'demande longue').

    The real response is often nested as a JSON string in data.response.
    """
    inner = ping_data.get("data", {})
    if not isinstance(inner, dict):
        return None

    # data.response can be a JSON string with the real charge result
    resp_str = inner.get("response", "")
    if isinstance(resp_str, str) and resp_str.startswith("{"):
        try:
            resp_obj = json.loads(resp_str)
            code = resp_obj.get("code", "")
            if code in FLW_CODES:
                msg = (f"Le réseau {network_label(network)} est actuellement dérangé. "
                       f"Veuillez réessayer avec {alt_network(network)}.")
                return "failed", msg + f" [code={code}]"
            # Could also contain success data
            flw_ref = resp_obj.get("flw_reference", "") or resp_obj.get("flwRef", "")
            if flw_ref:
                return None  # Not a failure, has a flw_ref → continue flow
        except json.JSONDecodeError:
            pass

    return None

# digiKUNTZ config
DIGIKUNTZ_BASE = "https://app.digikuntz.com/dev"
DIGIKUNTZ_USER_ID = "USERID-REDACTED"
DIGIKUNTZ_SECRET = "SK-REDACTED"

# Flutterwave config (from captured data)
FLW_PUB_KEY = "FLWPUBK-REDACTED"
FLW_CHARGE_URL = "https://api.ravepay.co/flwv3-pug/getpaidx/api/charge?use_polling=1"
FLW_VERIFY_URL = "https://api.ravepay.co/flwv3-pug/getpaidx/api/verify/mpesa"
FLW_INIT_URL = "https://api.ravepay.co/v3/checkout/initialize"
FLW_UPGRADE_URL = "https://api.ravepay.co/v2/checkout/upgrade"

HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "origin": "https://checkout-v3-ui-prod.f4b-flutterwave.com",
    "referer": "https://checkout-v3-ui-prod.f4b-flutterwave.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "x-flw-lang": "FR",
}


def encrypt_payload(plaintext: str, public_key_b64: str) -> str:
    """Encrypt plaintext using RSA public key in cryptico format.

    Cryptico uses RSA with PKCS1v1.5 padding. The public key is an RSA
    public key in a custom base64 format. The output is:
    base64(encrypted_aes_key)?base64(aes_encrypted_data)

    For simplicity, we use the 3DES encryption that Flutterwave's
    getEncryptionKey derives from the secret key.
    """
    # Actually, Flutterwave v3 checkout uses cryptico.js which is RSA+AES.
    # But the /charge endpoint also accepts a simpler format using
    # Flutterwave's own encryption with the encryption key derived from
    # the secret key. Let's try the direct Flutterwave v3 charge API
    # format instead.
    #
    # For now, return the plaintext — we'll use a different approach.
    return plaintext


async def step1_create_transaction(
    amount: int, phone: str, email: str, name: str
) -> dict:
    """Create a digikuntz transaction, return {txRef, paymentLink, amount}."""
    print(f"[1] Creating digikuntz transaction: {amount} XAF, {phone}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DIGIKUNTZ_BASE}/transaction",
            json={
                "estimation": amount,
                "raisonForTransfer": "Rauvalia replay",
                "userEmail": email,
                "userPhone": phone,
                "userCountry": "CM",
                "senderName": name,
                "callbackUrl": "https://app.digikuntz.com/callback",
            },
            headers={
                "x-user-id": DIGIKUNTZ_USER_ID,
                "x-secret-key": DIGIKUNTZ_SECRET,
                "content-type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json().get("data", resp.json())
        print(f"    txRef={data.get('transactionRef')}")
        print(f"    paymentLink={data.get('paymentLink')}")
        print(f"    paymentWithTaxes={data.get('paymentWithTaxes')}")
        return data


async def step2_initialize_checkout(payment_link: str) -> dict:
    """Load the payment link, initialize checkout, extract RSA public key."""
    print(f"[2] Initializing checkout...")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Get the hosted_pay JSON
        link_id = payment_link.split("/")[-1]
        resp2 = await client.get(
            f"https://api.ravepay.co/flwv3-pug/getpaidx/api/hosted_pay/{link_id}?json=1"
        )
        hosted = resp2.json()
        print(f"    hosted_pay status: {hosted.get('status')}")

        # Upgrade checkout
        hosted_data = hosted.get("data", {})
        await client.post(FLW_UPGRADE_URL, json=hosted_data, headers=HEADERS)

        # Initialize — this returns the RSA public key
        resp4 = await client.post(FLW_INIT_URL, json=hosted_data, headers=HEADERS)
        init_resp = resp4.json()
        init_data = init_resp.get("data") or {}
        public_key = init_data.get("public_key", "") if init_data else ""
        print(f"    initialize status: {init_resp.get('status')}")
        print(f"    RSA public key: {public_key[:60]}...")

        return {
            "hosted_data": hosted_data,
            "init_data": init_data,
            "public_key": public_key,
        }


async def step3_charge(
    amount: int,
    phone: str,
    network: str,
    email: str,
    firstname: str,
    lastname: str,
    tx_ref: str,
    public_key_rsa: str,
    redirect_url: str = "https://payments.digikuntz.com/payment-done/subscription/packages",
) -> dict:
    """Send the charge request using cryptico encryption (same as browser)."""
    import subprocess

    print(f"[3] Sending charge: {amount} XAF, {network}, {phone}")

    modalauditid = uuid.uuid4().hex
    device_fp = hashlib.sha256(f"{email}{time.time()}".encode()).hexdigest()

    # Build the plaintext payload (exact same structure the browser sends)
    plaintext = json.dumps({
        "amount": amount,
        "campaign_id": None,
        "is_discounted": 0,
        "country": "CM",
        "currency": "XAF",
        "cycle": "one-time",
        "device_fingerprint": device_fp,
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "meta": [
            {"metaname": "__CheckoutInitAddress", "metavalue": "N/A"},
            {"metaname": "app", "metavalue": "digikuntz-payments"},
            {"metaname": "env", "metavalue": "development"},
        ],
        "modalauditid": modalauditid,
        "payment_type": "mobilemoneyfranco",
        "PBFPubKey": FLW_PUB_KEY,
        "redirect_url": redirect_url,
        "txRef": tx_ref,
        "is_mobile_money_franco": True,
        "network": network,
        "phonenumber": phone,
    }, separators=(",", ":"))

    # Encrypt with REAL cryptico.js via Node.js — identical to browser
    script_dir = os.path.dirname(os.path.abspath(__file__))
    encrypt_js = os.path.join(script_dir, "encrypt.js")
    proc = subprocess.run(
        ["node", encrypt_js, plaintext, public_key_rsa],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        print(f"    ERROR: cryptico encryption failed: {proc.stderr}")
        return {"modalauditid": modalauditid, "charge_response": {"status": "error", "message": proc.stderr}}
    encrypted = proc.stdout.strip()
    print(f"    encrypted payload: {len(encrypted)} chars")

    # Send charge request (same format as captured from browser)
    charge_body = {
        "modalauditid": modalauditid,
        "PBFPubKey": FLW_PUB_KEY,
        "client": encrypted,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            FLW_CHARGE_URL,
            json=charge_body,
            headers=HEADERS,
        )
        result = resp.json()
        print(f"    charge status: {resp.status_code}")
        print(f"    response: {json.dumps(result, indent=2)[:500]}")
        return {"modalauditid": modalauditid, "charge_response": result}


async def step4_poll_verify(
    modalauditid: str, flw_ref: str, timeout_s: int = 60
) -> dict:
    """Poll the verify endpoint until payment is confirmed or failed."""
    print(f"[4] Polling verify (flw_ref={flw_ref})...")

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(timeout_s // 3):
            await asyncio.sleep(3)
            resp = await client.post(
                FLW_VERIFY_URL,
                json={
                    "modalauditid": modalauditid,
                    "PBFPubKey": FLW_PUB_KEY,
                    "flw_ref": flw_ref,
                },
                headers=HEADERS,
            )
            data = resp.json()
            status = data.get("data", {}).get("status", "pending")
            charge_code = data.get("data", {}).get("chargeResponseCode", "")
            print(f"    poll {i+1}: status={status} code={charge_code}")

            # Parse the structured verify response directly.
            verdict = interpret_verify(data, "")
            if verdict and verdict[0] in ("successful", "failed", "cancelled"):
                return data
            if status in ("successful", "failed"):
                return data
            if charge_code == "00":
                return data

    return {"status": "timeout", "message": "Verification timed out"}


async def main():
    amount = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    phone = sys.argv[2] if len(sys.argv) > 2 else "696080087"
    network = sys.argv[3] if len(sys.argv) > 3 else "Orangemoney"
    email = sys.argv[4] if len(sys.argv) > 4 else "tchindatchenetsuvaldoblair@gmail.com"
    name = sys.argv[5] if len(sys.argv) > 5 else "Blair"

    print(f"\n=== Flutterwave Direct Charge (No Browser) ===")
    print(f"Amount: {amount} XAF | Phone: {phone} | Network: {network}\n")

    # Step 1: Create digikuntz transaction
    tx = await step1_create_transaction(amount, phone, email, name)
    tx_ref = tx.get("transactionRef", "")
    payment_link = tx.get("paymentLink", "")
    total = int(tx.get("paymentWithTaxes", amount))

    if not payment_link:
        print("ERROR: No payment link returned")
        return

    # Step 2: Initialize checkout and get RSA public key
    checkout = await step2_initialize_checkout(payment_link)
    public_key_rsa = checkout.get("public_key", "")

    if not public_key_rsa:
        # Fallback to the key we captured from browser tests
        public_key_rsa = "baA/RgjURU3I0uqH3iRos3NbE8fT+lP8SDXKymsnfdPrMQAEoMBuXtoaQiJ1i5tuBG9EgSEOH1LAZEaAsvwClw=="
        print(f"    Using fallback RSA key")

    # Step 3: Charge with cryptico encryption
    charge = await step3_charge(
        amount=total,
        phone=phone,
        network=network,
        email=email,
        firstname="API",
        lastname=f"Call: {name}",
        tx_ref=tx_ref,
        public_key_rsa=public_key_rsa,
    )

    charge_resp = charge.get("charge_response", {})
    charge_data = charge_resp.get("data", {})

    # Interpret directly from JSON structure (no keyword matching needed).
    verdict = interpret_charge(charge_resp, network)
    if verdict:
        status, msg = verdict
        print(f"\n=== RESULTAT: {status.upper()} ===")
        print(f"    {msg}")
        return

    flw_ref = ""
    if charge_data:
        flw_ref = charge_data.get("flw_ref", "")
        if not flw_ref:
            nested = charge_data.get("data", {})
            if nested:
                flw_ref = nested.get("flw_reference", "")

        # Handle "demande longue" — poll ping_url for the real response
        ping_url = charge_data.get("ping_url", "")
        if ping_url and not flw_ref:
            print(f"\n[3b] Polling ping_url for charge result...")
            async with httpx.AsyncClient(timeout=30) as client:
                for i in range(20):
                    await asyncio.sleep(3)
                    resp = await client.get(ping_url, headers=HEADERS)
                    ping_data = resp.json()
                    print(f"    ping {i+1}: {json.dumps(ping_data)[:200]}")

                    # Parse the structured ping response directly.
                    verdict = interpret_ping(ping_data, network)
                    if verdict:
                        status, msg = verdict
                        print(f"\n=== RESULTAT: {status.upper()} ===")
                        print(f"    {msg}")
                        return

                    inner = ping_data.get("data", {})
                    if isinstance(inner, dict):
                        # The real charge result lives in data.response — a JSON
                        # *string* (or already-parsed in data.response_parsed).
                        resp_obj = inner.get("response_parsed")
                        if not isinstance(resp_obj, dict):
                            resp_str = inner.get("response", "")
                            if isinstance(resp_str, str) and resp_str.startswith("{"):
                                try:
                                    resp_obj = json.loads(resp_str)
                                except json.JSONDecodeError:
                                    resp_obj = {}
                        if isinstance(resp_obj, dict):
                            nested2 = resp_obj.get("data", {})
                            if isinstance(nested2, dict):
                                flw_ref = nested2.get("flw_reference", "") or nested2.get("flwRef", "")
                                if flw_ref:
                                    print(f"    Got flw_ref: {flw_ref}")
                                    print(f"    note: {nested2.get('note', '')}")
                                    break
                    status = ping_data.get("status", "")
                    if status == "error":
                        print(f"    Charge failed: {ping_data.get('message', '')}")
                        break

    if not flw_ref:
        print(f"\nERROR: No flw_ref in charge response")
        print(f"Full response: {json.dumps(charge_resp, indent=2)[:1000]}")
        return

    print(f"\n    flw_ref: {flw_ref}")
    print(f"    Compose #150*50# sur ton telephone pour valider!\n")

    # Step 4: Poll verify. The USSD WAS sent, so a failure = user refused / no funds.
    verify = await step4_poll_verify(charge["modalauditid"], flw_ref)
    verdict = interpret_verify(verify, network)
    if verdict:
        status, msg = verdict
    else:
        status = verify.get("data", {}).get("status", "unknown")
        if status == "unknown":
            msg = "Timeout — l'utilisateur n'a pas validé le USSD dans le délai."
        else:
            msg = f"Statut Flutterwave: {status}"
    print(f"\n=== RESULTAT: {status.upper()} ===")
    print(f"    {msg}")
    print(json.dumps(verify, indent=2)[:400])


if __name__ == "__main__":
    asyncio.run(main())
