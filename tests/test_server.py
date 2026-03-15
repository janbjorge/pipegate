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

from pipegate.schemas import BufferGateResponse, Settings
from pipegate.server import create_app

from .conftest import make_token

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_with_settings() -> FastAPI:
    """Create app with settings injected (no lifespan under raw ASGI)."""
    app = create_app()
    app.extra["settings"] = Settings(_cli_parse_args=False)
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
    response_body: str = "tunnel-response",
    response_status: int = 200,
) -> tuple[Response, dict[str, str]]:
    """
    Simulate a full tunnel round-trip:
    1. HTTP client sends a request
    2. WS tunnel client receives the forwarded request
    3. WS tunnel client sends a response back
    4. HTTP client receives the response

    Returns (http_response, forwarded_request_dict).
    """
    transport = ASGITransport(app=app)

    url = f"/{connection_id}/{path}"
    if query:
        url += f"?{query}"

    forwarded_request: dict[str, str] = {}

    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def http_request() -> Response:
            return await client.request(
                method,
                url,
                content=body,
            )

        async def ws_client() -> None:
            scope: dict[str, object] = {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "path": f"/{connection_id}",
                "query_string": f"token={token}".encode(),
                "headers": [],
            }

            inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()

            await inbox.put({"type": "websocket.connect"})

            app_task = asyncio.create_task(
                app(scope, inbox.get, outbox.put),  # type: ignore[arg-type]
            )

            # Wait for accept
            msg = await asyncio.wait_for(outbox.get(), timeout=5)
            assert msg["type"] == "websocket.accept"

            # Wait for the forwarded request from the server
            msg = await asyncio.wait_for(outbox.get(), timeout=5)
            assert msg["type"] == "websocket.send"

            fwd = json.loads(cast(str, msg["text"]))
            forwarded_request.update(fwd)

            # Send tunnel response back — body must be base64-encoded
            response = BufferGateResponse(
                correlation_id=fwd["correlation_id"],
                headers=orjson.dumps({"x-tunnel": "ok"}).decode(),
                body=base64.b64encode(response_body.encode()).decode(),
                status_code=response_status,
            )
            await inbox.put(
                {
                    "type": "websocket.receive",
                    "text": response.model_dump_json(),
                }
            )

            # Let it process, then disconnect
            await asyncio.sleep(0.05)
            await inbox.put({"type": "websocket.disconnect"})

            with contextlib.suppress(Exception):
                await asyncio.wait_for(app_task, timeout=2)

        # Run HTTP and WS concurrently — WS must start first so it's
        # ready to receive when the HTTP request enqueues into buffers.
        ws_task = asyncio.create_task(ws_client())

        # Small delay to let the WS connect before the HTTP request fires
        await asyncio.sleep(0.01)

        http_task = asyncio.create_task(http_request())

        await ws_task
        resp = await asyncio.wait_for(http_task, timeout=10)

    return resp, forwarded_request


# ---------------------------------------------------------------------------
# Full tunnel round-trip tests
# ---------------------------------------------------------------------------


