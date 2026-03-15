from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from pipegate.auth import verify_token
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
