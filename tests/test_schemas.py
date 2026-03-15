from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from pipegate.schemas import (
    BufferGateRequest,
    BufferGateResponse,
    JWTPayload,
)


class TestBufferGateRequest:
    def test_roundtrip_json(self) -> None:
        req = BufferGateRequest(
            correlation_id=uuid.uuid4(),
            url_path="/hello",
            url_query="{}",
            method="GET",
            headers="{}",
            body="",
        )
        restored = BufferGateRequest.model_validate_json(req.model_dump_json())
        assert restored == req

    def test_invalid_method(self) -> None:
        with pytest.raises(ValidationError):
            BufferGateRequest(
                correlation_id=uuid.uuid4(),
                url_path="/",
                url_query="{}",
                method="INVALID",
                headers="{}",
                body="",
            )

    def test_all_methods_accepted(self) -> None:
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            req = BufferGateRequest(
                correlation_id=uuid.uuid4(),
                url_path="/",
                url_query="{}",
                method=method,
                headers="{}",
                body="",
            )
            assert req.method == method


class TestBufferGateResponse:
    def test_roundtrip_json(self) -> None:
        resp = BufferGateResponse(
            correlation_id=uuid.uuid4(),
            headers='{"content-type": "text/plain"}',
            body="ok",
            status_code=200,
        )
        restored = BufferGateResponse.model_validate_json(resp.model_dump_json())
        assert restored == resp

    def test_missing_status_code(self) -> None:
        with pytest.raises(ValidationError):
            BufferGateResponse(
                correlation_id=uuid.uuid4(),
                headers="{}",
                body="ok",
            )  # type: ignore[call-arg]


class TestJWTPayload:
    def test_valid(self) -> None:
        p = JWTPayload(sub="abc", exp=9999999999)
        assert p.sub == "abc"

    def test_missing_sub(self) -> None:
        with pytest.raises(ValidationError):
            JWTPayload(exp=9999999999)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Refactor #10 — Settings cli_parse_args default must be False
# ---------------------------------------------------------------------------


class TestSettingsCLIParseArgsDefault:
    def test_settings_instantiates_without_cli_flag(self) -> None:
        """
        Settings() must not parse sys.argv by default.
        With cli_parse_args=True (the old default) this would fail under pytest
        because pytest's own argv is not a valid Settings CLI invocation.
        """
        from pipegate.schemas import Settings

        # Must not raise SystemExit or any error despite pytest's sys.argv
        settings = Settings()
        assert settings is not None

    def test_settings_default_is_cli_parse_args_false(self) -> None:
        """The model_config must explicitly set cli_parse_args=False."""
        from pipegate.schemas import Settings

        cfg = Settings.model_config
        assert cfg.get("cli_parse_args") is False, (
            "Settings.model_config must have cli_parse_args=False; "
            f"got {cfg.get('cli_parse_args')!r}"
        )