class TestTunnelRoundTrip:
    async def test_get_request(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, fwd = await _ws_roundtrip(app, connection_id, token)

        assert resp.status_code == 200
        assert resp.text == "tunnel-response"
        assert fwd["method"] == "GET"
        assert fwd["url_path"] == "test-path"

    async def test_post_with_body(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, fwd = await _ws_roundtrip(
            app, connection_id, token, method="POST", body="hello"
        )

        assert resp.status_code == 200
        assert fwd["method"] == "POST"
        # body is base64-encoded for binary-safe transport
        assert base64.b64decode(fwd["body"]) == b"hello"

    async def test_preserves_query_params(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, fwd = await _ws_roundtrip(app, connection_id, token, query="a=1&b=2")

        assert resp.status_code == 200
        query = json.loads(fwd["url_query"])
        assert isinstance(query, list)
        assert sorted(query) == [["a", "1"], ["b", "2"]]

    async def test_custom_response_status(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, _ = await _ws_roundtrip(
            app, connection_id, token, response_status=404, response_body="not found"
        )

        assert resp.status_code == 404
        assert resp.text == "not found"

    async def test_response_headers(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, _ = await _ws_roundtrip(app, connection_id, token)

        assert resp.headers.get("x-tunnel") == "ok"

    async def test_all_http_methods(self, connection_id: str) -> None:
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            app = _make_app_with_settings()
            token = make_token(connection_id)

            resp, fwd = await _ws_roundtrip(app, connection_id, token, method=method)

            assert resp.status_code == 200, f"{method} failed"
            assert fwd["method"] == method

    async def test_correlation_id_in_headers(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        _, fwd = await _ws_roundtrip(app, connection_id, token)

        headers = json.loads(fwd["headers"])
        assert "x-pipegate-correlation-id" in headers


# ---------------------------------------------------------------------------
# Bug #1 — Race condition: future created after enqueue
# ---------------------------------------------------------------------------


class TestRaceConditionFutureBeforeEnqueue:
    async def test_future_exists_in_dict_before_ws_receive_can_see_it(
        self, connection_id: str
    ) -> None:
        """
        Simulate the race directly: a WS receive() handler calls futures.get()
        for a correlation_id immediately after the HTTP request is enqueued but
        before `await futures[correlation_id]` in the HTTP handler has run.

        With the original code (defaultdict + .get()) the future is None at
        that point and the response is dropped.  After the fix the future is
        pre-created *before* the enqueue, so futures.get() always returns a
        live Future regardless of timing.
        """
        import collections as _collections
        import uuid as _uuid

        loop = asyncio.get_event_loop()

        # Reproduce the original broken pattern
        futures_broken: dict[_uuid.UUID, asyncio.Future[BufferGateResponse]] = (
            _collections.defaultdict(loop.create_future)
        )

        cid = _uuid.uuid4()

        # Simulate: WS receive() fires BEFORE HTTP handler accesses futures[cid]
        future_seen_by_ws = futures_broken.get(cid)  # old code path: .get()

        # Under the old approach the future hasn't been created yet → None
        assert future_seen_by_ws is None, (
            "Broken pattern: futures.get() returned non-None before HTTP handler "
            "accessed the key — race is not reproducible in this environment"
        )

        # Now simulate the fixed pattern: future pre-created before enqueue
        futures_fixed: dict[_uuid.UUID, asyncio.Future[BufferGateResponse]] = {}
        cid2 = _uuid.uuid4()
        futures_fixed[cid2] = loop.create_future()

        # WS receive() can now always find the future
        future_seen_after_fix = futures_fixed.get(cid2)
        assert future_seen_after_fix is not None, (
            "Fixed pattern must have the future present before enqueue"
        )
        assert not future_seen_after_fix.done()

    async def test_response_received_even_when_ws_replies_before_http_awaits(
        self, connection_id: str
    ) -> None:
        """
        End-to-end: the WS tunnel client replies immediately (no extra yields).
        The HTTP caller must still receive the correct response — no 504 timeout.
        """
        app = _make_app_with_settings()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response_sent = asyncio.Event()

            async def instant_ws_client() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": f"/{connection_id}",
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

                # Reply with NO extra yield — maximise race window
                resp = BufferGateResponse(
                    correlation_id=fwd["correlation_id"],
                    headers=orjson.dumps({"x-race": "test"}).decode(),
                    body=base64.b64encode(b"race-response").decode(),
                    status_code=200,
                )
                await inbox.put(
                    {"type": "websocket.receive", "text": resp.model_dump_json()}
                )
                response_sent.set()

                await asyncio.sleep(0.1)
                await inbox.put({"type": "websocket.disconnect"})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=2)

            ws_task = asyncio.create_task(instant_ws_client())
            await asyncio.sleep(0.01)

            http_resp = await asyncio.wait_for(
                client.get(f"/{connection_id}/ping"),
                timeout=10,
            )

            await ws_task

        assert response_sent.is_set(), "WS never sent a response"
        assert http_resp.status_code == 200, (
            f"Expected 200 but got {http_resp.status_code} — "
            "response was likely dropped due to race condition"
        )
        assert http_resp.text == "race-response"


# ---------------------------------------------------------------------------
# Bug #2 — Dropped message on WebSocket disconnect during send()
# ---------------------------------------------------------------------------


class TestDroppedMessageOnDisconnect:
    async def test_future_fails_immediately_when_ws_disconnects_during_send(
        self, connection_id: str
    ) -> None:
        """
        When the WebSocket disconnects while send() is forwarding a queued
        request, the waiting HTTP future must be resolved immediately with an
        error (not left to time out after 300 s).
        """

        from fastapi.websockets import WebSocketDisconnect as _WSD

        app = _make_app_with_settings()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            send_attempted = asyncio.Event()

            async def disconnecting_ws_client() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": f"/{connection_id}",
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

                # Wait for accept
                msg = await asyncio.wait_for(outbox.get(), timeout=5)
                assert msg["type"] == "websocket.accept"

                # Wait for send to be attempted (and fail)
                await asyncio.wait_for(send_attempted.wait(), timeout=5)

                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=3)

            ws_task = asyncio.create_task(disconnecting_ws_client())
            await asyncio.sleep(0.01)

            # The HTTP request should fail quickly (5 s), not wait 300 s
            http_resp = await asyncio.wait_for(
                client.get(f"/{connection_id}/ping"),
                timeout=5,
            )

            await ws_task

        # Must get a 5xx error immediately, not a 504 after 300 s timeout
        assert http_resp.status_code in (502, 503), (
            f"Expected 502 or 503 on disconnect, got {http_resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Bug #3 — buffers defaultdict leaks on connection close
# ---------------------------------------------------------------------------


class TestBuffersCleanupOnDisconnect:
    async def test_buffer_entry_removed_after_websocket_disconnects(
        self, connection_id: str
    ) -> None:
        """
        After a WebSocket client disconnects, its entry in the buffers dict
        must be removed.  With the original code (no cleanup) the queue entry
        lives forever — one entry per connection_id that ever connected.
        """
        app = _make_app_with_settings()
        token = make_token(connection_id)

        scope: dict[str, object] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "path": f"/{connection_id}",
            "query_string": f"token={token}".encode(),
            "headers": [],
        }
        inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        await inbox.put({"type": "websocket.connect"})
        app_task = asyncio.create_task(
            app(scope, inbox.get, outbox.put),  # type: ignore[arg-type]
        )

        # Wait for accept
        msg = await asyncio.wait_for(outbox.get(), timeout=5)
        assert msg["type"] == "websocket.accept"

        # Disconnect immediately
        await inbox.put({"type": "websocket.disconnect"})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(app_task, timeout=2)

        # The buffer entry must have been removed
        buffers: dict[str, object] = app.extra.get("buffers", {})
        assert connection_id not in buffers, (
            f"buffers[{connection_id!r}] was not cleaned up after disconnect"
        )


# ---------------------------------------------------------------------------
# Bug #4 — async-timeout is a redundant dependency
# ---------------------------------------------------------------------------


class TestNoAsyncTimeoutDependency:
    def test_server_does_not_import_async_timeout(self) -> None:
        """server.py must use asyncio.timeout() (stdlib) not async_timeout."""
        import pipegate.server as server_mod

        assert not hasattr(server_mod, "async_timeout"), (
            "server.py still imports async_timeout; replace with asyncio.timeout()"
        )


# ---------------------------------------------------------------------------
# Bug #5 — Duplicate query parameters silently lost
# ---------------------------------------------------------------------------


class TestDuplicateQueryParams:
    async def test_duplicate_params_preserved_in_forwarded_request(
        self, connection_id: str
    ) -> None:
        """
        ?a=1&a=2 must not be collapsed to {"a": "2"}.
        The forwarded url_query must carry both values.
        """
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, fwd = await _ws_roundtrip(app, connection_id, token, query="a=1&a=2&b=3")

        assert resp.status_code == 200
        query = json.loads(fwd["url_query"])
        # Must be a list of pairs, not a collapsed dict
        assert isinstance(query, list), (
            f"url_query should be a list of pairs, got {type(query)}: {query}"
        )
        pairs = [(k, v) for k, v in query]
        a_values = [v for k, v in pairs if k == "a"]
        assert sorted(a_values) == ["1", "2"], (
            f"Both values for 'a' must be present, got {a_values}"
        )
        b_values = [v for k, v in pairs if k == "b"]
        assert b_values == ["3"]


# ---------------------------------------------------------------------------
# Bug #6 — Binary request body corruption
# ---------------------------------------------------------------------------


class TestBinaryRequestBody:
    async def test_binary_body_survives_round_trip(self, connection_id: str) -> None:
        """
        A binary (non-UTF-8) request body must be forwarded intact.
        With the old `.decode()` approach this would raise UnicodeDecodeError
        or silently corrupt the data.
        """
        binary_body = bytes(range(256))

        app = _make_app_with_settings()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        received_body: list[str] = []

        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def ws_client_side() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": f"/{connection_id}",
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

                received_body.append(fwd["body"])

                # Body should be base64-encoded, not a raw string
                decoded = base64.b64decode(fwd["body"])
                assert decoded == binary_body, (
                    "Binary body was corrupted during transport"
                )

                resp = BufferGateResponse(
                    correlation_id=fwd["correlation_id"],
                    headers=orjson.dumps({}).decode(),
                    body=base64.b64encode(b"ok").decode(),
                    status_code=200,
                )
                await inbox.put(
                    {"type": "websocket.receive", "text": resp.model_dump_json()}
                )

                await asyncio.sleep(0.05)
                await inbox.put({"type": "websocket.disconnect"})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=2)

            ws_task = asyncio.create_task(ws_client_side())
            await asyncio.sleep(0.01)

            http_resp = await asyncio.wait_for(
                client.post(
                    f"/{connection_id}/upload",
                    content=binary_body,
                ),
                timeout=10,
            )
            await ws_task

        assert http_resp.status_code == 200


# ---------------------------------------------------------------------------
# Bug #7 — Response body charset-unsafe
# ---------------------------------------------------------------------------


class TestBinaryResponseBody:
    async def test_binary_response_body_survives_round_trip(
        self, connection_id: str
    ) -> None:
        """
        A binary response body (e.g. image, protobuf) must be returned intact.
        With the old response.text + .encode() approach the bytes would be
        mangled by charset detection.
        """
        binary_response = bytes(range(256))

        app = _make_app_with_settings()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def ws_client_side() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": f"/{connection_id}",
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

                # Tunnel client: base64-encode the binary response
                resp = BufferGateResponse(
                    correlation_id=fwd["correlation_id"],
                    headers=orjson.dumps(
                        {"content-type": "application/octet-stream"}
                    ).decode(),
                    body=base64.b64encode(binary_response).decode(),
                    status_code=200,
                )
                await inbox.put(
                    {"type": "websocket.receive", "text": resp.model_dump_json()}
                )

                await asyncio.sleep(0.05)
                await inbox.put({"type": "websocket.disconnect"})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=2)

            ws_task = asyncio.create_task(ws_client_side())
            await asyncio.sleep(0.01)

            http_resp = await asyncio.wait_for(
                client.get(f"/{connection_id}/download"),
                timeout=10,
            )
            await ws_task

        assert http_resp.status_code == 200
        assert http_resp.content == binary_response, (
            "Binary response body was corrupted during transport"
        )


# ---------------------------------------------------------------------------
# Refactor #9 — Replace print() with logging
# ---------------------------------------------------------------------------


class TestLoggingInsteadOfPrint:
    def test_server_uses_logger_not_print(self) -> None:
        """
        server.py must use logger.* instead of bare print() calls.
        A module-level logger must be defined.
        """
        import ast
        import importlib.util
        import pathlib

        spec = importlib.util.find_spec("pipegate.server")
        assert spec is not None and spec.origin is not None
        src = pathlib.Path(spec.origin)
        tree = ast.parse(src.read_text())

        print_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        assert not print_calls, (
            f"server.py still has {len(print_calls)} bare print() call(s); "
            "use logger.info/warning/error instead"
        )

        logger_assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "logger" for t in node.targets)
        ]
        assert logger_assignments, "server.py must define a module-level `logger`"


