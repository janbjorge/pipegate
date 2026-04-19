from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from typing import cast

import orjson
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from pipegate.schemas import BufferGateRequest, BufferGateResponse, Settings
from pipegate.server import create_app

from .conftest import make_token

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = create_app()
    app.extra["settings"] = Settings()
    return app


async def _ws_roundtrip(
    app: FastAPI,
    connection_id: str,
    token: str,
    *,
    method: str = "GET",
    path: str = "test-path",
    body: str = "",
    query: str = "",
    response_body: bytes = b"tunnel-response",
    response_status: int = 200,
    response_headers: str | None = None,
) -> tuple[Response, dict[str, str]]:
    """Full tunnel round-trip: HTTP -> WS forward -> WS response -> HTTP."""
    transport = ASGITransport(app=app)

    url = f"/{connection_id}/{path}"
    if query:
        url += f"?{query}"

    forwarded_request: dict[str, str] = {}

    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def http_request() -> Response:
            return await client.request(method, url, content=body)

        async def ws_client() -> None:
            scope: dict[str, object] = {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "path": "/",
                "query_string": f"token={token}".encode(),
                "headers": [],
            }
            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()

            await inbox.put({"type": "websocket.connect"})
            app_task = asyncio.create_task(
                app(scope, inbox.get, outbox.put),  # type: ignore[arg-type]
            )

            msg = await asyncio.wait_for(outbox.get(), timeout=5)
            assert msg["type"] == "websocket.accept"

            msg = await asyncio.wait_for(outbox.get(), timeout=5)
            assert msg["type"] == "websocket.send"

            fwd = json.loads(cast(str, msg["text"]))
            forwarded_request.update(fwd)

            response = BufferGateResponse(
                correlation_id=fwd["correlation_id"],
                headers=response_headers
                if response_headers is not None
                else orjson.dumps({"x-tunnel": "ok"}).decode(),
                body=base64.b64encode(response_body).decode(),
                status_code=response_status,
            )
            await inbox.put(
                {"type": "websocket.receive", "text": response.model_dump_json()}
            )

            await asyncio.sleep(0.05)
            await inbox.put({"type": "websocket.disconnect"})
            with contextlib.suppress(Exception):
                await asyncio.wait_for(app_task, timeout=2)

        ws_task = asyncio.create_task(ws_client())
        await asyncio.sleep(0.01)
        http_task = asyncio.create_task(http_request())

        await ws_task
        resp = await asyncio.wait_for(http_task, timeout=10)

    return resp, forwarded_request


async def _connect_ws(
    app: FastAPI,
    *,
    token: str | None = None,
) -> str:
    """Attempt a WS connection, return the first server message type."""
    query = f"token={token}" if token else ""
    scope: dict[str, object] = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "path": "/",
        "query_string": query.encode(),
        "headers": [],
    }
    inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    app_task = asyncio.create_task(
        app(scope, inbox.get, outbox.put)  # type: ignore[arg-type]
    )
    msg = await asyncio.wait_for(outbox.get(), timeout=3)
    await inbox.put({"type": "websocket.disconnect"})
    with contextlib.suppress(Exception):
        await asyncio.wait_for(app_task, timeout=2)
    return cast(str, msg["type"])


# ---------------------------------------------------------------------------
# Tunnel round-trip
# ---------------------------------------------------------------------------


