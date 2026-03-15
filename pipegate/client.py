from __future__ import annotations

import asyncio
import base64

import httpx
import orjson
import typer
from websockets.asyncio.client import ClientConnection, connect

from .schemas import BufferGateRequest, BufferGateResponse

app = typer.Typer()

_BACKOFF_BASE: float = 1.0
_BACKOFF_MAX: float = 60.0


@app.command()
def start_client(target_url: str, server_url: str) -> None:
    """Start the PipeGate client to expose a local server."""
    asyncio.run(main(target_url, server_url))


async def handle_request(
    target: str,
    request: BufferGateRequest,
    http_client: httpx.AsyncClient,
    ws_client: ClientConnection,
) -> None:
    try:
        response = await http_client.request(
            method=request.method,
            url=f"{target}/{request.url_path}",
            headers=orjson.loads(request.headers),
            params=orjson.loads(request.url_query),
            content=base64.b64decode(request.body) if request.body else b"",
        )
        payload = BufferGateResponse(
            correlation_id=request.correlation_id,
            headers=orjson.dumps(dict(response.headers)).decode(),
            body=base64.b64encode(response.content).decode(),
            status_code=response.status_code,
        )
    except Exception as e:
        typer.echo(f"Error processing request {request.correlation_id}: {e}", err=True)
        payload = BufferGateResponse(
            correlation_id=request.correlation_id,
            headers="",
            body="",
            status_code=504,
        )

    await ws_client.send(payload.model_dump_json())


async def main(target_url: str, server_url: str) -> None:
    attempt = 0

    while True:
        delay = min(_BACKOFF_BASE * (2**attempt), _BACKOFF_MAX)

        if attempt > 0:
            typer.echo(f"Reconnecting in {delay:.0f}s (attempt {attempt + 1})...")
            await asyncio.sleep(delay)

        typer.echo(f"Connecting to {server_url}...")

        try:
            async with (
                connect(server_url) as ws_client,
                httpx.AsyncClient() as http_client,
            ):
                typer.echo("Connected.")
                attempt = 0
                async with asyncio.TaskGroup() as tg:
                    while True:
                        try:
                            message = await ws_client.recv()
                            request = BufferGateRequest.model_validate_json(message)
                            tg.create_task(
                                handle_request(
                                    target_url, request, http_client, ws_client
                                )
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            typer.echo(f"Error receiving message: {e}", err=True)
        except asyncio.CancelledError:
            raise
        except (ConnectionRefusedError, OSError) as e:
            typer.echo(f"Connection failed: {e}", err=True)
        except Exception as e:
            typer.echo(f"Unexpected error: {e}", err=True)

        attempt += 1


if __name__ == "__main__":
    app()
