from __future__ import annotations

import time
from datetime import timedelta

import jwt
import pytest

from pipegate.auth import TokenResult, generate_token, verify_token
from pipegate.schemas import JWTPayload, Settings

from .conftest import make_token


class TestVerifyToken:
    def test_valid_token(self, connection_id: str, settings: Settings) -> None:
        token = make_token(connection_id)
        result = verify_token(token, settings)

        assert isinstance(result, JWTPayload)
        assert result.sub == connection_id

    def test_expired_token(self, connection_id: str, settings: Settings) -> None:
        token = make_token(connection_id, expires_in=timedelta(seconds=-1))

        with pytest.raises(jwt.ExpiredSignatureError):
            verify_token(token, settings)

    def test_wrong_secret(self, connection_id: str, settings: Settings) -> None:
        token = make_token(connection_id, secret="wrong-secret-that-is-long-enough!!")

        with pytest.raises(jwt.InvalidSignatureError):
            verify_token(token, settings)

    def test_malformed_token(self, settings: Settings) -> None:
        with pytest.raises(jwt.DecodeError):
            verify_token("not.a.jwt", settings)

    def test_empty_token(self, settings: Settings) -> None:
        with pytest.raises(jwt.DecodeError):
            verify_token("", settings)


class TestGenerateToken:
    def test_returns_token_result(self, settings: Settings) -> None:
        result = generate_token(settings)
        assert isinstance(result, TokenResult)
        assert isinstance(result.connection_id, str)
        assert isinstance(result.bearer, str)

    def test_generated_token_is_valid(self, settings: Settings) -> None:
        result = generate_token(settings)
        payload = verify_token(result.bearer, settings)
        assert payload.sub == result.connection_id

    def test_explicit_id_takes_precedence(self, settings: Settings) -> None:
        result = generate_token(settings, connection_id="explicit")
        assert result.connection_id == "explicit"
        assert verify_token(result.bearer, settings).sub == "explicit"

    def test_falls_back_to_settings_connection_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPEGATE_CONNECTION_ID", "from-env")
        assert generate_token(Settings()).connection_id == "from-env"

    def test_explicit_overrides_settings_connection_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPEGATE_CONNECTION_ID", "from-env")
        result = generate_token(Settings(), connection_id="explicit")
        assert result.connection_id == "explicit"

    def test_random_when_nothing_pinned(self, settings: Settings) -> None:
        cid1 = generate_token(settings).connection_id
        cid2 = generate_token(settings).connection_id
        assert cid1 != cid2

    def test_token_expires_in_21_days(self, settings: Settings) -> None:
        result = generate_token(settings)
        decoded = jwt.decode(
            result.bearer,
            settings.jwt_secret.get_secret_value(),
            algorithms=settings.jwt_algorithms,
        )
        days_21 = 21 * 24 * 3600
        assert abs(decoded["exp"] - (int(time.time()) + days_21)) < 60
