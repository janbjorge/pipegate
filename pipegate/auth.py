from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from .schemas import JWTPayload, Settings


def generate_token(
    settings: Settings, connection_id: str | None = None
) -> tuple[str, str]:
    """Create a connection ID and signed JWT. Returns (connection_id, bearer_token)."""
    cid = connection_id or settings.connection_id or uuid.uuid4().hex
    payload = JWTPayload(
        sub=cid,
        exp=int((datetime.now(UTC) + timedelta(days=21)).timestamp()),
    )
    token = jwt.encode(
        payload.model_dump(mode="json"),
        key=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithms[0],
    )
    return cid, token


def verify_token(token: str, settings: Settings) -> JWTPayload:
    """Decode and verify a JWT, returning the payload (connection ID in ``sub``)."""
    decoded = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=settings.jwt_algorithms,
    )
    return JWTPayload.model_validate(decoded)
