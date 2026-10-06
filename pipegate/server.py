from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast, get_args

import orjson
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from .auth import verify_token
from .schemas import (
    BufferGateCancel,
    BufferGateChunk,
    BufferGateRequest,
    BufferGateResponse,
    Methods,
    Settings,
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    buffers: dict[str, asyncio.Queue[BufferGateRequest | BufferGateCancel]] = {}
    futures: dict[uuid.UUID, asyncio.Future[BufferGateResponse]] = {}
    # Streamed bodies in flight: correlation id -> (connection id, pieces).
    # None in the queue ends the body.
    streams: dict[uuid.UUID, tuple[str, asyncio.Queue[bytes | None]]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.extra["settings"] = Settings()
        app.extra["buffers"] = buffers
        try:
            yield
        finally:
            for fut in futures.values():
                if not fut.done():
                    fut.set_exception(
                        HTTPException(status_code=504, detail="Gateway Timeout")
                    )

    app = FastAPI(lifespan=lifespan)
    app.extra["buffers"] = buffers

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route(
        "/{connection_id}/{path_slug:path}",
        methods=list(get_args(Methods)),
    )
    async def handle_http_request(
        connection_id: str,
        request: Request,
        path_slug: str = "",
    ) -> Response:
        settings: Settings = request.app.extra["settings"]
        correlation_id = uuid.uuid4()

        raw_body = await request.body()
        if len(raw_body) > settings.max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Request body exceeds limit of {settings.max_body_bytes} bytes",
            )

        future: asyncio.Future[BufferGateResponse] = asyncio.Future()
        futures[correlation_id] = future

        if connection_id not in buffers:
            buffers[connection_id] = asyncio.Queue(maxsize=settings.max_queue_depth)
        queue = buffers[connection_id]

        try:
            queue.put_nowait(
                BufferGateRequest(
                    correlation_id=correlation_id,
                    method=cast(Methods, request.method),
                    url_path=path_slug,
                    url_query=orjson.dumps(
                        list(request.query_params.multi_items())
                    ).decode(),
                    headers=orjson.dumps(
                        {
                            **dict(request.headers),
                            "x-pipegate-correlation-id": correlation_id.hex,
                        }
                    ).decode(),
                    body=base64.b64encode(raw_body).decode(),
                )
            )
        except asyncio.QueueFull:
            futures.pop(correlation_id, None)
            raise HTTPException(
                status_code=503,
                detail="Queue full — tunnel client is too slow or not connected",
            ) from None

        try:
            async with asyncio.timeout(300):
                response = await future
        except TimeoutError as e:
            raise HTTPException(status_code=504, detail="Gateway Timeout") from e
        finally:
            futures.pop(correlation_id, None)

        if response.stream:
            return stream_body(connection_id, correlation_id, response, request.method)

        response_content = (
            b""
            if request.method == "HEAD"
            else (base64.b64decode(response.body) if response.body else b"")
        )
        return Response(
            content=response_content,
            headers=orjson.loads(response.headers) if response.headers else {},
            status_code=response.status_code,
        )

    def stream_body(
        connection_id: str,
        correlation_id: uuid.UUID,
        head: BufferGateResponse,
        method: str,
    ) -> Response:
        """The body of a streamed response, piece by piece as the client sends
        it. If the caller goes away first, the client is told to stop."""
        headers = orjson.loads(head.headers) if head.headers else {}

        def cancel() -> None:
            if streams.pop(correlation_id, None) is None:
                return
            queue = buffers.get(connection_id)
            if queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(BufferGateCancel(correlation_id=correlation_id))

        if method == "HEAD":
            cancel()
            return Response(headers=headers, status_code=head.status_code)

        async def pieces() -> AsyncGenerator[bytes, None]:
            entry = streams.get(correlation_id)
            if entry is None:
                return
            try:
                while (piece := await entry[1].get()) is not None:
                    yield piece
                streams.pop(correlation_id, None)
            finally:
                cancel()

        return StreamingResponse(
            pieces(), status_code=head.status_code, headers=headers
        )

    @app.websocket("/")
    async def handle_websocket(
        websocket: WebSocket,
    ) -> None:
        settings: Settings = websocket.app.extra["settings"]
        token: str | None = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=1008, reason="Missing token")
            return
        try:
            payload = verify_token(token, settings)
        except Exception as exc:
            logger.warning("WebSocket auth failed: %s", exc)
            await websocket.close(code=1008, reason="Invalid token")
            return

        connection_id = payload.sub

        await websocket.accept()
        logger.info("WebSocket connected: %s", connection_id)

        if connection_id not in buffers:
            buffers[connection_id] = asyncio.Queue(maxsize=settings.max_queue_depth)
        queue = buffers[connection_id]

        async def receive() -> None:
            try:
                while True:
                    message_text = await websocket.receive_text()
                    try:
                        data = orjson.loads(message_text)
                        if isinstance(data, dict) and data.get("kind") == "chunk":
                            chunk = BufferGateChunk.model_validate(data)
                            entry = streams.get(chunk.correlation_id)
                            if entry is not None:
                                if chunk.body:
                                    entry[1].put_nowait(base64.b64decode(chunk.body))
                                if chunk.done:
                                    entry[1].put_nowait(None)
                            continue
                        message = BufferGateResponse.model_validate(data)
                        future = futures.get(message.correlation_id)
                        if future and not future.done():
                            # Ready before the caller resumes, so no piece
                            # that follows the head is lost.
                            if message.stream:
                                streams[message.correlation_id] = (
                                    connection_id,
                                    asyncio.Queue(),
                                )
                            future.set_result(message)
                        else:
                            logger.warning(
                                "No pending future for: %s", message.correlation_id
                            )
                    except (ValidationError, orjson.JSONDecodeError) as ve:
                        logger.warning("Invalid message format: %s", ve)
            except WebSocketDisconnect:
                pass

        async def send() -> None:
            while True:
                request = await queue.get()
                try:
                    await websocket.send_text(request.model_dump_json())
                except WebSocketDisconnect:
                    fut = futures.get(request.correlation_id)
                    if fut and not fut.done():
                        fut.set_exception(
                            HTTPException(
                                status_code=502,
                                detail="Tunnel client disconnected",
                            )
                        )
                    break

        receive_task = asyncio.create_task(receive())
        send_task = asyncio.create_task(send())

        _done, pending = await asyncio.wait(
            {receive_task, send_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        logger.info("WebSocket disconnected: %s", connection_id)
        buffers.pop(connection_id, None)
        # Streams through this tunnel end with it.
        for owner, pieces in list(streams.values()):
            if owner == connection_id:
                pieces.put_nowait(None)

    return app
