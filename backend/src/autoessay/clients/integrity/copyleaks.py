"""Copyleaks integrity adapter."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
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


class CopyleaksClient:
    vendor = "copyleaks"

    def __init__(
        self,
        *,
        auth_base_url: str | None = None,
        api_base_url: str | None = None,
        email: str | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        backoff_seconds: float = 0.5,
    ) -> None:
        settings = get_settings()
        self._auth_base_url = auth_base_url or settings.copyleaks_auth_base_url
        self._api_base_url = api_base_url or settings.copyleaks_base_url
        self._email = email if email is not None else settings.copyleaks_email
        self._api_key = api_key if api_key is not None else settings.copyleaks_api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=60.0)
        self._backoff_seconds = backoff_seconds
        self._jwt_token: str | None = None
        self._jwt_expires_at: datetime | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def scan(self, text: str, kind: str) -> NormalizedScanResult:
        scan_kind = normalize_kind(kind)
        if get_settings().integrity_stub:
            return _stub_result(text, scan_kind)
        token = await self._token()
        scan_id = f"autoessay-{uuid4().hex}"
        raw = await self._post_json(
            f"{self._api_base_url}/v3/scans/submit/file/{scan_id}",
            headers={"Authorization": f"Bearer {token}"},
            json_payload={
                "base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "filename": "manuscript.txt",
                "properties": {
                    "sandbox": True,
                    "webhooks": {"status": ""},
                    "aiGeneratedText": scan_kind == "ai_style",
                },
            },
            retries=1,
        )
        raw.setdefault("scan_id", scan_id)
        return _normalize_result(raw, text, scan_kind)

    async def _token(self) -> str:
        if (
            self._jwt_token is not None
            and self._jwt_expires_at is not None
            and datetime.now(timezone.utc) < self._jwt_expires_at
        ):
            return self._jwt_token
        if not self._email or not self._api_key:
            raise IntegrityClientError("COPYLEAKS_EMAIL and COPYLEAKS_API_KEY are required")
        raw = await self._post_json(
            f"{self._auth_base_url}/v3/account/login/api",
            headers={},
            json_payload={"email": self._email, "key": self._api_key},
            retries=1,
        )
        token = _string_value(raw, ("access_token", "token"))
        if token is None:
            raise IntegrityClientError("Copyleaks login did not return a JWT")
        self._jwt_token = token
        self._jwt_expires_at = datetime.now(timezone.utc) + timedelta(hours=47)
        return token

    async def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_payload: dict[str, object],
        retries: int,
    ) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                response = await self._client.post(url, headers=headers, json=json_payload)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        await asyncio.sleep(self._backoff_seconds * (2**attempt))
                        continue
                    raise IntegrityClientError(f"Copyleaks HTTP {response.status_code}")
                response.raise_for_status()
                if not response.content:
                    return {"status": "submitted"}
                payload = response.json()
                if not isinstance(payload, dict):
                    raise IntegrityClientError("Copyleaks response was not an object")
                return payload
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise IntegrityClientError(str(exc)) from exc
        if response is not None:
            raise IntegrityClientError(f"Copyleaks HTTP {response.status_code}")
        raise IntegrityClientError("Copyleaks request failed")


async def scan(text: str, kind: str) -> NormalizedScanResult:
    client = CopyleaksClient()
    try:
        return await client.scan(text, kind)
    finally:
        await client.aclose()


def _normalize_result(raw: dict[str, Any], text: str, kind: str) -> NormalizedScanResult:
    scan_id = _string_value(raw, ("scan_id", "scannedDocument", "id")) or f"copyleaks_{uuid4().hex}"
    score = _score_value(raw, kind)
    return NormalizedScanResult(
        vendor=CopyleaksClient.vendor,
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
        "scan_id": f"stub_copyleaks_{kind}",
        "status": "complete",
        "score": 0.11 if kind == "plagiarism" else 0.24,
        "results": {
            "spans": [
                {
                    "span_id": f"stub-copyleaks-{kind}-001",
                    "start": 0,
                    "end": min(70, len(text)),
                    "label": "possible_match",
                    "confidence": 0.24,
                    "source_url": "https://example.invalid/source",
                },
            ],
        },
    }
    return _normalize_result(raw, text, kind)


def _score_value(raw: dict[str, Any], kind: str) -> float | None:
    for key in ("score", "totalScore", "plagiarismScore", "aiScore"):
        value = raw.get(key)
        if isinstance(value, int | float):
            return float(value)
    nested = raw.get("summary")
    if isinstance(nested, dict):
        key = "aiScore" if kind == "ai_style" else "plagiarismScore"
        value = nested.get(key) or nested.get("score")
        if isinstance(value, int | float):
            return float(value)
    return None


def _span_values(raw: dict[str, Any]) -> list[NormalizedScanSpan]:
    raw_spans: object = raw.get("spans")
    results = raw.get("results")
    if not isinstance(raw_spans, list) and isinstance(results, dict):
        raw_spans = results.get("spans")
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
                span_id=_string_value(item, ("span_id", "id")) or f"copyleaks-span-{index:03d}",
                start=start,
                end=end,
                label=_string_value(item, ("label", "type")) or "possible_match",
                confidence=_float_value(item, ("confidence", "score", "probability")),
                source_url=_string_value(item, ("source_url", "url")),
                text=_string_value(item, ("text", "matched_text")),
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
