# Contributing to PipeGate

## Project Structure

```
pipegate/
  schemas.py    # Pydantic models and settings
  auth.py       # JWT verification and token generation (no framework deps)
  server.py     # FastAPI routes, WebSocket handler, app factory
```

- **`schemas.py`** — data models only. No logic.
- **`auth.py`** — pure auth logic. Depends on `schemas` and `PyJWT`, nothing else.
  Also serves as the CLI entry point for token generation (`python -m pipegate.auth`).
- **`server.py`** — all FastAPI/WebSocket code. Calls into `auth.py` for JWT
  verification. Owns the in-memory request buffers and response futures.

## Guidelines

- Keep `auth.py` free of framework imports (no FastAPI, no WebSocket).
  This makes auth logic testable without spinning up a server.
- HTTP-specific concerns (status codes, `HTTPException`) belong in `server.py`.
- New pure logic should go in its own module, not inside route handlers.

## Running

```bash
python -m pipegate.server   # start the server
python -m pipegate.auth     # generate a JWT token
```
