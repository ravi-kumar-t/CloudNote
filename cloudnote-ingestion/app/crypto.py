"""
Zero-dependency authenticated symmetric encryption for portal credential storage.
Uses HMAC-SHA256 Counter Mode (Encrypt-then-MAC) with standard library hashlib, hmac, os, and base64.
"""
import os
import hmac
import hashlib
import base64
from typing import Optional

def encrypt_portal_password(password: str, secret: str) -> str:
    """
    Encrypts a plaintext portal password using an HMAC-SHA256 CTR keystream
    with an Encrypt-then-MAC authentication tag.
    Returns a URL-safe Base64-encoded ciphertext string.
    """
    if not password:
        return ""
    if not secret:
        raise ValueError("Encryption secret key cannot be empty.")

    key = hashlib.sha256(secret.encode("utf-8")).digest()
    iv = os.urandom(16)
    p_bytes = password.encode("utf-8")

    # Generate CTR keystream blocks
    blocks = []
    num_blocks = (len(p_bytes) + 31) // 32
    for i in range(num_blocks):
        ctr = i.to_bytes(4, "big")
        block = hmac.new(key, iv + ctr, hashlib.sha256).digest()
        blocks.append(block)

    keystream = b"".join(blocks)[:len(p_bytes)]
    ciphertext = bytes(a ^ b for a, b in zip(p_bytes, keystream))

    # Encrypt-then-MAC authentication tag
    tag = hmac.new(key, iv + ciphertext, hashlib.sha256).digest()

    # Format: IV (16 bytes) + Tag (32 bytes) + Ciphertext (N bytes)
    raw = iv + tag + ciphertext
    return base64.urlsafe_b64encode(raw).decode("utf-8")

def decrypt_portal_password(token: str, secret: str) -> str:
    """
    Decrypts a URL-safe Base64-encoded ciphertext string back to plaintext.
    Verifies the HMAC authentication tag in constant time prior to decryption.
    """
    if not token:
        return ""
    if not secret:
        raise ValueError("Decryption secret key cannot be empty.")

    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
    except Exception as e:
        raise ValueError(f"Malformed ciphertext encoding: {e}")

    if len(raw) < 48:
        raise ValueError("Ciphertext payload is too short to be valid.")

    iv = raw[:16]
    tag = raw[16:48]
    ciphertext = raw[48:]

    key = hashlib.sha256(secret.encode("utf-8")).digest()
    expected_tag = hmac.new(key, iv + ciphertext, hashlib.sha256).digest()

    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Authentication tag mismatch: ciphertext corrupted or tampered.")

    # Generate matching CTR keystream blocks
    blocks = []
    num_blocks = (len(ciphertext) + 31) // 32
    for i in range(num_blocks):
        ctr = i.to_bytes(4, "big")
        block = hmac.new(key, iv + ctr, hashlib.sha256).digest()
        blocks.append(block)

    keystream = b"".join(blocks)[:len(ciphertext)]
    plaintext_bytes = bytes(a ^ b for a, b in zip(ciphertext, keystream))
    return plaintext_bytes.decode("utf-8")
