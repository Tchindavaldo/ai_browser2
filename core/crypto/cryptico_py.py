"""Python implementation of cryptico.js encrypt function.

Cryptico encrypt flow:
1. Generate random 32-byte AES key
2. RSA-encrypt the AES key with the recipient's public key
3. AES-encrypt the plaintext with AES (custom ECB-based CBC, zero-padded)
4. Output: base64(RSA(aes_key)) + "?" + base64_256(AES(plaintext))

The public key format is: base64_hex(modulus) where exponent is always "03" (0x03).
"""

import os
import struct
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

# ---------- Base conversion helpers (matching cryptico.js) ----------

B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def b16_to_b64(hex_str: str) -> str:
    """Convert hex string to cryptico's base64 format (b16to64)."""
    if len(hex_str) % 2 == 1:
        hex_str = "0" + hex_str
    result = ""
    i = 0
    while i + 3 <= len(hex_str):
        d = int(hex_str[i:i+3], 16)
        result += B64_CHARS[d >> 6]
        result += B64_CHARS[d & 63]
        i += 3
    if i + 1 == len(hex_str):
        d = int(hex_str[i:i+1], 16)
        result += B64_CHARS[d << 2]
    elif i + 2 == len(hex_str):
        d = int(hex_str[i:i+2], 16)
        result += B64_CHARS[d >> 2]
        result += B64_CHARS[(d & 3) << 4]
    return result


def b64_to_b16(b64_str: str) -> str:
    """Convert cryptico's base64 to hex string (b64to16)."""
    result = ""
    for i in range(len(b64_str)):
        v = B64_CHARS.index(b64_str[i]) if b64_str[i] in B64_CHARS else 0
        if i == 0:
            result += hex(v >> 2)[2:]
            e = (v & 3)
            k = 1
        elif k == 1:
            result += hex(e << 2 | v >> 4)[2:]
            e = (v & 15)
            k = 2
        elif k == 2:
            result += hex(e)[2:]
            result += hex(v >> 2)[2:]
            e = (v & 3)
            k = 3
        elif k == 3:
            result += hex(e << 2 | v >> 4)[2:]
            result += hex(v & 15)[2:]
            k = 0
    return result


def bytes_to_b256_b64(data: bytes) -> str:
    """Convert bytes to cryptico's b256to64 encoding."""
    result = ""
    c = 0
    f = 0
    for byte in data:
        d = byte
        if f == 0:
            result += B64_CHARS[d >> 2 & 63]
            c = (d & 3) << 4
        elif f == 1:
            result += B64_CHARS[c | d >> 4 & 15]
            c = (d & 15) << 2
        elif f == 2:
            result += B64_CHARS[c | d >> 6 & 3]
            result += B64_CHARS[d & 63]
        f = (f + 1) % 3
    if f == 1:
        result += B64_CHARS[c]
        result += "="
    elif f == 2:
        result += B64_CHARS[c]
    return result


# ---------- AES (cryptico uses a custom implementation) ----------
# Instead of reimplementing cryptico's custom AES, use PyCryptodome's
# AES-ECB to replicate the CBC mode manually (same as cryptico does).

from Crypto.Cipher import AES


def aes_encrypt_block(block: bytes, expanded_key: bytes) -> bytes:
    """Encrypt one 16-byte block with AES-ECB."""
    cipher = AES.new(expanded_key, AES.MODE_ECB)
    return cipher.encrypt(block)


def xor_blocks(a: bytes, b: bytes) -> bytes:
    """XOR two 16-byte blocks."""
    return bytes(x ^ y for x, y in zip(a, b))


def cryptico_aes_cbc_encrypt(plaintext_bytes: list[int], key_bytes: list[int]) -> str:
    """Encrypt with cryptico's AES-CBC mode.

    - key is 32 bytes (256-bit AES)
    - IV is random 16 bytes, prepended to ciphertext
    - Padding is zero-padding to 16-byte boundary
    - Output is b256to64 encoded
    """
    key = bytes(key_bytes)

    # Pad plaintext to 16-byte boundary with zeros
    data = list(plaintext_bytes)
    pad_len = (16 - len(data) % 16) % 16
    data.extend([0] * pad_len)

    # Generate random IV (16 bytes)
    iv = list(os.urandom(16))

    # CBC encrypt: each block XORed with previous ciphertext block
    result = list(iv)  # IV is first block of output
    prev_block = bytes(iv)

    for i in range(0, len(data), 16):
        block = bytes(data[i:i+16])
        xored = xor_blocks(prev_block, block)
        encrypted = aes_encrypt_block(xored, key)
        result.extend(encrypted)
        prev_block = encrypted

    # Encode as b256to64
    return bytes_to_b256_b64(bytes(result))


