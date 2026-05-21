"""GPTZero AI-style integrity adapter."""

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


class GPTZeroClient:
    vendor = "gptzero"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        backoff_seconds: float = 0.5,
    ) -> None:
        settings = get_settings()
        self._base_url = base_url or settings.gptzero_base_url
        self._api_key = api_key if api_key is not None else settings.gptzero_api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=self._base_url, timeout=60.0)
        self._backoff_seconds = backoff_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def scan(self, text: str, kind: str) -> NormalizedScanResult:
        scan_kind = normalize_kind(kind)
        if scan_kind != "ai_style":
            raise IntegrityClientError("GPTZero only supports ai_style scans")
        if get_settings().integrity_stub:
            return _stub_result(text)
        if not self._api_key:
            raise IntegrityClientError("GPTZERO_API_KEY is not configured")
        raw = await self._post_json(
            "/v2/predict/text",
            headers={"x-api-key": self._api_key},
            json_payload={"document": text},
            retries=1,
        )
        return _normalize_result(raw, text)

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
                    raise IntegrityClientError(f"GPTZero HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise IntegrityClientError("GPTZero response was not an object")
                return payload
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise IntegrityClientError(str(exc)) from exc
        if response is not None:
            raise IntegrityClientError(f"GPTZero HTTP {response.status_code}")
        raise IntegrityClientError("GPTZero request failed")


async def scan(text: str, kind: str) -> NormalizedScanResult:
    client = GPTZeroClient()
    try:
        return await client.scan(text, kind)
    finally:
        await client.aclose()


def _normalize_result(raw: dict[str, Any], text: str) -> NormalizedScanResult:
    documents = raw.get("documents")
    first_document = documents[0] if isinstance(documents, list) and documents else {}
    doc = first_document if isinstance(first_document, dict) else {}
    scan_id = _string_value(raw, ("scan_id", "id", "request_id"))
    if scan_id is None:
        scan_id = _string_value(doc, ("scan_id", "id", "request_id"))
    score = _float_value(raw, ("score", "ai_probability", "completely_generated_prob"))
    if score is None:
        score = _float_value(doc, ("score", "ai_probability", "completely_generated_prob"))
    spans = _span_values(raw)
    if not spans:
        spans = _span_values(doc)
    return NormalizedScanResult(
        vendor=GPTZeroClient.vendor,
        scan_type="ai_style",
        document_hash=document_hash(text),
        status=_string_value(raw, ("status",)) or "complete",
        score=score,
        spans=spans,
        scan_id=scan_id or f"gptzero_{uuid4().hex}",
        raw_response=raw,
    )


def _stub_result(text: str) -> NormalizedScanResult:
    raw = {
        "scan_id": "stub_gptzero_ai_style",
        "status": "complete",
        "score": 0.19,
        "spans": [
            {
                "span_id": "stub-ai-style-001",
                "start": 0,
                "end": min(60, len(text)),
                "label": "ai_likelihood_low",
                "confidence": 0.19,
            },
        ],
    }
    return _normalize_result(raw, text)


def _span_values(raw: dict[str, Any]) -> list[NormalizedScanSpan]:
    raw_spans = raw.get("spans")
    if not isinstance(raw_spans, list):
        raw_spans = raw.get("sentences")
    if not isinstance(raw_spans, list):
        return []
    spans: list[NormalizedScanSpan] = []
    for index, item in enumerate(raw_spans, start=1):
        if not isinstance(item, dict):
            continue
        start = _int_value(item, ("start", "start_index", "offset"))
        end = _int_value(item, ("end", "end_index"))
        if start is None or end is None or end < start:
            continue
        spans.append(
            NormalizedScanSpan(
                span_id=_string_value(item, ("span_id", "id")) or f"gptzero-span-{index:03d}",
                start=start,
                end=end,
                label=_string_value(item, ("label", "type")) or "ai_likelihood",
                confidence=_float_value(item, ("confidence", "score", "probability")),
                text=_string_value(item, ("text", "sentence")),
            ),
        )
    return spans


def _string_value(raw: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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
