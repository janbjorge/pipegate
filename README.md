# PipeGate

A lightweight, self-hosted tunneling proxy built with FastAPI. Expose local servers to the internet — a poor man's ngrok.

## How It Works

PipeGate has two sides: a **server** you deploy on public infrastructure, and a **client** that runs on your local machine.

1. The server accepts incoming HTTP requests at `/{connection_id}/{path}`.
2. A WebSocket tunnel client connects to `/{connection_id}?token=<jwt>` and receives forwarded requests.
3. The client forwards each request to your local service, then sends the response back over the WebSocket.
4. The server returns that response to the original HTTP caller.

Requests are correlated via a `x-pipegate-correlation-id` header injected by the server. Request and response bodies are base64-encoded for binary-safe transport over the JSON WebSocket channel.

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

| Variable | Required | Default | Description |
|---|---|---|---|
| `PIPEGATE_JWT_SECRET` | **Yes** | — | Secret key for signing/verifying JWT tokens |
| `PIPEGATE_JWT_ALGORITHMS` | **Yes** | — | JSON array of algorithms, e.g. `'["HS256"]'` |
| `PIPEGATE_CONNECTION_ID` | No | random hex UUID | Custom connection ID for token generation |
| `PIPEGATE_MAX_BODY_BYTES` | No | `10485760` (10 MB) | Maximum request body size; larger bodies are rejected with 413 |
| `PIPEGATE_MAX_QUEUE_DEPTH` | No | `100` | Maximum number of queued requests per tunnel connection; excess returns 503 |

Set the required variables before running any command:

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
python -m pipegate.client http://localhost:3000 "ws://yourserver:8000/{connection_id}?token={jwt}"
```

Arguments:

- `target_url` — the local service to forward requests to (e.g. `http://localhost:3000`)
- `server_url` — the PipeGate server WebSocket endpoint, including the `?token=` query parameter

The client connects to the server via WebSocket, receives forwarded requests, proxies them to your local service, and sends responses back through the tunnel. It automatically reconnects with **exponential backoff** (1 s → 2 s → 4 s … up to 60 s) on any connection failure.

### Endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /healthz` | None | Health check — returns `{"status": "ok"}` with HTTP 200 |
| `GET POST PUT DELETE PATCH OPTIONS HEAD /{connection_id}/{path}` | `Authorization: Bearer <jwt>` | Tunnel HTTP endpoint |
| `WS /{connection_id}?token=<jwt>` | `?token=` query param | WebSocket endpoint for tunnel clients |

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

Requests time out after **300 seconds** (5 minutes). If the tunnel client disconnects while requests are in-flight, those requests fail immediately with 503 rather than waiting for the timeout.

## HTTP Behaviour

- **Binary bodies** — request and response bodies are base64-encoded for transport, so file uploads, protobuf, and any non-UTF-8 content are handled correctly.
- **Duplicate query parameters** — `?a=1&a=2` is preserved as-is and forwarded with both values.
- **HEAD requests** — the response body is stripped as required by the HTTP spec, even if the local service returns one.
- **Request body limit** — bodies exceeding `PIPEGATE_MAX_BODY_BYTES` are rejected with `413 Request Entity Too Large` before they reach the queue.
- **Queue backpressure** — when a connection's queue is full (tunnel client too slow or not connected), new requests get `503 Service Unavailable` immediately.

## Security

- **HTTP endpoints require JWT auth** — requests without a valid `Authorization: Bearer <token>` header are rejected with 401/403.
- **WebSocket endpoint requires JWT auth** — the token is passed as a `?token=<jwt>` query parameter (WebSocket clients cannot send custom headers). Connections without a valid token are rejected with close code 1008 before being accepted.
- Use **HTTPS/WSS** in production to encrypt traffic and prevent token interception.
- Use a strong, random `PIPEGATE_JWT_SECRET`.
- Tokens are valid for 21 days — rotate them periodically.
- Consider rate limiting and monitoring at the reverse proxy layer.

## Development

```bash
uv sync               # install deps including dev group
uv run pytest         # run tests
uv run ruff check .   # lint
uv run mypy pipegate/ tests/  # type check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture overview and contribution guidelines.

## License

This project is licensed under the [MIT License](LICENSE).

## Acknowledgements

- [FastAPI](https://fastapi.tiangolo.com/)
- Inspired by [ngrok](https://ngrok.com/)
