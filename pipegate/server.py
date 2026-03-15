from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast, get_args

import orjson
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError

from .auth import verify_token
from .schemas import (
    BufferGateRequest,
    BufferGateResponse,
    JWTPayload,
    Methods,
    Settings,
)

logger = logging.getLogger(__name__)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.extra["settings"]
    return settings


def verify_jwt_uuid_match(
    connection_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> JWTPayload:
    authorization: str | None = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
        )

    token = authorization.split(" ", 1)[1]

    try:
        return verify_token(token, connection_id, settings)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


def create_app() -> FastAPI:
    buffers: dict[str, asyncio.Queue[BufferGateRequest]] = {}  # bounded queues via #15
    futures: dict[uuid.UUID, asyncio.Future[BufferGateResponse]] = {}
    # Maps connection_id → set of in-flight correlation_ids for that connection.
    # Used to fail all waiting futures immediately on WebSocket disconnect.  (#11)
    connection_futures: dict[str, set[uuid.UUID]] = collections.defaultdict(set)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.extra["settings"] = Settings(_cli_parse_args=False)
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
    # Also store outside of lifespan so tests that skip lifespan can access it
    app.extra["buffers"] = buffers

    @app.api_route(
        "/{connection_id}/{path_slug:path}",
        methods=list(get_args(Methods)),
    )
    async def handle_http_request(
        connection_id: str,
        request: Request,
        path_slug: str = "",
        payload: JWTPayload = Depends(verify_jwt_uuid_match),  # noqa: B008
    ) -> Response:
        correlation_id = uuid.uuid4()

        # Enforce body size limit before doing any async work.  (#14)
        settings: Settings = request.app.extra["settings"]
        raw_body = await request.body()
        if len(raw_body) > settings.max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Request body exceeds limit of {settings.max_body_bytes} bytes",
            )

        # Create and store the future BEFORE enqueuing so that the WebSocket
        # receive() handler can always find it via futures.get(), even when
        # the tunnel client responds before this coroutine resumes.  (#1)
        loop = asyncio.get_event_loop()
        future: asyncio.Future[BufferGateResponse] = loop.create_future()
        futures[correlation_id] = future
        connection_futures[connection_id].add(correlation_id)  # #11

        # Get or create a bounded queue for this connection.  (#15)
        if connection_id not in buffers:
            buffers[connection_id] = asyncio.Queue(maxsize=settings.max_queue_depth)

        try:
            buffers[connection_id].put_nowait(  # #15: raises QueueFull if at capacity
                BufferGateRequest(
                    correlation_id=correlation_id,
                    method=cast(Methods, request.method),
                    url_path=path_slug,
                    url_query=orjson.dumps(
                        list(request.query_params.multi_items())  # #5: preserve dupes
                    ).decode(),
                    headers=orjson.dumps(
                        {
                            **dict(request.headers),
                            "x-pipegate-correlation-id": correlation_id.hex,
                        }
                    ).decode(),
                    body=base64.b64encode(raw_body).decode(),  # #6
                )
            )
        except asyncio.QueueFull:
            futures.pop(correlation_id, None)
            connection_futures[connection_id].discard(correlation_id)
            raise HTTPException(
                status_code=503,
                detail="Queue full — tunnel client is too slow or not connected",
            )
        except Exception as e:
            futures.pop(correlation_id, None)
            connection_futures[connection_id].discard(correlation_id)
            raise HTTPException(
                status_code=500, detail=f"Failed to enqueue request: {e}"
            ) from e

        timeout = timedelta(seconds=300)

        try:
            async with asyncio.timeout(timeout.total_seconds()):  # #4
                response = await future
        except TimeoutError as e:
            raise HTTPException(status_code=504, detail="Gateway Timeout") from e
        finally:
            futures.pop(correlation_id, None)
            connection_futures[connection_id].discard(correlation_id)

        return Response(
            content=base64.b64decode(response.body) if response.body else b"",  # #7
            headers=orjson.loads(response.headers) if response.headers else {},
            status_code=response.status_code,
        )

    @app.websocket("/{connection_id}")
    async def handle_websocket(
        connection_id: str,
        websocket: WebSocket,
    ) -> None:
        # Authenticate before accepting — WS clients cannot send custom headers,
        # so the JWT is passed as the `token` query parameter.  (#13)
        settings: Settings = websocket.app.extra["settings"]
        token: str | None = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=1008, reason="Missing token")
            return
        try:
            verify_token(token, connection_id, settings)
        except Exception as exc:
            logger.warning("WebSocket auth failed for %s: %s", connection_id, exc)
            await websocket.close(code=1008, reason="Invalid token")
            return

        await websocket.accept()
        logger.info("WebSocket connection established for ID: %s", connection_id)

        # Ensure the queue exists with the correct maxsize for this connection.  (#15)
        if connection_id not in buffers:
            buffers[connection_id] = asyncio.Queue(maxsize=settings.max_queue_depth)

        async def receive() -> None:
            try:
                while True:
                    message_text = await websocket.receive_text()
                    try:
                        message = BufferGateResponse.model_validate_json(message_text)
                        future = futures.get(message.correlation_id)
                        if future and not future.done():
                            future.set_result(message)
                        else:
                            cid = message.correlation_id
                            logger.warning("No pending future for: %s", cid)
                    except ValidationError as ve:
                        logger.warning("Invalid message format: %s", ve)
                    except Exception as e:
                        logger.error("Error processing received message: %s", e)
            except WebSocketDisconnect as e:
                logger.info("WebSocket disconnected during receive: %s", e)
            except Exception as e:
                logger.error("Unexpected error in receive handler: %s", e)

        async def send() -> None:
            try:
                while True:
                    request = await buffers[connection_id].get()
                    try:
                        await websocket.send_text(request.model_dump_json())
                    except WebSocketDisconnect as e:
                        logger.info("WebSocket disconnected during send: %s", e)
                        # The item was already dequeued; fail its future
                        # immediately so the HTTP caller gets a 502 rather
                        # than waiting for the 300 s timeout.  (#2)
                        fut = futures.get(request.correlation_id)
                        if fut and not fut.done():
                            fut.set_exception(
                                HTTPException(
                                    status_code=502,
                                    detail="Tunnel client disconnected",
                                )
                            )
                        break
                    except Exception as e:
                        logger.error("Error sending message: %s", e)
            except Exception as e:
                logger.error("Unexpected error in send handler: %s", e)

        receive_task = asyncio.create_task(receive())
        send_task = asyncio.create_task(send())

        # When either side finishes (disconnect), cancel the other so we
        # don't leak the send() task blocked on buffers[connection_id].get().  (#3)
        _done, pending = await asyncio.wait(
            {receive_task, send_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        logger.info("WebSocket connection closed for ID: %s", connection_id)

        # Fail all in-flight futures for this connection immediately so HTTP
        # callers get a 503 instead of waiting until the 300 s timeout expires.  (#11)
        for cid in list(connection_futures.pop(connection_id, set())):
            fut = futures.get(cid)
            if fut and not fut.done():
                fut.set_exception(
                    HTTPException(
                        status_code=503,
                        detail="Tunnel client disconnected",
                    )
                )

        buffers.pop(connection_id, None)  # #3: clean up queue entry

    return app


if __name__ == "__main__":
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