class TestTunnelRoundTrip:
    async def test_get(self, connection_id: str) -> None:
        resp, fwd = await _ws_roundtrip(
            _make_app(), connection_id, make_token(connection_id)
        )
        assert resp.status_code == 200
        assert resp.text == "tunnel-response"
        assert fwd["method"] == "GET"
        assert fwd["url_path"] == "test-path"

    async def test_post_with_body(self, connection_id: str) -> None:
        resp, fwd = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            method="POST",
            body="hello",
        )
        assert resp.status_code == 200
        assert fwd["method"] == "POST"
        assert base64.b64decode(fwd["body"]) == b"hello"

    async def test_preserves_query_params(self, connection_id: str) -> None:
        resp, fwd = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            query="a=1&a=2&b=3",
        )
        assert resp.status_code == 200
        query = json.loads(fwd["url_query"])
        assert isinstance(query, list)
        a_vals = sorted(v for k, v in query if k == "a")
        assert a_vals == ["1", "2"]
        assert [v for k, v in query if k == "b"] == ["3"]

    async def test_custom_response_status(self, connection_id: str) -> None:
        resp, _ = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            response_status=404,
            response_body=b"not found",
        )
        assert resp.status_code == 404
        assert resp.text == "not found"

    async def test_binary_body_roundtrip(self, connection_id: str) -> None:
        """Binary (non-UTF-8) bodies survive the tunnel."""
        binary = bytes(range(256))
        resp, fwd = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            method="POST",
            body=binary,  # type: ignore[arg-type]
            response_body=binary,
        )
        assert resp.status_code == 200
        assert resp.content == binary
        assert base64.b64decode(fwd["body"]) == binary

    async def test_head_response_has_no_body(self, connection_id: str) -> None:
        resp, _ = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            method="HEAD",
            response_body=b"hello",
        )
        assert resp.status_code == 200
        assert resp.content == b""


# ---------------------------------------------------------------------------
# Disconnect behaviour
# ---------------------------------------------------------------------------


class TestDisconnect:
    async def test_future_fails_on_ws_disconnect_during_send(
        self, connection_id: str
    ) -> None:
        from fastapi.websockets import WebSocketDisconnect as _WSD

        app = _make_app()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            send_attempted = asyncio.Event()

            async def ws_side() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": "/",
                    "query_string": f"token={token}".encode(),
                    "headers": [],
                }
                inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                await inbox.put({"type": "websocket.connect"})

                async def patched_send(message: dict[str, object]) -> None:
                    if message.get("type") == "websocket.send":
                        send_attempted.set()
                        raise _WSD(code=1006, reason="simulated disconnect")
                    await outbox.put(message)

                app_task = asyncio.create_task(
                    app(scope, inbox.get, patched_send),  # type: ignore[arg-type]
                )
                msg = await asyncio.wait_for(outbox.get(), timeout=5)
                assert msg["type"] == "websocket.accept"
                await asyncio.wait_for(send_attempted.wait(), timeout=5)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=3)

            ws_task = asyncio.create_task(ws_side())
            await asyncio.sleep(0.01)

            http_resp = await asyncio.wait_for(
                client.get(f"/{connection_id}/ping"), timeout=5
            )
            await ws_task

        assert http_resp.status_code == 502

    async def test_buffer_cleaned_up_after_disconnect(self, connection_id: str) -> None:
        app = _make_app()
        token = make_token(connection_id)

        scope: dict[str, object] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "path": "/",
            "query_string": f"token={token}".encode(),
            "headers": [],
        }
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        await inbox.put({"type": "websocket.connect"})
        app_task = asyncio.create_task(
            app(scope, inbox.get, outbox.put),  # type: ignore[arg-type]
        )
        msg = await asyncio.wait_for(outbox.get(), timeout=5)
        assert msg["type"] == "websocket.accept"

        await inbox.put({"type": "websocket.disconnect"})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(app_task, timeout=2)

        buffers: dict[str, object] = app.extra.get("buffers", {})
        assert connection_id not in buffers


# ---------------------------------------------------------------------------
# WebSocket authentication
# ---------------------------------------------------------------------------


class TestWebSocketAuth:
    async def test_rejected_without_token(self) -> None:
        assert await _connect_ws(_make_app()) == "websocket.close"

    async def test_rejected_with_expired_token(self, connection_id: str) -> None:
        from datetime import timedelta

        expired = make_token(connection_id, expires_in=timedelta(seconds=-1))
        assert await _connect_ws(_make_app(), token=expired) == "websocket.close"

    async def test_accepted_with_valid_token(self, connection_id: str) -> None:
        valid = make_token(connection_id)
        assert await _connect_ws(_make_app(), token=valid) == "websocket.accept"