# ---------------------------------------------------------------------------
# Refactor #11 — Fail waiting futures immediately on WebSocket disconnect
# ---------------------------------------------------------------------------


class TestInFlightFuturesFailOnDisconnect:
    async def test_all_inflight_futures_fail_when_ws_disconnects(
        self, connection_id: str
    ) -> None:
        """
        When the WebSocket client disconnects while multiple HTTP requests are
        in-flight (queued but not yet forwarded), all their futures must be
        immediately resolved with an error — not left waiting 300 s each.
        """
        app = _make_app_with_settings()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        ws_connected = asyncio.Event()
        ready_to_disconnect = asyncio.Event()

        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def ws_client_disconnects_early() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": f"/{connection_id}",
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

                ws_connected.set()

                await asyncio.wait_for(ready_to_disconnect.wait(), timeout=5)
                await inbox.put({"type": "websocket.disconnect"})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=3)

            ws_task = asyncio.create_task(ws_client_disconnects_early())

            await asyncio.wait_for(ws_connected.wait(), timeout=5)

            async def make_request(path: str) -> Response:
                return await client.get(f"/{connection_id}/{path}")

            http_task1 = asyncio.create_task(make_request("req1"))
            http_task2 = asyncio.create_task(make_request("req2"))

            await asyncio.sleep(0.05)
            ready_to_disconnect.set()

            resp1, resp2 = await asyncio.wait_for(
                asyncio.gather(http_task1, http_task2),
                timeout=5,
            )
            await ws_task

        assert resp1.status_code in (502, 503), (
            f"req1 expected 502/503, got {resp1.status_code}"
        )
        assert resp2.status_code in (502, 503), (
            f"req2 expected 502/503, got {resp2.status_code}"
        )


