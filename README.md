# PipeGate

A lightweight, self-hosted tunneling proxy built with FastAPI. Expose local servers to the internet — a poor man's ngrok.

## How It Works

PipeGate has two sides: a **server** you deploy on public infrastructure, and a **client** that runs on your local machine.

1. The server accepts incoming HTTP requests at `/{connection_id}/{path}`.
2. A WebSocket client connects to `/{connection_id}` and receives forwarded requests.
3. The client forwards each request to your local service, then sends the response back over the WebSocket.
4. The server returns that response to the original HTTP caller.

Requests are matched via a `x-pipegate-correlation-id` header injected by the server.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Installation

```bash
git clone https://github.com/janbjorge/pipegate.git
cd pipegate
uv sync
```

Or install directly:

```bash
uv pip install git+https://github.com/janbjorge/pipegate.git
# or
pip install git+https://github.com/janbjorge/pipegate.git
```

## Configuration

PipeGate is configured entirely through environment variables (via pydantic-settings):

| Variable | Required | Description |
|---|---|---|
| `PIPEGATE_JWT_SECRET` | **Yes** | Secret key for signing/verifying JWT tokens |
| `PIPEGATE_JWT_ALGORITHMS` | **Yes** | JSON array of algorithms, e.g. `'["HS256"]'` |
| `PIPEGATE_CONNECTION_ID` | No | Custom connection ID for token generation (default: random hex UUID) |

Set these before running any command:

```bash
export PIPEGATE_JWT_SECRET="change-me-to-something-secret"
export PIPEGATE_JWT_ALGORITHMS='["HS256"]'
```

## Usage

### 1. Generate a JWT Token

```bash
python -m pipegate.auth
```

Output:

```
Connection-id: a1b2c3d4e5f6...
JWT Bearer:    eyJhbGciOiJIUzI1NiIs...
```

Tokens expire after **21 days**.

To use a custom connection ID:

```bash
PIPEGATE_CONNECTION_ID=my-api python -m pipegate.auth
```

Connection IDs can be any string — they don't have to be UUIDs.

### 2. Start the Server

```bash
python -m pipegate.server
```

Binds to `0.0.0.0:8000` by default. To change the host/port, edit the `uvicorn.run()` call in `server.py`.

### 3. Start the Client

```bash
python -m pipegate.client http://localhost:3000 ws://yourserver:8000/{connection_id}
```

Arguments:

- `target_url` — the local service to forward requests to (e.g. `http://localhost:3000`)
- `server_url` — the PipeGate server WebSocket endpoint (e.g. `ws://yourserver:8000/a1b2c3d4`)

The client connects to the server via WebSocket, receives forwarded requests, proxies them to your local service, and sends responses back through the tunnel.

### Endpoints

The server exposes two endpoints:

- **`GET/POST/PUT/DELETE/PATCH/OPTIONS/HEAD /{connection_id}/{path}`** — authenticated HTTP endpoint. Requires `Authorization: Bearer <jwt>` header.
- **`WS /{connection_id}`** — WebSocket endpoint for tunnel clients.

### Example Flow

```
External caller                PipeGate server              PipeGate client         Local service
      |                              |                            |                       |
      |-- POST /my-api/webhook -->   |                            |                       |
      |                              |-- WS: BufferGateRequest -> |                       |
      |                              |                            |-- forward request -->  |
      |                              |                            |<-- response ---------- |
      |                              |<- WS: BufferGateResponse - |                       |
      |<-- response -------------    |                            |                       |
```

Requests time out after **300 seconds** (5 minutes).

## Security Considerations

PipeGate has minimal built-in security. Be aware of the following:

- **HTTP endpoints require JWT auth** — requests without a valid `Authorization: Bearer` header are rejected (401/403).
- **The WebSocket endpoint has no authentication** — anyone who knows a connection ID can connect. Protect it at the network level (firewall, VPN, reverse proxy).
- Use **HTTPS/WSS** in production to encrypt traffic.
- Use a strong, random `PIPEGATE_JWT_SECRET`.
- Tokens are valid for 21 days — rotate them periodically.
- Consider rate limiting and monitoring at the reverse proxy layer.

## Development

```bash
uv sync          # install deps (including dev group)
uv run pytest    # run tests (37 tests, zero warnings)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture overview and contribution guidelines.

## License

This project is licensed under the [MIT License](LICENSE).

## Acknowledgements

- [FastAPI](https://fastapi.tiangolo.com/)
- Inspired by [ngrok](https://ngrok.com/)
