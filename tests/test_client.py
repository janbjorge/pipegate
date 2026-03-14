from __future__ import annotations

import asyncio
import importlib
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import orjson
import pytest

from pipegate.client import handle_request, main
from pipegate.schemas import BufferGateRequest, BufferGateResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def correlation_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def sample_request(correlation_id: uuid.UUID) -> BufferGateRequest:
    return BufferGateRequest(
        correlation_id=correlation_id,
        url_path="api/data",
        url_query=orjson.dumps({"key": "value"}).decode(),
        method="GET",
        headers=orjson.dumps({"x-custom": "header"}).decode(),
        body="",
    )


@pytest.fixture
def ws_client() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# handle_request tests
# ---------------------------------------------------------------------------


class TestHandleRequest:
    async def test_forwards_request_to_target(
        self,
        sample_request: BufferGateRequest,
        ws_client: AsyncMock,
    ) -> None:
        """Successful request is forwarded and response sent back."""
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    text="ok",
                    headers={"x-resp": "val"},
                )
            )
        ) as http_client:
            await handle_request(
                "http://localhost:9000",
                sample_request,
                http_client,
                ws_client,
            )

        ws_client.send.assert_called_once()
        sent_json: str = ws_client.send.call_args[0][0]
        resp = BufferGateResponse.model_validate_json(sent_json)

        assert resp.correlation_id == sample_request.correlation_id
        assert resp.status_code == 200
        assert resp.body == "ok"
        resp_headers = orjson.loads(resp.headers)
        assert resp_headers["x-resp"] == "val"

    async def test_preserves_method_and_body(
        self,
        correlation_id: uuid.UUID,
        ws_client: AsyncMock,
    ) -> None:
        """POST body and method are forwarded correctly."""
        request = BufferGateRequest(
            correlation_id=correlation_id,
            url_path="submit",
            url_query=orjson.dumps({}).decode(),
            method="POST",
            headers=orjson.dumps({}).decode(),
            body="payload-data",
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
                "http://localhost:9000",
                request,
                http_client,
                ws_client,
            )

        assert captured["method"] == "POST"
        assert captured["content"] == "payload-data"

        sent_json: str = ws_client.send.call_args[0][0]
        resp = BufferGateResponse.model_validate_json(sent_json)
        assert resp.status_code == 201

    async def test_preserves_url_path(
        self,
        correlation_id: uuid.UUID,
        ws_client: AsyncMock,
    ) -> None:
        """Target URL is constructed from target + url_path."""
        request = BufferGateRequest(
            correlation_id=correlation_id,
            url_path="nested/path/here",
            url_query=orjson.dumps({}).decode(),
            method="GET",
            headers=orjson.dumps({}).decode(),
            body="",
        )

        captured_url: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured_url.append(str(req.url))
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000",
                request,
                http_client,
                ws_client,
            )

        assert "localhost:9000/nested/path/here" in captured_url[0]

    async def test_preserves_query_params(
        self,
        correlation_id: uuid.UUID,
        ws_client: AsyncMock,
    ) -> None:
        """Query parameters are forwarded to the target."""
        request = BufferGateRequest(
            correlation_id=correlation_id,
            url_path="search",
            url_query=orjson.dumps({"q": "test", "page": "1"}).decode(),
            method="GET",
            headers=orjson.dumps({}).decode(),
            body="",
        )

        captured_params: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured_params.update(dict(req.url.params))
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000",
                request,
                http_client,
                ws_client,
            )

        assert captured_params["q"] == "test"
        assert captured_params["page"] == "1"

    async def test_preserves_headers(
        self,
        correlation_id: uuid.UUID,
        ws_client: AsyncMock,
    ) -> None:
        """Custom headers are forwarded to the target."""
        request = BufferGateRequest(
            correlation_id=correlation_id,
            url_path="api",
            url_query=orjson.dumps({}).decode(),
            method="GET",
            headers=orjson.dumps(
                {"x-custom": "value", "accept": "application/json"}
            ).decode(),
            body="",
        )

        captured_headers: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(req.headers))
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000",
                request,
                http_client,
                ws_client,
            )

        assert captured_headers["x-custom"] == "value"
        assert captured_headers["accept"] == "application/json"

    async def test_http_error_returns_504(
        self,
        sample_request: BufferGateRequest,
        ws_client: AsyncMock,
    ) -> None:
        """When the target raises an exception, a 504 response is sent."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000",
                sample_request,
                http_client,
                ws_client,
            )

        ws_client.send.assert_called_once()
        sent_json: str = ws_client.send.call_args[0][0]
        resp = BufferGateResponse.model_validate_json(sent_json)

        assert resp.correlation_id == sample_request.correlation_id
        assert resp.status_code == 504
        assert resp.body == ""
        assert resp.headers == ""

    async def test_target_timeout_returns_504(
        self,
        sample_request: BufferGateRequest,
        ws_client: AsyncMock,
    ) -> None:
        """When the target times out, a 504 response is sent."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await handle_request(
                "http://localhost:9000",
                sample_request,
                http_client,
                ws_client,
            )

        sent_json: str = ws_client.send.call_args[0][0]
        resp = BufferGateResponse.model_validate_json(sent_json)
        assert resp.status_code == 504


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------


