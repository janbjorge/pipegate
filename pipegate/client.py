from __future__ import annotations

import asyncio
import base64

import httpx
import orjson
import typer
from websockets.asyncio.client import ClientConnection, connect

from .schemas import BufferGateRequest, BufferGateResponse

app = typer.Typer()


@app.command()
def start_client(target_url: str, server_url: str) -> None:
    """
    Start the PipeGate Client to expose a local server.

    Args:
        target_url (str): The server to route incoming traffic to.
        server_url (str): The WebSocket server URL to connect to.
    """
    asyncio.run(main(target_url, server_url))


async def handle_request(
    target: str,
    request: BufferGateRequest,
    http_client: httpx.AsyncClient,
    ws_client: ClientConnection,
) -> None:
    """
    Process an incoming request from the server, forward it to the local server,
    and send back the response via WebSocket.

    Args:
        target (str): The target URL for the local HTTP server.
        request (BufferGateRequest): The incoming request data.
        http_client (httpx.AsyncClient): The HTTP client for making requests.
        ws_client (ClientConnection): The WebSocket client for sending responses.
    """
    try:
        response = await http_client.request(
            method=request.method,
            url=f"{target}/{request.url_path}",
            headers=orjson.loads(request.headers),
            params=orjson.loads(request.url_query),  # list of pairs (#5)
            content=base64.b64decode(request.body) if request.body else b"",  # #6
        )
        response_payload = BufferGateResponse(
            correlation_id=request.correlation_id,
            headers=orjson.dumps(dict(response.headers)).decode(),
            body=base64.b64encode(response.content).decode(),  # #7
            status_code=response.status_code,
        )
    except Exception as e:
        typer.secho(
            f"Error processing request {request.correlation_id}: {e}",
            fg=typer.colors.RED,
        )
        response_payload = BufferGateResponse(
            correlation_id=request.correlation_id,
            headers="",
            body="",
            status_code=504,
        )

    await ws_client.send(response_payload.model_dump_json())


async def main(target_url: str, server_url: str) -> None:
    """
    Establish a WebSocket connection to the server and handle requests.

    Args:
        target_url (str): The local server to route incoming traffic to.
        server_url (str): The WebSocket server URL to connect to.
    """
    typer.secho(
        f"Connecting to server at {server_url}...",
        fg=typer.colors.BLUE,
    )

    try:
        async with connect(server_url) as ws_client, httpx.AsyncClient() as http_client:
            typer.secho("Connected to server.", fg=typer.colors.GREEN)
            async with asyncio.TaskGroup() as task_group:
                while True:
                    try:
                        message = await ws_client.recv()
                        request = BufferGateRequest.model_validate_json(message)
                        task_group.create_task(
                            handle_request(
                                target_url,
                                request,
                                http_client,
                                ws_client,
                            )
                        )
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        typer.secho(
                            f"Error receiving message: {e}",
                            fg=typer.colors.RED,
                        )
    except (ConnectionRefusedError, OSError) as e:
        typer.secho(
            f"Failed to connect to the server: {e}",
            fg=typer.colors.RED,
        )
    except Exception as e:
        typer.secho(
            f"An unexpected error occurred: {e}",
            fg=typer.colors.RED,
        )


if __name__ == "__main__":
    app()
