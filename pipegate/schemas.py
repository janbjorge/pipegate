from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Methods = Literal[
    "GET",
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    "OPTIONS",
    "HEAD",
]


class BufferGateCorrelationId(BaseModel):
    correlation_id: uuid.UUID


class BufferGateRequest(BufferGateCorrelationId):
    url_path: str
    url_query: str

    method: Methods
    headers: str
    body: str


class BufferGateResponse(BufferGateCorrelationId):
    headers: str
    body: str
    status_code: int


class JWTPayload(BaseModel):
    sub: str  # UUID of the user or connection_id
    exp: int  # Expiration timestamp


class Settings(BaseSettings):
    model_config = SettingsConfigDict(cli_parse_args=False)  # #10
    connection_id: str | None = Field(alias="PIPEGATE_CONNECTION_ID", default=None)
    jwt_secret: SecretStr = Field(alias="PIPEGATE_JWT_SECRET")
    jwt_algorithms: list[str] = Field(alias="PIPEGATE_JWT_ALGORITHMS")
    max_body_bytes: int = Field(
        alias="PIPEGATE_MAX_BODY_BYTES",
        default=10 * 1024 * 1024,  # 10 MB  (#14)
    )
    max_queue_depth: int = Field(
        alias="PIPEGATE_MAX_QUEUE_DEPTH",
        default=100,  # (#15)
    )
