"""API-key authentication.

Clients send `X-API-Key: <key>`. The server only knows SHA-256 hashes of valid
keys (API_KEY_HASHES, comma-separated), so a leaked config never leaks keys.
Generate keys with keys.py.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Model API key")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parse_key_hashes(raw: str | None) -> frozenset[str]:
    hashes = frozenset(h.strip().lower() for h in (raw or "").replace("\n", ",").split(",") if h.strip())
    if not hashes:
        raise RuntimeError("API_KEY_HASHES is empty: refusing to start without at least one API key")
    bad = [h for h in hashes if not _HASH_RE.match(h)]
    if bad:
        raise RuntimeError(
            f"API_KEY_HASHES has {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} that are not SHA-256 hex "
            "digests; store key hashes from keys.py, never raw keys"
        )
    return hashes


class ApiKeyAuth:
    def __init__(self, key_hashes: frozenset[str]):
        self.key_hashes = key_hashes

    def __call__(self, key: str | None = Security(_header)) -> None:
        if key:
            digest = hash_key(key)
            # Check every hash so timing doesn't reveal which (if any) matched.
            matched = False
            for h in self.key_hashes:
                matched |= hmac.compare_digest(digest, h)
            if matched:
                return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key (send it in the X-API-Key header)",
            headers={"WWW-Authenticate": "ApiKey"},
        )
