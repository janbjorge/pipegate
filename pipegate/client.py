from __future__ import annotations

import asyncio

import httpx
import orjson
import typer
from typing_extensions import Annotated
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedError

from .schemas import BufferGateRequest, BufferGateResponse

app = typer.Typer()


@app.command()
def start_client(
    local_url: Annotated[str, typer.Option(help="URL of the local server to forward requests to")],
    server_url:  Annotated[str, typer.Option(help="URL of the PipeGate server to forward requests through")],
    client_token: Annotated[str, typer.Option(help="Token used to authenticate with the PipeGate server")]
):
    asyncio.run(main(local_url, server_url, client_token))


async def handle_request(
    local_url: str,
    request: BufferGateRequest,
    http_client: httpx.AsyncClient,
    ws_client: ClientConnection,
) -> None:
    """
    Process an incoming request from the server, forward it to the local server,
    and send back the response via WebSocket.

    Args:
        local_url (str): The target URL for the local HTTP server.
        request (BufferGateRequest): The incoming request data.
        http_client (httpx.AsyncClient): The HTTP client for making requests.
        ws_client (ClientConnection): The WebSocket client for sending responses.
    """
    try:
        response = await http_client.request(
            method=request.method,
            url=f"{local_url}/{request.url_path}",
            headers=orjson.loads(request.headers),
            params=orjson.loads(request.url_query),
            content=request.body.encode(),
        )
        response_payload = BufferGateResponse(
            correlation_id=request.correlation_id,
            headers=orjson.dumps(dict(response.headers)).decode(),
            body=response.text,
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


async def main(local_url: str, server_url: str, client_token: str) -> None:
    """
    Establish a WebSocket connection to the PipeGate server and handle incoming requests.

    Args:
        port (int): The port number of the local HTTP server to expose.
        server_url (str): The WebSocket server URL to connect to.
    """
    typer.secho(
        f"Connecting to server at {server_url}...",
        fg=typer.colors.BLUE,
    )

    try:
        async with connect(uri=server_url, additional_headers={'PIPEGATE_CLIENT_TOKEN': client_token}) as ws_client, httpx.AsyncClient() as http_client:
            typer.secho("Connected to server.", fg=typer.colors.GREEN)
            async with asyncio.TaskGroup() as task_group:
                while True:
                    try:
                        message = await ws_client.recv()
                        request = BufferGateRequest.model_validate_json(message)
                        task_group.create_task(
                            handle_request(
                                local_url,
                                request,
                                http_client,
                                ws_client,
                            )
                        )
                    except asyncio.CancelledError:
                        break
                    except ConnectionClosedError:
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