class TestMain:
    async def test_receives_and_processes_request(
        self,
        sample_request: BufferGateRequest,
    ) -> None:
        """main() connects via WS, receives a request, forwards it."""
        ws_mock = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                sample_request.model_dump_json(),
                asyncio.CancelledError(),
            ]
        )
        ws_mock.send = AsyncMock()

        connect_cm = AsyncMock()
        connect_cm.__aenter__ = AsyncMock(return_value=ws_mock)
        connect_cm.__aexit__ = AsyncMock(return_value=False)

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="hello"))
        )
        http_cm = AsyncMock()
        http_cm.__aenter__ = AsyncMock(return_value=http_client)
        http_cm.__aexit__ = AsyncMock(return_value=False)

        try:
            with (
                patch(
                    "pipegate.client.connect",
                    return_value=connect_cm,
                ),
                patch(
                    "pipegate.client.httpx.AsyncClient",
                    return_value=http_cm,
                ),
            ):
                await main(
                    "http://localhost:9000",
                    "ws://fake:8000/conn",
                )
        except BaseExceptionGroup:
            # TaskGroup may raise ExceptionGroup with CancelledError
            pass
        finally:
            await http_client.aclose()

        assert ws_mock.send.called

    async def test_connection_refused(self) -> None:
        """main() handles ConnectionRefusedError gracefully."""
        connect_cm = AsyncMock()
        connect_cm.__aenter__ = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        connect_cm.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "pipegate.client.connect",
            return_value=connect_cm,
        ):
            # Should not raise — error is caught internally
            await main(
                "http://localhost:9000",
                "ws://fake:8000/conn",
            )

    async def test_os_error(self) -> None:
        """main() handles OSError (e.g. DNS failure) gracefully."""
        connect_cm = AsyncMock()
        connect_cm.__aenter__ = AsyncMock(side_effect=OSError("network unreachable"))
        connect_cm.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "pipegate.client.connect",
            return_value=connect_cm,
        ):
            await main(
                "http://localhost:9000",
                "ws://fake:8000/conn",
            )


# ---------------------------------------------------------------------------
# CLI entry point test
# ---------------------------------------------------------------------------


class TestCLI:
    def test_typer_app_exists(self) -> None:
        """The Typer app is importable and has a command."""
        from pipegate.client import app

        assert app is not None
        assert len(app.registered_commands) > 0

    def test_module_runnable(self) -> None:
        """client module has __name__ == '__main__' guard."""
        source = importlib.util.find_spec("pipegate.client")
        assert source is not None
        assert source.origin is not None
        with open(source.origin) as f:
            content = f.read()
        assert 'if __name__ == "__main__":' in content
