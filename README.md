# PipeGate

A lightweight, self-hosted tunneling proxy built with FastAPI. Expose local servers to the internet.

## How It Works

1. The **server** accepts HTTP requests at `/{connection_id}/{path}` and holds them.
2. A **client** connects via WebSocket to `/{connection_id}?token=<jwt>`, receives forwarded requests, proxies them to your local service, and sends responses back.
3. The server returns the response to the original caller.

Request/response bodies are base64-encoded for binary-safe transport over the JSON WebSocket channel.

## Quick Start

```bash
# Install
git clone https://github.com/janbjorge/pipegate.git && cd pipegate
uv sync

# Configure
export PIPEGATE_JWT_SECRET="change-me-to-something-secret"
export PIPEGATE_JWT_ALGORITHMS='["HS256"]'

# Generate a token
python -m pipegate.auth
# Connection-id: a1b2c3d4...
# JWT Bearer:    eyJhbGci...

# Start the server
python -m pipegate.server

# Start the client (in another terminal)
python -m pipegate.client http://localhost:3000 "ws://yourserver:8000/{connection_id}?token={jwt}"
```

## Configuration

Environment variables (via pydantic-settings):

| Variable | Required | Default | Description |
|---|---|---|---|
| `PIPEGATE_JWT_SECRET` | Yes | — | Secret for signing/verifying JWTs |
| `PIPEGATE_JWT_ALGORITHMS` | Yes | — | JSON array, e.g. `'["HS256"]'` |
| `PIPEGATE_CONNECTION_ID` | No | random UUID hex | Custom connection ID for token generation |
| `PIPEGATE_MAX_BODY_BYTES` | No | 10 MB | Max request body size (413 if exceeded) |
| `PIPEGATE_MAX_QUEUE_DEPTH` | No | 100 | Max queued requests per connection (503 if exceeded) |

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /healthz` | Health check — `{"status": "ok"}` |
| `* /{connection_id}/{path}` | Tunnel HTTP endpoint (all methods) |
| `WS /{connection_id}?token=<jwt>` | WebSocket tunnel (JWT required) |

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy pipegate/ tests/
```

## License

[MIT](LICENSE)
