from __future__ import annotations

import asyncio
import base64
import functools
import sys
import uuid

import httpx
import orjson
from websockets.asyncio.client import ClientConnection, connect

from .schemas import BufferGateChunk, BufferGateRequest, BufferGateResponse

_BACKOFF_BASE: float = 1.0
_BACKOFF_MAX: float = 60.0
# No read timeout: a server-sent event stream is idle between events for as
# long as it likes. Until the head is sent a request has as long as the
# server waits for it; after that the server cancels a stream whose caller
# went away.
_TIMEOUT = httpx.Timeout(5.0, read=None)
_HEAD_TIMEOUT_S = 300.0
# Not passed on with a streamed body: it is re-framed by the server.
_STREAM_DROP_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
}


def _is_stream(response: httpx.Response) -> bool:
    content_type: str = response.headers.get("content-type", "")
    return content_type.startswith("text/event-stream")


async def handle_request(
    target: str,
    request: BufferGateRequest,
    http_client: httpx.AsyncClient,
    ws_client: ClientConnection,
) -> None:
    streaming = False
    try:
        async with (
            asyncio.timeout(_HEAD_TIMEOUT_S) as deadline,
            http_client.stream(
                method=request.method,
                url=f"{target}/{request.url_path}",
                headers=orjson.loads(request.headers),
                params=orjson.loads(request.url_query),
                content=base64.b64decode(request.body) if request.body else b"",
                timeout=_TIMEOUT,
            ) as response,
        ):
            if not _is_stream(response):
                body = await response.aread()
                payload = BufferGateResponse(
                    correlation_id=request.correlation_id,
                    headers=orjson.dumps(dict(response.headers)).decode(),
                    body=base64.b64encode(body).decode(),
                    status_code=response.status_code,
                )
                await ws_client.send(payload.model_dump_json())
                return

            # Server-sent events: the head now, then each piece as it comes.
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in _STREAM_DROP_HEADERS
            }
            await ws_client.send(
                BufferGateResponse(
                    correlation_id=request.correlation_id,
                    headers=orjson.dumps(headers).decode(),
                    body="",
                    status_code=response.status_code,
                    stream=True,
                ).model_dump_json()
            )
            streaming = True
            deadline.reschedule(None)
            async for piece in response.aiter_bytes():
                await ws_client.send(
                    BufferGateChunk(
                        correlation_id=request.correlation_id,
                        body=base64.b64encode(piece).decode(),
                    ).model_dump_json()
                )
            await ws_client.send(
                BufferGateChunk(
                    correlation_id=request.correlation_id, done=True
                ).model_dump_json()
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(
            f"Error processing request {request.correlation_id}: {e}",
            file=sys.stderr,
        )
        reply: BufferGateResponse | BufferGateChunk = (
            BufferGateChunk(correlation_id=request.correlation_id, done=True)
            if streaming
            else BufferGateResponse(
                correlation_id=request.correlation_id,
                headers="{}",
                body="",
                status_code=504,
            )
        )
        await ws_client.send(reply.model_dump_json())


async def main(target_url: str, server_url: str) -> None:
    attempt = 0

    while True:
        delay = min(_BACKOFF_BASE * (2**attempt), _BACKOFF_MAX)

        if attempt > 0:
            print(
                f"Reconnecting in {delay:.0f}s (attempt {attempt + 1})...",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)

        print(f"Connecting to {server_url}...")

        try:
            async with (
                connect(server_url) as ws_client,
                httpx.AsyncClient() as http_client,
            ):
                print("Connected.")
                attempt = 0
                # Requests in flight, so the server can cancel a stream.
                tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
                async with asyncio.TaskGroup() as tg:
                    while True:
                        try:
                            message = await ws_client.recv()
                            data = orjson.loads(message)
                            if data.get("kind") == "cancel":
                                cid = uuid.UUID(data["correlation_id"])
                                if cancelled := tasks.pop(cid, None):
                                    cancelled.cancel()
                                continue
                            request = BufferGateRequest.model_validate(data)
                            task = tg.create_task(
                                handle_request(
                                    target_url, request, http_client, ws_client
                                )
                            )
                            tasks[request.correlation_id] = task
                            # Called with the task: pop(cid, task) forgets it.
                            task.add_done_callback(
                                functools.partial(tasks.pop, request.correlation_id)
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            print(f"Error receiving message: {e}", file=sys.stderr)
        except asyncio.CancelledError:
            raise
        except (ConnectionRefusedError, OSError) as e:
            print(f"Connection failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)

        attempt += 1
