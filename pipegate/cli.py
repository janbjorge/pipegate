from __future__ import annotations

import asyncio

import typer
import uvicorn

from .auth import generate_token
from .client import main as run_client
from .schemas import Settings
from .server import create_app

app = typer.Typer(help="PipeGate — self-hosted HTTP tunnel.")


@app.command("token")
def token_cmd(
    connection_id: str | None = typer.Option(
        None, "--connection-id", "-c", help="Pin a connection ID (overrides env var)."
    ),
) -> None:
    """Generate a JWT bearer token for a tunnel connection."""
    settings = Settings()
    result = generate_token(settings, connection_id)
    typer.echo(f"Connection-id: {result.connection_id}")
    typer.echo(f"JWT Bearer:    {result.bearer}")


@app.command("client")
def client_cmd(
    target_url: str = typer.Argument(..., help="Local server to forward requests to."),
    server_url: str = typer.Argument(
        ..., help="PipeGate server WebSocket URL (include ?token=…)"
    ),
) -> None:
    """Start the tunnel client."""
    asyncio.run(run_client(target_url, server_url))


@app.command("server")
def server_cmd(
    host: str = typer.Option("0.0.0.0", "--host", help="Host to bind to."),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
) -> None:
    """Start the PipeGate server."""
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    app()
