from __future__ import annotations

import os
from unittest.mock import patch

from typer.testing import CliRunner

from pipegate.auth import verify_token
from pipegate.cli import app
from pipegate.schemas import Settings

from .conftest import JWT_ALGORITHM, JWT_SECRET

runner = CliRunner()

ENV = {
    "PIPEGATE_JWT_SECRET": JWT_SECRET,
    "PIPEGATE_JWT_ALGORITHMS": '["HS256"]',
}


class TestTokenCommand:
    def test_exits_zero(self) -> None:
        result = runner.invoke(app, ["token"], env=ENV)
        assert result.exit_code == 0, result.output

    def test_output_has_connection_id_and_bearer(self) -> None:
        result = runner.invoke(app, ["token"], env=ENV)
        assert "Connection-id:" in result.output
        assert "JWT Bearer:" in result.output

    def test_pinned_connection_id(self) -> None:
        result = runner.invoke(app, ["token", "--connection-id", "myconn123"], env=ENV)
        assert result.exit_code == 0
        assert "Connection-id: myconn123" in result.output

    def test_short_flag_connection_id(self) -> None:
        result = runner.invoke(app, ["token", "-c", "short123"], env=ENV)
        assert result.exit_code == 0
        assert "Connection-id: short123" in result.output

    def test_generated_token_is_verifiable(self) -> None:
        result = runner.invoke(app, ["token", "--connection-id", "verifyme"], env=ENV)
        assert result.exit_code == 0

        import jwt as pyjwt

        token_line = [l for l in result.output.splitlines() if "JWT Bearer:" in l][0]
        token = token_line.split("JWT Bearer:")[1].strip()

        decoded = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert decoded["sub"] == "verifyme"

    def test_random_ids_differ(self) -> None:
        r1 = runner.invoke(app, ["token"], env=ENV)
        r2 = runner.invoke(app, ["token"], env=ENV)
        id1 = [l for l in r1.output.splitlines() if "Connection-id:" in l][0]
        id2 = [l for l in r2.output.splitlines() if "Connection-id:" in l][0]
        assert id1 != id2

    def test_missing_jwt_secret_exits_nonzero(self) -> None:
        # Remove PIPEGATE_JWT_SECRET from the actual environment (autouse fixture set it)
        clean = {k: v for k, v in os.environ.items() if k != "PIPEGATE_JWT_SECRET"}
        with patch.dict(os.environ, clean, clear=True):
            result = runner.invoke(app, ["token"])
        assert result.exit_code != 0
