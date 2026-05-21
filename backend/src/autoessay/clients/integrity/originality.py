"""Originality.AI integrity adapter."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import httpx

from autoessay.clients.integrity import (
    IntegrityClientError,
    NormalizedScanResult,
    NormalizedScanSpan,
    document_hash,
    normalize_kind,
)
from autoessay.config import get_settings


class OriginalityClient:
    vendor = "originality_ai"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        backoff_seconds: float = 0.5,
    ) -> None:
        settings = get_settings()
        self._base_url = base_url or settings.originality_base_url
        self._api_key = api_key if api_key is not None else settings.originality_api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=self._base_url, timeout=60.0)
        self._backoff_seconds = backoff_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def scan(self, text: str, kind: str) -> NormalizedScanResult:
        scan_kind = normalize_kind(kind)
        if get_settings().integrity_stub:
            return _stub_result(text, scan_kind)
        if not self._api_key:
            raise IntegrityClientError("ORIGINALITY_API_KEY is not configured")
        endpoint = "/api/v1/scan/ai" if scan_kind == "ai_style" else "/api/v1/scan/plagiarism"
        raw = await self._post_json(
            endpoint,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json_payload={"text": text, "scan_type": scan_kind},
            retries=1,
        )
        return _normalize_result(raw, text, scan_kind)

    async def _post_json(
        self,
        path: str,
        *,
        headers: dict[str, str],
        json_payload: dict[str, object],
        retries: int,
    ) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.post(path, headers=headers, json=json_payload)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        await asyncio.sleep(self._backoff_seconds * (2**attempt))
                        continue
                    raise IntegrityClientError(f"Originality.AI HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise IntegrityClientError("Originality.AI response was not an object")
                return payload
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise IntegrityClientError(str(exc)) from exc
        if response is not None:
            raise IntegrityClientError(f"Originality.AI HTTP {response.status_code}")
        raise IntegrityClientError("Originality.AI request failed")


async def scan(text: str, kind: str) -> NormalizedScanResult:
    client = OriginalityClient()
    try:
        return await client.scan(text, kind)
    finally:
        await client.aclose()


def _normalize_result(raw: dict[str, Any], text: str, kind: str) -> NormalizedScanResult:
    scan_id = _string_value(raw, ("scan_id", "id", "request_id")) or f"originality_{uuid4().hex}"
    score = _score_value(raw, kind)
    return NormalizedScanResult(
        vendor=OriginalityClient.vendor,
        scan_type=kind,
        document_hash=document_hash(text),
        status=_string_value(raw, ("status",)) or "complete",
        score=score,
        spans=_span_values(raw),
        scan_id=scan_id,
        raw_response=raw,
    )


def _stub_result(text: str, kind: str) -> NormalizedScanResult:
    raw = {
        "scan_id": f"stub_originality_{kind}",
        "status": "complete",
        "score": 0.08 if kind == "plagiarism" else 0.22,
        "spans": [
            {
                "span_id": f"stub-{kind}-001",
                "start": 0,
                "end": min(80, len(text)),
                "label": "low_overlap" if kind == "plagiarism" else "ai_likelihood_low",
                "confidence": 0.22,
            },
        ],
    }
    return _normalize_result(raw, text, kind)


def _string_value(raw: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _score_value(raw: dict[str, Any], kind: str) -> float | None:
    keys = ("score", "ai_score", "plagiarism_score", "probability")
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int | float):
            return float(value)
    nested_key = "ai" if kind == "ai_style" else "plagiarism"
    nested = raw.get(nested_key)
    if isinstance(nested, dict):
        value = nested.get("score")
        if isinstance(value, int | float):
            return float(value)
    return None


def _span_values(raw: dict[str, Any]) -> list[NormalizedScanSpan]:
    raw_spans = raw.get("spans")
    if not isinstance(raw_spans, list):
        raw_spans = raw.get("matches")
    if not isinstance(raw_spans, list):
        return []
    spans: list[NormalizedScanSpan] = []
    for index, item in enumerate(raw_spans, start=1):
        if not isinstance(item, dict):
            continue
        start = _int_value(item, ("start", "start_index", "offset"))
        end = _int_value(item, ("end", "end_index"))
        length = _int_value(item, ("length",))
        if end is None and start is not None and length is not None:
            end = start + length
        if start is None or end is None or end < start:
            continue
        confidence = _float_value(item, ("confidence", "score", "probability"))
        span_id = _string_value(item, ("span_id", "id")) or f"originality-span-{index:03d}"
        label = _string_value(item, ("label", "type", "category")) or "integrity_match"
        source_url = _string_value(item, ("source_url", "url"))
        text = _string_value(item, ("text", "matched_text"))
        spans.append(
            NormalizedScanSpan(
                span_id=span_id,
                start=start,
                end=end,
                label=label,
                confidence=confidence,
                source_url=source_url,
                text=text,
            ),
        )
    return spans


def _int_value(raw: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _float_value(raw: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None
