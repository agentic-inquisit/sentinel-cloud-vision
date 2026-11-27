"""Device identity tokens for the ingest service.

Devices send `authorization: Bearer <token>`. The token's claims decide routing:
device_class becomes the x-sentinel-device-class header, so a device cannot
choose its own pool. Tokens are signed with INGEST_JWT_SECRET (a key separate from
the user/admin apps') and carry audience "sentinel-ingest".

Issue one:
    INGEST_JWT_SECRET=... python -m streaming.devices <device_id> <camera|mobile|edge-agent> <tenant> [ttl_hours]

Device mTLS is planned to replace this for provisioned hardware; mobile apps keep tokens.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from streaming.routing import DEVICE_CLASSES

ALGORITHM = "HS256"
AUDIENCE = "sentinel-ingest"
MIN_SECRET_LEN = 32


class InvalidDeviceToken(Exception):
    pass


@dataclass(frozen=True)
class Device:
    device_id: str
    device_class: str
    tenant: str


def load_secret() -> str:
    secret = os.environ.get("INGEST_JWT_SECRET", "").strip()
    if len(secret) < MIN_SECRET_LEN:
        raise RuntimeError(f"INGEST_JWT_SECRET must be set to a random value of at least {MIN_SECRET_LEN} characters")
    return secret


def issue(secret: str, device_id: str, device_class: str, tenant: str, ttl: timedelta = timedelta(hours=24)) -> str:
    if device_class not in DEVICE_CLASSES:
        raise ValueError(f"device_class must be one of {DEVICE_CLASSES}")
    if not device_id or not tenant:
        raise ValueError("device_id and tenant are required")
    now = datetime.now(timezone.utc)
    claims = {"sub": device_id, "device_class": device_class, "tenant": tenant,
              "aud": AUDIENCE, "iat": now, "exp": now + ttl}
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify(secret: str, token: str) -> Device:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], audience=AUDIENCE,
                            options={"require": ["sub", "device_class", "tenant", "aud", "exp", "iat"]})
    except jwt.InvalidTokenError as exc:
        raise InvalidDeviceToken(str(exc)) from exc
    if claims["device_class"] not in DEVICE_CLASSES:
        raise InvalidDeviceToken(f"unknown device_class {claims['device_class']!r}")
    return Device(device_id=claims["sub"], device_class=claims["device_class"], tenant=claims["tenant"])


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    ttl = timedelta(hours=float(argv[3])) if len(argv) == 4 else timedelta(hours=24)
    try:
        print(issue(load_secret(), argv[0], argv[1], argv[2], ttl))
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
