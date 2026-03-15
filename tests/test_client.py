from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import orjson
import pytest

from pipegate.client import handle_request, main
from pipegate.schemas import BufferGateRequest, BufferGateResponse


@pytest.fixture
def correlation_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def sample_request(correlation_id: uuid.UUID) -> BufferGateRequest:
    return BufferGateRequest(
        correlation_id=correlation_id,
        url_path="api/data",
        url_query=orjson.dumps([["key", "value"]]).decode(),
        method="GET",
        headers=orjson.dumps({"x-custom": "header"}).decode(),
        body="",
    )


@pytest.fixture
def ws_client() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# handle_request
# ---------------------------------------------------------------------------


class TestHandleRequest:
    async def test_successful_forward(
        self,
        sample_request: BufferGateRequest,
        ws_client: AsyncMock,
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text="ok", headers={"x-resp": "val"})
            )
        ) as http_client:
            await handle_request(
                "http://localhost:9000", sample_request, http_client, ws_client
            )

        ws_client.send.assert_called_once()
        resp = BufferGateResponse.model_validate_json(ws_client.send.call_args[0][0])
        assert resp.correlation_id == sample_request.correlation_id
        assert resp.status_code == 200
        assert base64.b64decode(resp.body) == b"ok"
        assert orjson.loads(resp.headers)["x-resp"] == "val"

    async def test_post_body_forwarded(
        self,
        correlation_id: uuid.UUID,
        ws_client: AsyncMock,
    ) -> None:
        request = BufferGateRequest(
            correlation_id=correlation_id,
            url_path="submit",
            url_query=orjson.dumps([]).decode(),
            method="POST",
            headers=orjson.dumps({}).decode(),
            body=base64.b64encode(b"payload-data").decode(),
        )

        captured: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["content"] = req.content.decode()
            return httpx.Response(201, text="created")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000", request, http_client, ws_client
            )

        assert captured["method"] == "POST"
        assert captured["content"] == "payload-data"
        resp = BufferGateResponse.model_validate_json(ws_client.send.call_args[0][0])
        assert resp.status_code == 201

    async def test_binary_body(
        self,
        correlation_id: uuid.UUID,
        ws_client: AsyncMock,
    ) -> None:
        binary = bytes(range(256))
        request = BufferGateRequest(
            correlation_id=correlation_id,
            url_path="upload",
            url_query=orjson.dumps([]).decode(),
            method="POST",
            headers=orjson.dumps({}).decode(),
            body=base64.b64encode(binary).decode(),
        )

        captured: list[bytes] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req.content)
            return httpx.Response(200, content=binary)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000", request, http_client, ws_client
            )

        assert captured[0] == binary
        resp = BufferGateResponse.model_validate_json(ws_client.send.call_args[0][0])
        assert base64.b64decode(resp.body) == binary

    async def test_target_error_returns_504(
        self,
        sample_request: BufferGateRequest,
        ws_client: AsyncMock,
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000", sample_request, http_client, ws_client
            )

        resp = BufferGateResponse.model_validate_json(ws_client.send.call_args[0][0])
        assert resp.status_code == 504
        assert resp.body == ""


# ---------------------------------------------------------------------------
# main() reconnection
# ---------------------------------------------------------------------------


class TestMainReconnect:
    async def test_retries_on_connection_refused(self) -> None:
        call_count = 0
        connect_cm = AsyncMock()

        async def side_effect(*a: object, **kw: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionRefusedError("refused")
            raise asyncio.CancelledError()

        connect_cm.__aenter__ = side_effect
        connect_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            contextlib.suppress(asyncio.CancelledError),
            patch("pipegate.client.connect", return_value=connect_cm),
            patch("pipegate.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            await main("http://localhost:9000", "ws://fake:8000/conn")

        assert call_count >= 2

    async def test_backoff_increases(self) -> None:
        delays: list[float] = []
        call_count = 0
        connect_cm = AsyncMock()

        async def side_effect(*a: object, **kw: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise ConnectionRefusedError("refused")
            raise asyncio.CancelledError()

        connect_cm.__aenter__ = side_effect
        connect_cm.__aexit__ = AsyncMock(return_value=False)

        async def mock_sleep(delay: float) -> None:
            delays.append(delay)

        with (
            contextlib.suppress(asyncio.CancelledError),
            patch("pipegate.client.connect", return_value=connect_cm),
            patch("pipegate.client.asyncio.sleep", side_effect=mock_sleep),
        ):
            await main("http://localhost:9000", "ws://fake:8000/conn")

        assert len(delays) >= 2
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1]
