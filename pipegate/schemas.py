from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel

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
