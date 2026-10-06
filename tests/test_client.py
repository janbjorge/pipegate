from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import orjson

from pipegate.client import handle_request, main
from pipegate.schemas import (
    BufferGateCancel,
    BufferGateChunk,
    BufferGateRequest,
    BufferGateResponse,
)


class TestHandleRequest:
    async def test_successful_forward(self) -> None:
        ws = AsyncMock()
        request = BufferGateRequest(
            correlation_id=uuid.uuid4(),
            url_path="api/data",
            url_query=orjson.dumps([["key", "value"]]).decode(),
            method="GET",
            headers=orjson.dumps({"x-custom": "header"}).decode(),
            body="",
        )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, text="ok", headers={"x-resp": "val"})
            )
        ) as http_client:
            await handle_request("http://localhost:9000", request, http_client, ws)

        ws.send.assert_called_once()
        resp = BufferGateResponse.model_validate_json(ws.send.call_args[0][0])
        assert resp.correlation_id == request.correlation_id
        assert resp.status_code == 200
        assert base64.b64decode(resp.body) == b"ok"
        assert orjson.loads(resp.headers)["x-resp"] == "val"

    async def test_post_with_binary_body(self) -> None:
        ws = AsyncMock()
        binary = bytes(range(256))
        request = BufferGateRequest(
            correlation_id=uuid.uuid4(),
            url_path="upload",
            url_query=orjson.dumps([]).decode(),
            method="POST",
            headers=orjson.dumps({}).decode(),
            body=base64.b64encode(binary).decode(),
        )

        captured: list[bytes] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(req.content)
            return httpx.Response(201, content=binary)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request("http://localhost:9000", request, http_client, ws)

        assert captured[0] == binary
        resp = BufferGateResponse.model_validate_json(ws.send.call_args[0][0])
        assert resp.status_code == 201
        assert base64.b64decode(resp.body) == binary

    async def test_target_error_returns_504(self) -> None:
        ws = AsyncMock()
        request = BufferGateRequest(
            correlation_id=uuid.uuid4(),
            url_path="api/data",
            url_query=orjson.dumps([]).decode(),
            method="GET",
            headers=orjson.dumps({}).decode(),
            body="",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request("http://localhost:9000", request, http_client, ws)

        resp = BufferGateResponse.model_validate_json(ws.send.call_args[0][0])
        assert resp.status_code == 504
        assert resp.body == ""

    async def test_error_response_headers_is_valid_json(self) -> None:
        """PR #24: client error path must emit parseable headers.

        headers="" fails orjson.loads("") → JSONDecodeError if any consumer
        calls orjson.loads without a falsy guard.
        headers="{}" is valid JSON and the correct fix.

        This test FAILS on the original code (headers="") and PASSES on the fix.
        """
        ws = AsyncMock()
        request = BufferGateRequest(
            correlation_id=uuid.uuid4(),
            url_path="api/data",
            url_query=orjson.dumps([]).decode(),
            method="GET",
            headers=orjson.dumps({}).decode(),
            body="",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request("http://localhost:9000", request, http_client, ws)

        resp = BufferGateResponse.model_validate_json(ws.send.call_args[0][0])
        assert resp.status_code == 504
        # Must be parseable JSON — orjson.loads("") raises JSONDecodeError
        parsed = orjson.loads(resp.headers)
        assert parsed == {}


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


class TestStreaming:
    def _request(self) -> BufferGateRequest:
        return BufferGateRequest(
            correlation_id=uuid.uuid4(),
            url_path="stream",
            url_query=orjson.dumps([]).decode(),
            method="GET",
            headers=orjson.dumps({}).decode(),
            body="",
        )

    async def test_event_stream_is_sent_as_head_and_pieces(self) -> None:
        ws = AsyncMock()
        request = self._request()

        async def events() -> AsyncIterator[bytes]:
            yield b"data: 1\n\n"
            yield b"data: 2\n\n"

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "content-length": "18"},
                content=events(),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request("http://localhost:9000", request, http_client, ws)

        sent = [orjson.loads(c.args[0]) for c in ws.send.call_args_list]
        head = BufferGateResponse.model_validate(sent[0])
        assert head.stream and head.status_code == 200 and head.body == ""
        assert "content-length" not in orjson.loads(head.headers)
        chunks = [BufferGateChunk.model_validate(m) for m in sent[1:]]
        assert b"".join(base64.b64decode(c.body) for c in chunks) == (
            b"data: 1\n\ndata: 2\n\n"
        )
        assert [c.done for c in chunks] == [False, False, True]

    async def test_cancel_stops_a_stream(self) -> None:
        request = self._request()
        cancel = BufferGateCancel(correlation_id=request.correlation_id)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def endless() -> AsyncIterator[bytes]:
            try:
                while True:
                    started.set()
                    yield b"data: tick\n\n"
                    await asyncio.sleep(0.01)
            finally:
                stopped.set()

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=endless()
            )

        sent: list[str] = []
        inbox: asyncio.Queue[str] = asyncio.Queue()
        await inbox.put(request.model_dump_json())

        ws = AsyncMock()
        ws.recv.side_effect = inbox.get
        ws.send.side_effect = sent.append
        connect_cm = AsyncMock()
        connect_cm.__aenter__.return_value = ws
        connect_cm.__aexit__.return_value = False
        real_client = httpx.AsyncClient

        def mock_client(*a: object, **kw: object) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(handler))

        with (
            patch("pipegate.client.connect", return_value=connect_cm),
            patch("pipegate.client.httpx.AsyncClient", side_effect=mock_client),
        ):
            task = asyncio.create_task(main("http://localhost:9000", "ws://fake/"))
            await asyncio.wait_for(started.wait(), 2)
            await inbox.put(cancel.model_dump_json())
            await asyncio.wait_for(stopped.wait(), 2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert BufferGateResponse.model_validate_json(sent[0]).stream
