"""Normalized integrity scan client models."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field

ScanKind = str


class IntegrityClientError(RuntimeError):
    """Raised when an integrity vendor cannot complete a scan."""


class NormalizedScanSpan(BaseModel):
    span_id: str
    start: int
    end: int
    label: str
    confidence: float | None = None
    source_url: str | None = None
    text: str | None = None

    class Config:
        extra = "ignore"


class NormalizedScanResult(BaseModel):
    vendor: str
    scan_type: str
    document_hash: str
    status: str
    score: float | None = None
    spans: list[NormalizedScanSpan] = Field(default_factory=list)
    raw_report_path: str | None = None
    scan_id: str
    raw_response: dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "ignore"


def document_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def normalize_kind(kind: str) -> str:
    if kind not in {"plagiarism", "ai_style"}:
        raise ValueError(f"unsupported integrity scan kind: {kind}")
    return kind
