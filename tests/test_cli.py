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


def _find_line(output: str, prefix: str) -> str:
    return next(line for line in output.splitlines() if prefix in line)


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
        id1 = _find_line(r1.output, "Connection-id:")
        id2 = _find_line(r2.output, "Connection-id:")
        assert id1 != id2

    def test_pinned_via_env_var(self) -> None:
        env = {**ENV, "PIPEGATE_CONNECTION_ID": "pinned"}
        result = runner.invoke(app, ["token"], env=env)
        assert "Connection-id: pinned" in result.output

    def test_generated_token_is_verifiable(self) -> None:
        env = {**ENV, "PIPEGATE_CONNECTION_ID": "verifyme"}
        result = runner.invoke(app, ["token"], env=env)
        assert result.exit_code == 0
        raw = _find_line(result.output, "JWT Bearer:").split("JWT Bearer:")[1].strip()
        decoded = pyjwt.decode(raw, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert decoded["sub"] == "verifyme"

    def test_missing_jwt_secret_exits_nonzero(self) -> None:
        clean = {k: v for k, v in os.environ.items() if k != "PIPEGATE_JWT_SECRET"}
        with patch.dict(os.environ, clean, clear=True):
            result = runner.invoke(app, ["token"])
        assert result.exit_code != 0
