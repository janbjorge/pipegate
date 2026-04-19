# PipeGate

**Self-hosted HTTP tunnel -- poor man's ngrok.**

Expose a local server to the internet through a single WebSocket. No accounts, no cloud dependencies, no daemon -- just a server on your VPS and a client next to your app. ~400 lines of Python.

```
[Any HTTP client] ---> https://yourserver/a1b2c3/api/data
                              |
                        PipeGate Server
                              |  (WebSocket)
                        PipeGate Client
                              |
                        http://localhost:3000/api/data
```

## Quick Start

```bash
git clone https://github.com/janbjorge/pipegate.git && cd pipegate
uv sync

export PIPEGATE_JWT_SECRET="change-me-to-something-secret"
export PIPEGATE_JWT_ALGORITHMS='["HS256"]'

# Generate a tunnel token (21-day expiry)
pipegate token
# Connection-id: a1b2c3d4...
# JWT Bearer:    eyJhbGci...

# Run the server (on your public VPS)
pipegate server

# Run the client (on your local machine, another terminal)
pipegate client http://localhost:3000 "ws://yourserver:8000/?token=<jwt>"
```

Requests to `http://yourserver:8000/a1b2c3d4/anything` now reach `http://localhost:3000/anything`.

## CLI

```
pipegate token [--connection-id ID]        Generate a JWT bearer token
pipegate client TARGET_URL SERVER_URL      Start the tunnel client
pipegate server [--host HOST] [--port N]   Start the server (default: 0.0.0.0:8000)
```

Pin a specific connection ID so your public URL stays stable across token renewals:

```bash
pipegate token --connection-id my-app
```

## How It Works

A caller hits the server at `/{connection_id}/{path}`. The server wraps the request into a JSON message (method, path, headers, base64-encoded body) tagged with a `correlation_id` (UUID4), and pushes it into an in-memory `asyncio.Queue` for that connection. A background task drains the queue over the WebSocket to the tunnel client.

The client receives the message, makes a real HTTP request to your local service, and sends back a response message with the same `correlation_id`. The server matches it to the waiting `asyncio.Future` and returns the response to the original caller.

Multiple requests fly concurrently over one WebSocket -- the correlation ID is what ties each request to its response. Bodies are base64-encoded so binary payloads survive the JSON text frames.

### What happens when things go wrong

| Situation | What PipeGate does |
|---|---|
| Client is slow / not connected | Queue fills up, caller gets **503** |
| Request body too large | Rejected immediately with **413** |
| Client disconnects mid-request | Pending future fails with **502** |
| No response within 5 minutes | Caller gets **504** |
| Server shuts down | All pending futures resolve with **504** (no hanging requests) |
| WebSocket drops | Client reconnects automatically (exponential backoff, 1s to 60s) |
| Client can't reach local service | Returns **504** to server, which forwards it to caller |

## Authentication

Tunnel connections are JWT-authenticated. The token carries the connection ID as its `sub` claim -- it's the only credential the client needs.

```bash
# Both server and token generator need the same secret
export PIPEGATE_JWT_SECRET="my-secret"
export PIPEGATE_JWT_ALGORITHMS='["HS256"]'

# Generate token
pipegate token

# Client connects with the token
pipegate client http://localhost:3000 "ws://server/?token=<jwt>"
```

External HTTP callers don't need the JWT. They only need the connection ID in the URL path. The server rejects WebSocket connections with missing, expired, or invalid tokens (close code 1008).

## Configuration

Environment variables via pydantic-settings:

| Variable | Required | Default | Description |
|---|---|---|---|
| `PIPEGATE_JWT_SECRET` | Yes | -- | Shared secret for JWT signing/verification |
| `PIPEGATE_JWT_ALGORITHMS` | Yes | -- | Algorithm list, e.g. `'["HS256"]'` |
| `PIPEGATE_CONNECTION_ID` | No | random UUID | Pin a specific connection ID when generating tokens |
| `PIPEGATE_MAX_BODY_BYTES` | No | 10 MB | Reject requests larger than this (413) |
| `PIPEGATE_MAX_QUEUE_DEPTH` | No | 100 | Per-tunnel queue size before returning 503 |

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/healthz` | None | Returns `{"status": "ok"}` |
| `*` | `/{connection_id}/{path}` | None | Tunnel passthrough (all standard HTTP methods) |
| `WS` | `/?token=<jwt>` | JWT | Tunnel client connection |

## Design Notes

**No external state.** The entire coordination layer is `dict[str, asyncio.Queue]` for pending requests and `dict[UUID, asyncio.Future]` for pending responses. This makes PipeGate trivially deployable (single process, no Redis/database), but means it doesn't survive server restarts and doesn't scale horizontally. That's fine for the intended use case.

**Closure-based app factory.** `create_app()` captures all mutable state in a closure rather than using global variables. Each call gets completely fresh state, which makes tests fully isolated without any cleanup fixtures.

**The server injects `x-pipegate-correlation-id`** into forwarded request headers. Your local service can log this to correlate requests end-to-end through the tunnel.

**Query parameters are preserved faithfully** -- including duplicate keys and ordering -- by serializing `multi_items()` as `[[key, value], ...]` rather than collapsing into a dict.

## Development

```bash
uv run pytest tests/ -v             # tests
uv run ruff check . && uv run ruff format --check .  # lint
uv run mypy pipegate/ tests/        # typecheck (strict mode)
```

CI runs lint, typecheck, and tests on Python 3.12 and 3.13.

## License

[MIT](LICENSE)
