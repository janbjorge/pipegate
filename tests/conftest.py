from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pipegate.schemas import Settings
from pipegate.server import create_app

JWT_SECRET = "test-secret-that-is-long-enough-for-hs256!"
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "pipegate"
JWT_AUDIENCE = "pipegate"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPEGATE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("PIPEGATE_JWT_ALGORITHMS", '["HS256"]')


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def connection_id() -> str:
    return uuid.uuid4().hex


def make_token(
    connection_id: str,
    *,
    secret: str = JWT_SECRET,
    algorithm: str = JWT_ALGORITHM,
    issuer: str = JWT_ISSUER,
    audience: str = JWT_AUDIENCE,
    expires_in: timedelta = timedelta(days=1),
) -> str:
    now = datetime.now(UTC)
    ts = int(now.timestamp())
    payload = {
        "sub": connection_id,
        "exp": int((now + expires_in).timestamp()),
        "nbf": ts,
        "iat": ts,
        "iss": issuer,
        "aud": audience,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture
def app() -> FastAPI:
    application = create_app()
    application.extra["settings"] = Settings()
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
