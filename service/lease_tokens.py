from __future__ import annotations

import hashlib
import hmac
import secrets


def issue_lease_token() -> str:
    """Return a high-entropy capability that is safe to place in an HTTP header."""
    return secrets.token_urlsafe(32)


def digest_lease_token(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise ValueError("review lease token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lease_token_matches(stored_digest: str | None, presented_token: str | None) -> bool:
    """Compare a presented capability without persisting or logging the raw value."""
    if stored_digest is None or presented_token is None:
        return False
    try:
        presented_digest = digest_lease_token(presented_token)
    except ValueError:
        return False
    return hmac.compare_digest(stored_digest, presented_digest)
