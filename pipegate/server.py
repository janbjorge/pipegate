from __future__ import annotations

import asyncio
import collections
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast, get_args

import async_timeout
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
    buffers: dict[str, asyncio.Queue[BufferGateRequest]] = collections.defaultdict(
        asyncio.Queue
    )
    futures: dict[uuid.UUID, asyncio.Future[BufferGateResponse]] = (
        collections.defaultdict(asyncio.Future)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.extra["settings"] = Settings(_cli_parse_args=False)

        try:
            yield
        finally:
            for fut in futures.values():
                if not fut.done():
                    fut.set_exception(
                        HTTPException(status_code=504, detail="Gateway Timeout")
                    )

    app = FastAPI(lifespan=lifespan)

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

        try:
            await buffers[connection_id].put(
                BufferGateRequest(
                    correlation_id=correlation_id,
                    method=cast(Methods, request.method),
                    url_path=path_slug,
                    url_query=orjson.dumps(
                        {k: v for k, v in request.query_params.multi_items()}
                    ).decode(),
                    headers=orjson.dumps(
                        {
                            **dict(request.headers),
                            "x-pipegate-correlation-id": correlation_id.hex,
                        }
                    ).decode(),
                    body=(await request.body()).decode(),
                )
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to enqueue request: {e}"
            ) from e

        timeout = timedelta(seconds=300)

        try:
            async with async_timeout.timeout(timeout.total_seconds()):
                response = await futures[correlation_id]
        except TimeoutError as e:
            raise HTTPException(status_code=504, detail="Gateway Timeout") from e
        finally:
            futures.pop(correlation_id, None)

        return Response(
            content=response.body.encode(),
            headers=orjson.loads(response.headers) if response.headers else {},
            status_code=response.status_code,
        )

    @app.websocket("/{connection_id}")
    async def handle_websocket(
        connection_id: str,
        websocket: WebSocket,
    ) -> None:
        await websocket.accept()
        print(f"WebSocket connection established for ID: {connection_id}")

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
                            print(f"No pending future for: {cid}")
                    except ValidationError as ve:
                        print(f"Invalid message format: {ve}")
                    except Exception as e:
                        print(f"Error processing received message: {e}")
            except WebSocketDisconnect as e:
                print(f"WebSocket disconnected during receive: {e}")
            except Exception as e:
                print(f"Unexpected error in receive handler: {e}")

        async def send() -> None:
            try:
                while True:
                    request = await buffers[connection_id].get()
                    try:
                        await websocket.send_text(request.model_dump_json())
                    except WebSocketDisconnect as e:
                        print(f"WebSocket disconnected during send: {e}")
                        break
                    except Exception as e:
                        print(f"Error sending message: {e}")
            except Exception as e:
                print(f"Unexpected error in send handler: {e}")

        await asyncio.gather(receive(), send())
        print(f"WebSocket connection closed for ID: {connection_id}")

    return app


if __name__ == "__main__":
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
