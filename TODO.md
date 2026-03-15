# PipeGate — Bugs, Refactors & Features

## Bugs

- [x] **#1 (High) Race condition — future created after enqueue**
  `server.py:66,124` — `asyncio.Future` is inserted into the `futures` dict only when the HTTP handler reaches `await futures[correlation_id]`, *after* the request is already on the queue. If the WebSocket client responds extremely fast the future does not yet exist, `futures.get()` returns `None`, and the response is silently dropped. Fix: create and store the future explicitly *before* calling `buffers[connection_id].put(...)`.

- [x] **#2 (Medium) Dropped message on WebSocket disconnect during `send()`**
  `server.py:168-177` — the item is dequeued from the buffer before the send is attempted. If the WebSocket disconnects mid-send the item is lost and the waiting HTTP future silently times out after 300 s. Fix: re-queue the item or immediately fail its future on disconnect.

- [x] **#3 (Low) `buffers` defaultdict leaks on connection close**
  `server.py:63` — the `buffers` dict is never pruned. Every `connection_id` that ever connects leaves a permanent `asyncio.Queue` entry. Fix: delete `buffers[connection_id]` when the WebSocket handler exits.

- [x] **#4 (Low) `async-timeout` is a redundant dependency**
  `pyproject.toml:19` — `async_timeout` has been part of the Python standard library as `asyncio.timeout()` since Python 3.11. The project already requires Python ≥ 3.12. Fix: remove the `async-timeout` dependency and replace `async_timeout.timeout(...)` with `asyncio.timeout(...)`.

- [x] **#5 (Medium) Duplicate query parameters silently lost**
  `server.py:103-105` — `{k: v for k, v in request.query_params.multi_items()}` collapses repeated keys. `?a=1&a=2` becomes `{"a": "2"}`. Fix: serialize as a list of pairs, e.g. `list(request.query_params.multi_items())`, and update the client to reconstruct params accordingly.

- [x] **#6 (Medium) Binary request body corruption**
  `server.py:112`, `client.py:49` — `(await request.body()).decode()` and `request.body.encode()` will raise or silently corrupt any non-UTF-8 binary body (file uploads, protobuf, etc.). Fix: base64-encode the body for transport and update `BufferGateRequest.body` to carry a base64 string.

- [x] **#7 (Medium) Response body charset-unsafe**
  `client.py:54`, `server.py:131` — `response.text` performs charset detection and may mangle binary data; `response.body.encode()` on the server side compounds the issue. Fix: use `base64.b64encode(response.content)` on the client and `base64.b64decode(response.body)` on the server, mirroring the fix for #6.

---

## Refactors

- [x] **#9 Replace `print()` with `logging`**
  `server.py:142,155,157,159,161,163,172,175,177,180` — all diagnostic output uses bare `print()`, making it impossible to filter log levels or integrate with structured logging. Fix: add `logger = logging.getLogger(__name__)` and replace every `print(...)` with the appropriate `logger.info/warning/error(...)`.

- [x] **#10 Fix `Settings` `cli_parse_args=True` default**
  `schemas.py:45` — `SettingsConfigDict(cli_parse_args=True)` causes `Settings()` to parse `sys.argv` in any context (imports, tests, library use), which can fail unexpectedly. Every call site already works around this with `_cli_parse_args=False`. Fix: change the default to `cli_parse_args=False`; only `make_jwt_bearer()` in `auth.py` should pass `True`.

- [x] **#11 Fail waiting futures immediately on WebSocket disconnect**
  `server.py:136-180` — when the WebSocket client disconnects, all in-flight HTTP futures for that `connection_id` silently wait until their individual 300 s timeouts expire. Fix: when either `receive()` or `send()` exits due to disconnect, iterate `futures` and immediately set a 503/502 exception on any future whose `correlation_id` belongs to that connection.

---

## Features

- [x] **#12 (High) Client reconnect with exponential backoff**
  `client.py:72-117` — `main()` makes a single connection attempt and exits on any error or disconnect. Fix: wrap the connection block in a retry loop with exponential backoff (e.g. 1 s, 2 s, 4 s … up to a configurable cap), logging each attempt.

- [x] **#13 (High) WebSocket authentication**
  `server.py:136-141` — the WebSocket endpoint is completely unauthenticated. Anyone who knows the `connection_id` can register as a tunnel client and intercept all traffic for that connection. Fix: require a JWT passed as a `token` query parameter on the WebSocket URL (WS clients cannot send custom headers), verify it with the existing `verify_token()` logic before calling `websocket.accept()`.

- [x] **#14 (Medium) Configurable request/response size limit**
  No limit is enforced on body size. A large upload or response is held entirely in memory as a string inside the queue and futures dict. Fix: add a `max_body_bytes: int` field to `Settings` (e.g. default 10 MB) and reject requests/responses that exceed it with a 413.

- [x] **#15 (Medium) Per-connection queue backpressure**
  `server.py:63` — `asyncio.Queue()` is unbounded. A slow or offline tunnel client causes the queue to grow without limit. Fix: use `asyncio.Queue(maxsize=N)` with a configurable `max_queue_depth` in `Settings`, and return 503 when the queue is full.

- [x] **#16 (Low) Strip response body for `HEAD` requests**
  `server.py:85-87` — `HEAD` is registered as a supported method but the server always forwards and returns a body. HTTP requires `HEAD` responses to have no body. Fix: if `request.method == "HEAD"`, return the response with `content=b""`.

- [x] **#17 (Low) Add `GET /healthz` health endpoint**
  There is no health or readiness endpoint, making the server incompatible with Kubernetes liveness probes, ECS health checks, and uptime monitors. Fix: add a `GET /healthz` route that returns `{"status": "ok"}` with HTTP 200, registered before the catch-all `/{connection_id}/{path_slug:path}` route.
