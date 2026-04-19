from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt

from .schemas import JWTPayload, Settings

ISSUER = "pipegate"
AUDIENCE = "pipegate"
_TOKEN_TTL = timedelta(days=21)


class TokenResult(NamedTuple):
    connection_id: str
    bearer: str


def generate_token(
    settings: Settings, connection_id: str | None = None
) -> TokenResult:
    """Create a connection ID and signed JWT with full standard claims."""
    cid = connection_id or settings.connection_id or uuid.uuid4().hex
    now = datetime.now(UTC)
    ts = int(now.timestamp())
    payload = JWTPayload(
        sub=cid,
        exp=int((now + _TOKEN_TTL).timestamp()),
        nbf=ts,
        iat=ts,
        iss=ISSUER,
        aud=AUDIENCE,
        jti=uuid.uuid4().hex,
    )
    token = jwt.encode(
        payload.model_dump(mode="json"),
        key=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithms[0],
    )
    return TokenResult(connection_id=cid, bearer=token)


def verify_token(token: str, settings: Settings) -> JWTPayload:
    """Decode and verify a JWT, validating all standard claims."""
    decoded = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=settings.jwt_algorithms,
        audience=AUDIENCE,
        issuer=ISSUER,
    )
    return JWTPayload.model_validate(decoded)
