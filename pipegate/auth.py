from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from .schemas import JWTPayload, Settings


def verify_token(token: str, connection_id: str, settings: Settings) -> JWTPayload:
    """Decode and verify a JWT token matches the given connection ID."""
    decoded = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=settings.jwt_algorithms,
    )
    payload = JWTPayload.model_validate(decoded)

    if payload.sub != connection_id:
        raise PermissionError("Token UUID does not match path UUID")

    return payload


def make_jwt_bearer() -> None:
    """CLI helper: generate a connection ID and JWT token."""
    settings = Settings(_cli_parse_args=True)  # #10: only here do we want CLI parsing
    connection_id = settings.connection_id or uuid.uuid4().hex

    jwt_payload = JWTPayload(
        sub=connection_id,
        exp=int((datetime.now(UTC) + timedelta(days=21)).timestamp()),
    )

    jwt_bearer = jwt.encode(
        jwt_payload.model_dump(mode="json"),
        key=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithms[0],
    )

    print(f"Connection-id: {connection_id}")
    print(f"JWT Bearer:    {jwt_bearer}")


if __name__ == "__main__":
    make_jwt_bearer()
