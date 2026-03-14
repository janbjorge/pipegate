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


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject required env vars for Settings before every test."""
    monkeypatch.setenv("PIPEGATE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("PIPEGATE_JWT_ALGORITHMS", '["HS256"]')


@pytest.fixture
def settings() -> Settings:
    return Settings(_cli_parse_args=False)


@pytest.fixture
def connection_id() -> str:
    return uuid.uuid4().hex


def make_token(
    connection_id: str,
    *,
    secret: str = JWT_SECRET,
    algorithm: str = JWT_ALGORITHM,
    expires_in: timedelta = timedelta(days=1),
) -> str:
    """Helper to mint a JWT token for tests."""
    payload = {
        "sub": connection_id,
        "exp": int((datetime.now(UTC) + expires_in).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture
def token(connection_id: str) -> str:
    return make_token(connection_id)


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app() -> FastAPI:
    """Create app with settings pre-injected (no lifespan under ASGI transport)."""
    application = create_app()
    application.extra["settings"] = Settings(_cli_parse_args=False)
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
