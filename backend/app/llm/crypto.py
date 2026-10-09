"""Phase 3 credential encryption at rest (Fernet authenticated encryption).

Centralized so the mechanism can be replaced later without touching every
endpoint. The master key comes ONLY from the server environment
(FIXHUB_CREDENTIAL_ENCRYPTION_KEY); it is never stored in the database,
never returned by APIs, and never passed to the agent.
"""

from __future__ import annotations


class CredentialEncryptionUnavailable(RuntimeError):
    """Master key missing or invalid — BYOK storage refuses to operate."""


def _fernet():
    from cryptography.fernet import Fernet, InvalidToken  # noqa: F401 (re-export guard)

    from app.config import settings

    raw = (settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if not raw:
        raise CredentialEncryptionUnavailable(
            "credential encryption not configured "
            "(set FIXHUB_CREDENTIAL_ENCRYPTION_KEY)"
        )
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception as exc:
        raise CredentialEncryptionUnavailable(
            f"invalid credential encryption key: {exc}"
        ) from exc


def _previous_fernet():
    """Phase 4.5 rotation support: old key, decryption-only. None when unset."""
    from cryptography.fernet import Fernet

    from app.config import settings

    raw = (settings.FIXHUB_CREDENTIAL_ENCRYPTION_KEY_PREVIOUS or "").strip()
    if not raw:
        return None
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception:
        return None


def encrypt_secret(secret: str) -> str:
    """Encrypt a raw API key. Raises CredentialEncryptionUnavailable when unset."""
    if not secret:
        raise ValueError("secret is required")
    token = _fernet().encrypt(secret.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(encrypted: str) -> str:
    """Decrypt to the raw API key (caller must drop it as soon as practical).

    Phase 4.5: falls back to the previous master key (rotation window), so
    mixed-state rows remain readable until rotation completes.
    """
    from cryptography.fernet import InvalidToken

    try:
        raw = _fernet().decrypt((encrypted or "").encode("utf-8"))
    except InvalidToken:
        prev = _previous_fernet()
        if prev is None:
            raise CredentialEncryptionUnavailable("credential cannot be decrypted")
        try:
            raw = prev.decrypt((encrypted or "").encode("utf-8"))
        except InvalidToken as exc:
            raise CredentialEncryptionUnavailable(
                "credential cannot be decrypted"
            ) from exc
    return raw.decode("utf-8")


def key_hint(secret: str) -> str:
    """Safe display hint (last 4 chars only). Not a substitute for encryption."""
    s = secret or ""
    return f"••••{s[-4:]}" if len(s) >= 4 else "••••"


def encrypt_and_verify(secret: str) -> str:
    """Encrypt then immediately verify decryptability (rotation safety)."""
    fresh = encrypt_secret(secret)
    check = _fernet().decrypt(fresh.encode("utf-8")).decode("utf-8")
    if check != secret:
        raise CredentialEncryptionUnavailable("encryption verification failed")
    return fresh
