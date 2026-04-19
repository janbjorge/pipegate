from __future__ import annotations

import os
from unittest.mock import patch

import jwt as pyjwt
from typer.testing import CliRunner

from pipegate.cli import app

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

    def test_random_ids_differ(self) -> None:
        r1 = runner.invoke(app, ["token"], env=ENV)
        r2 = runner.invoke(app, ["token"], env=ENV)
        id1 = next(l for l in r1.output.splitlines() if "Connection-id:" in l)
        id2 = next(l for l in r2.output.splitlines() if "Connection-id:" in l)
        assert id1 != id2

    def test_pinned_via_env_var(self) -> None:
        result = runner.invoke(app, ["token"], env={**ENV, "PIPEGATE_CONNECTION_ID": "pinned"})
        assert "Connection-id: pinned" in result.output

    def test_generated_token_is_verifiable(self) -> None:
        result = runner.invoke(app, ["token"], env={**ENV, "PIPEGATE_CONNECTION_ID": "verifyme"})
        assert result.exit_code == 0
        token = next(l for l in result.output.splitlines() if "JWT Bearer:" in l).split("JWT Bearer:")[1].strip()
        decoded = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert decoded["sub"] == "verifyme"

    def test_missing_jwt_secret_exits_nonzero(self) -> None:
        clean = {k: v for k, v in os.environ.items() if k != "PIPEGATE_JWT_SECRET"}
        with patch.dict(os.environ, clean, clear=True):
            result = runner.invoke(app, ["token"])
        assert result.exit_code != 0