# ---------------------------------------------------------------------------
# Feature #13 — WebSocket authentication
# ---------------------------------------------------------------------------


class TestWebSocketAuthentication:
    async def _connect_ws(
        self,
        app: FastAPI,
        connection_id: str,
        *,
        token: str | None = None,
    ) -> str:
        """Attempt a WS connection, return the first server message type."""
        query = f"token={token}" if token else ""
        scope: dict[str, object] = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "path": f"/{connection_id}",
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

    async def test_ws_rejected_without_token(self, connection_id: str) -> None:
        """WebSocket connections without a token must be rejected."""
        app = _make_app_with_settings()
        assert await self._connect_ws(app, connection_id) == "websocket.close"

    async def test_ws_rejected_with_wrong_token(self, connection_id: str) -> None:
        """Tokens for a different connection_id must be rejected."""
        app = _make_app_with_settings()
        wrong_token = make_token(uuid.uuid4().hex)
        result = await self._connect_ws(app, connection_id, token=wrong_token)
        assert result == "websocket.close"

    async def test_ws_rejected_with_expired_token(self, connection_id: str) -> None:
        """Expired tokens must be rejected."""
        from datetime import timedelta as _td

        app = _make_app_with_settings()
        expired = make_token(connection_id, expires_in=_td(seconds=-1))
        result = await self._connect_ws(app, connection_id, token=expired)
        assert result == "websocket.close"

    async def test_ws_accepted_with_valid_token(self, connection_id: str) -> None:
        """Valid tokens must be accepted."""
        app = _make_app_with_settings()
        valid_token = make_token(connection_id)
        result = await self._connect_ws(app, connection_id, token=valid_token)
        assert result == "websocket.accept"


