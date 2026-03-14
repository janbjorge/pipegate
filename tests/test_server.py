from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import orjson
import pytest
from httpx import ASGITransport, AsyncClient

from pipegate.schemas import BufferGateResponse, Settings
from pipegate.server import create_app

from .conftest import make_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_with_settings() -> object:
    """Create an app and manually inject settings (lifespan doesn't run with raw ASGI)."""
    app = create_app()
    app.extra["settings"] = Settings(_cli_parse_args=False)
    return app


async def _ws_roundtrip(
    app,
    connection_id: str,
    token: str,
    *,
    method: str = "GET",
    path: str = "test-path",
    body: str = "",
    query: str = "",
    response_body: str = "tunnel-response",
    response_status: int = 200,
) -> tuple:
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

    forwarded_request = {}

    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def http_request():
            return await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                content=body,
            )

        async def ws_client():
            scope = {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "path": f"/{connection_id}",
                "query_string": b"",
                "headers": [],
            }

            inbox: asyncio.Queue = asyncio.Queue()
            outbox: asyncio.Queue = asyncio.Queue()

            await inbox.put({"type": "websocket.connect"})

            app_task = asyncio.create_task(app(scope, inbox.get, outbox.put))

            # Wait for accept
            msg = await asyncio.wait_for(outbox.get(), timeout=5)
            assert msg["type"] == "websocket.accept"

            # Wait for the forwarded request from the server
            msg = await asyncio.wait_for(outbox.get(), timeout=5)
            assert msg["type"] == "websocket.send"

            fwd = json.loads(msg["text"])
            forwarded_request.update(fwd)

            # Send tunnel response back
            response = BufferGateResponse(
                correlation_id=fwd["correlation_id"],
                headers=orjson.dumps({"x-tunnel": "ok"}).decode(),
                body=response_body,
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

            try:
                await asyncio.wait_for(app_task, timeout=2)
            except Exception:
                pass

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
# Auth tests (HTTP endpoint)
# ---------------------------------------------------------------------------


class TestHTTPAuth:
    async def test_no_auth_header(
        self, client: AsyncClient, connection_id: str
    ) -> None:
        resp = await client.get(f"/{connection_id}/ping")
        assert resp.status_code == 401

    async def test_bad_auth_scheme(
        self, client: AsyncClient, connection_id: str
    ) -> None:
        resp = await client.get(
            f"/{connection_id}/ping",
            headers={"Authorization": "Basic abc"},
        )
        assert resp.status_code == 401

    async def test_expired_token(self, client: AsyncClient, connection_id: str) -> None:
        token = make_token(connection_id, expires_in=timedelta(seconds=-1))
        resp = await client.get(
            f"/{connection_id}/ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    async def test_wrong_connection_id(self, client: AsyncClient) -> None:
        cid_a = uuid.uuid4().hex
        cid_b = uuid.uuid4().hex
        token = make_token(cid_a)
        resp = await client.get(
            f"/{cid_b}/ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_malformed_token(
        self, client: AsyncClient, connection_id: str
    ) -> None:
        resp = await client.get(
            f"/{connection_id}/ping",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401


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
        assert fwd["body"] == "hello"

    async def test_preserves_query_params(self, connection_id: str) -> None:
        app = _make_app_with_settings()
        token = make_token(connection_id)

        resp, fwd = await _ws_roundtrip(app, connection_id, token, query="a=1&b=2")

        assert resp.status_code == 200
        query = json.loads(fwd["url_query"])
        assert query == {"a": "1", "b": "2"}

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