# ---------------------------------------------------------------------------
# Body size limit
# ---------------------------------------------------------------------------


class TestBodySizeLimit:
    async def test_oversized_body_rejected_with_413(self, connection_id: str) -> None:
        app = _make_app()
        app.extra["settings"].max_body_bytes = 10
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/{connection_id}/upload", content=b"x" * 100)

        assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Queue backpressure
# ---------------------------------------------------------------------------


class TestQueueBackpressure:
    async def test_503_when_queue_full(self, connection_id: str) -> None:
        app = create_app()
        settings = Settings()
        settings.max_queue_depth = 1
        app.extra["settings"] = settings

        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(
                client.post(f"/{connection_id}/req1", content=b"a")
            )
            await asyncio.sleep(0.05)
            second = await asyncio.wait_for(
                client.post(f"/{connection_id}/req2", content=b"b"), timeout=2
            )
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first

        assert second.status_code == 503


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_healthz(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# PR #24: empty headers in WS error response
# ---------------------------------------------------------------------------

#
# Server guard: orjson.loads(response.headers) if response.headers else {}
# headers="" is falsy → safe today. headers="{}" is valid JSON either way.
# orjson.loads("") raises JSONDecodeError, so "{}" is the correct value for
# any consumer without the guard.


class TestEmptyHeadersHandling:
    async def test_empty_string_headers_does_not_crash_server(
        self, connection_id: str
    ) -> None:
        # server guard makes "" safe — passes on both old and new code
        resp, _ = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            response_headers="",
            response_body=b"",
            response_status=504,
        )
        assert resp.status_code == 504

    async def test_empty_json_object_headers_works(self, connection_id: str) -> None:
        resp, _ = await _ws_roundtrip(
            _make_app(),
            connection_id,
            make_token(connection_id),
            response_headers="{}",
            response_body=b"",
            response_status=504,
        )
        assert resp.status_code == 504

    def test_empty_string_is_not_valid_json(self) -> None:
        # Guard is load-bearing: removing it would crash on headers=""
        import pytest

        with pytest.raises(ValueError):
            orjson.loads("")
        assert orjson.loads("{}") == {}


# ---------------------------------------------------------------------------
# PR #24: send() must use the queue captured at connect, not dict re-lookup
# ---------------------------------------------------------------------------

#
# If buffers[connection_id] is replaced while a WS is active, the original
# send() (dict lookup each iteration) drains the replacement queue silently.
# The fix captures `queue` as a local variable at connect time.


class TestQueueStability:
    async def test_send_uses_captured_queue_not_dict_lookup(
        self, connection_id: str
    ) -> None:
        """Replace buffers[connection_id] while send() is blocked; verify send()
        ignores the replacement (fixed) rather than draining it (original bug)."""
        app = _make_app()
        token = make_token(connection_id)
        scope: dict[str, object] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "path": "/",
            "query_string": f"token={token}".encode(),
            "headers": [],
        }
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        await inbox.put({"type": "websocket.connect"})
        app_task = asyncio.create_task(app(scope, inbox.get, outbox.put))  # type: ignore[arg-type]

        msg = await asyncio.wait_for(outbox.get(), timeout=5)
        assert msg["type"] == "websocket.accept"

        buffers: dict[str, asyncio.Queue[BufferGateRequest]] = app.extra["buffers"]
        replacement: asyncio.Queue[BufferGateRequest] = asyncio.Queue(maxsize=100)
        buffers[connection_id] = replacement

        replacement.put_nowait(
            BufferGateRequest(
                correlation_id=uuid.uuid4(),
                url_path="injected",
                url_query="[]",
                method="GET",
                headers="{}",
                body="",
            )
        )

        await asyncio.sleep(0.05)
        received_from_replacement = not outbox.empty()

        await inbox.put({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(app_task, timeout=2)

        assert not received_from_replacement, (
            "send() read from a replacement queue — it must use the captured queue ref"
        )
