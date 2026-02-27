# app/crypto.py
"""
Symmetric encryption for sensitive DB fields (Smuggler Codes).

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography package.
The key is derived from CODE_ENCRYPTION_KEY env var via PBKDF2.

If CODE_ENCRYPTION_KEY is not set, encryption is a no-op (plain text).
This ensures backward compatibility and local dev works without config.
"""
from __future__ import annotations
import base64
import hashlib
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _get_fernet():
    """Build and cache the Fernet instance from env var."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    raw_key = os.environ.get("CODE_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        return None  # no key configured → plain text mode

    # Derive a 32-byte key from the secret using SHA-256
    key_bytes = hashlib.sha256(raw_key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_code(plaintext: str) -> str:
    """
    Encrypt a smuggler code for DB storage.
    Returns 'enc:' + base64(fernet_token) if key is configured,
    otherwise returns plaintext unchanged.
    """
    f = _get_fernet()
    if f is None:
        return plaintext  # no key → store as-is (dev mode)
    token = f.encrypt(plaintext.encode())
    return "enc:" + token.decode()


def decrypt_code(stored: str) -> str:
    """
    Decrypt a stored smuggler code for display.
    Handles both encrypted ('enc:...') and legacy plain text values.
    """
    if not stored.startswith("enc:"):
        return stored  # legacy plain text or no key configured

    f = _get_fernet()
    if f is None:
        # Key was removed after encryption — can't decrypt
        return "***-****-****"

    try:
        token = stored[4:].encode()
        return f.decrypt(token).decode()
    except Exception:
        return "***-****-****"  # decryption failed (wrong key?)
