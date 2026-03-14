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