# ---------- RSA encryption ----------

def public_key_from_string(key_b64: str) -> RSA.RsaKey:
    """Parse a cryptico public key string into an RSA key.

    The key is standard base64-encoded raw modulus bytes.
    May contain "|" — left part is the modulus, right ignored.
    Exponent is always 3 (0x03).
    """
    import base64
    parts = key_b64.split("|")
    mod_bytes = base64.b64decode(parts[0])
    n = int.from_bytes(mod_bytes, 'big')
    e = 3  # cryptico always uses exponent 3
    return RSA.construct((n, e))


def rsa_encrypt_bytes(data: bytes, pub_key: RSA.RsaKey) -> str:
    """RSA-encrypt data and return as b16to64 encoded string.

    Cryptico.js does:
    1. pkcs1pad2(data, key_byte_length) — PKCS#1 type 2 padding
    2. doPublic(padded) — raw modular exponentiation
    3. result.toString(16) — to hex
    4. b16to64(hex) — custom base64 encoding

    The bytes2string conversion in cryptico converts byte array to string
    where each byte becomes a char code.
    """
    key_size = (pub_key.n.bit_length() + 7) // 8

    # PKCS#1 type 2 padding (matching pkcs1pad2 in jsbn.js)
    if len(data) > key_size - 11:
        raise ValueError(f"Data too long for RSA key: {len(data)} > {key_size - 11}")

    # Build padded block: 0x00 0x02 [random_nonzero_bytes] 0x00 [data]
    pad_len = key_size - len(data) - 3
    padding = bytearray(pad_len)
    for i in range(pad_len):
        while True:
            b = os.urandom(1)[0]
            if b != 0:
                padding[i] = b
                break
    padded = b'\x00\x02' + bytes(padding) + b'\x00' + data

    # Raw RSA: m^e mod n
    m = int.from_bytes(padded, 'big')
    c = pow(m, pub_key.e, pub_key.n)

    # To hex string (matching BigInteger.toString(16))
    c_hex = format(c, 'x')
    # Ensure even length
    if len(c_hex) & 1:
        c_hex = '0' + c_hex

    return b16_to_b64(c_hex)


# ---------- Main encrypt function ----------

def encrypt(plaintext: str, public_key_b64: str) -> str:
    """Encrypt plaintext with cryptico, matching cryptico.encrypt() output.

    Returns: base64(RSA(aes_key)) + "?" + b256_b64(AES-CBC(plaintext))
    """
    # 1. Generate random 32-byte AES key
    aes_key = list(os.urandom(32))

    # 2. RSA-encrypt the AES key
    pub_key = public_key_from_string(public_key_b64)
    aes_key_bytes = bytes(aes_key)
    rsa_encrypted = rsa_encrypt_bytes(aes_key_bytes, pub_key)

    # 3. AES-CBC encrypt the plaintext
    plaintext_bytes = [ord(c) for c in plaintext]
    aes_encrypted = cryptico_aes_cbc_encrypt(plaintext_bytes, aes_key)

    # 4. Combine
    return rsa_encrypted + "?" + aes_encrypted


# ---------- Test ----------

if __name__ == "__main__":
    # Test with the captured public key
    pub_key = "baA/RgjURU3I0uqH3iRos3NbE8fT+lP8SDXKymsnfdPrMQAEoMBuXtoaQiJ1i5tuBG9EgSEOH1LAZEaAsvwClw=="

    test_payload = '{"amount":27,"country":"CM","currency":"XAF","email":"test@test.com","network":"Orangemoney","phonenumber":"696080087"}'

    encrypted = encrypt(test_payload, pub_key)
    print(f"Encrypted length: {len(encrypted)}")
    print(f"Format OK: {'?' in encrypted}")
    parts = encrypted.split("?")
    print(f"RSA part: {len(parts[0])} chars")
    print(f"AES part: {len(parts[1])} chars")
    print(f"\nFirst 100 chars: {encrypted[:100]}...")