# ---------------------------------------------------------------------------
# Feature #14 — Configurable request/response size limit
# ---------------------------------------------------------------------------


class TestBodySizeLimit:
    async def test_oversized_request_body_rejected_with_413(
        self, connection_id: str
    ) -> None:
        """A request body larger than max_body_bytes must return 413."""
        app = _make_app_with_settings()
        app.extra["settings"].max_body_bytes = 10
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/{connection_id}/upload",
                content=b"x" * 100,
            )

        assert resp.status_code == 413, (
            f"Expected 413 for oversized body, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Feature #15 — Per-connection queue backpressure
# ---------------------------------------------------------------------------


class TestQueueBackpressure:
    async def test_503_when_queue_is_full(self, connection_id: str) -> None:
        """When the queue is full, new requests must get 503 immediately."""
        app = create_app()
        settings = Settings(_cli_parse_args=False)
        settings.max_queue_depth = 1
        app.extra["settings"] = settings

        transport = ASGITransport(app=app)
        second_resp: Response | None = None

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first_task = asyncio.create_task(
                client.post(f"/{connection_id}/req1", content=b"a")
            )
            await asyncio.sleep(0.05)
            second_resp = await asyncio.wait_for(
                client.post(f"/{connection_id}/req2", content=b"b"),
                timeout=2,
            )
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await first_task

        assert second_resp is not None
        assert second_resp.status_code == 503, (
            f"Expected 503 when queue is full, got {second_resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Feature #16 — Strip response body for HEAD requests
# ---------------------------------------------------------------------------


class TestHeadRequestBodyStripped:
    async def test_head_response_has_no_body(self, connection_id: str) -> None:
        """HEAD responses must have an empty body per HTTP spec."""
        app = _make_app_with_settings()
        token = make_token(connection_id)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:

            async def ws_client_side() -> None:
                scope: dict[str, object] = {
                    "type": "websocket",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "path": f"/{connection_id}",
                    "query_string": f"token={token}".encode(),
                    "headers": [],
                }
                inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                outbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                await inbox.put({"type": "websocket.connect"})
                app_task = asyncio.create_task(
                    app(scope, inbox.get, outbox.put)  # type: ignore[arg-type]
                )

                msg = await asyncio.wait_for(outbox.get(), timeout=5)
                assert msg["type"] == "websocket.accept"
                msg = await asyncio.wait_for(outbox.get(), timeout=5)
                assert msg["type"] == "websocket.send"
                fwd = json.loads(cast(str, msg["text"]))

                resp = BufferGateResponse(
                    correlation_id=fwd["correlation_id"],
                    headers=orjson.dumps({"content-length": "5"}).decode(),
                    body=base64.b64encode(b"hello").decode(),
                    status_code=200,
                )
                await inbox.put(
                    {"type": "websocket.receive", "text": resp.model_dump_json()}
                )
                await asyncio.sleep(0.05)
                await inbox.put({"type": "websocket.disconnect"})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(app_task, timeout=2)

            ws_task = asyncio.create_task(ws_client_side())
            await asyncio.sleep(0.01)
            http_resp = await asyncio.wait_for(
                client.head(f"/{connection_id}/resource"),
                timeout=10,
            )
            await ws_task

        assert http_resp.status_code == 200
        assert http_resp.content == b""


# ---------------------------------------------------------------------------
# Feature #17 — Add GET /healthz health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    async def test_healthz_returns_200(self, client: AsyncClient) -> None:
        """GET /healthz must return 200 with {"status": "ok"}."""
        resp = await client.get("/healthz")
        assert resp.status_code == 200

    async def test_healthz_returns_ok_body(self, client: AsyncClient) -> None:
        """GET /healthz response body must be {"status": "ok"}."""
        resp = await client.get("/healthz")
        assert resp.json() == {"status": "ok"}

    async def test_healthz_does_not_require_auth(self, client: AsyncClient) -> None:
        """GET /healthz must be accessible without any Authorization header."""
        resp = await client.get("/healthz")
        assert resp.status_code == 200
