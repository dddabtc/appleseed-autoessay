"""Open-access PDF fetching with size and content validation."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx

from autoessay.config import get_settings


class OpenAccessUnavailable(RuntimeError):
    """Raised when an open PDF cannot be fetched safely."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def fetch_pdf(url: str, timeout: float, max_size_mb: int) -> bytes:
    settings = get_settings()
    if settings.curator_stub:
        return b"%PDF-1.4"

    if max_size_mb <= 0:
        raise OpenAccessUnavailable("max_size_mb must be positive")

    max_bytes = max_size_mb * 1024 * 1024
    try:
        return await _fetch_pdf_with_httpx(url, timeout=timeout, max_bytes=max_bytes)
    except OpenAccessUnavailable as http_exc:
        if not settings.pdf_fetch_browser_fallback:
            raise
        try:
            return await _fetch_pdf_with_browser(url, timeout=timeout, max_bytes=max_bytes)
        except OpenAccessUnavailable as browser_exc:
            raise OpenAccessUnavailable(
                f"{http_exc}; browser fallback failed: {browser_exc}",
            ) from browser_exc


async def _fetch_pdf_with_httpx(url: str, timeout: float, max_bytes: int) -> bytes:
    backoff_seconds = 0.5
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(4):
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Accept": "application/pdf"},
                ) as response:
                    if response.status_code == 429:
                        if attempt < 3:
                            await asyncio.sleep(backoff_seconds * (2**attempt))
                            continue
                        raise OpenAccessUnavailable("HTTP 429")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise OpenAccessUnavailable(f"HTTP {response.status_code}")

                    content_type = response.headers.get("content-type", "").lower()
                    if "pdf" not in content_type:
                        raise OpenAccessUnavailable(
                            f"content-type is not PDF: {content_type or 'missing'}",
                        )

                    content_length = response.headers.get("content-length")
                    if content_length is not None and _too_large(content_length, max_bytes):
                        raise OpenAccessUnavailable("too large")

                    return _validate_pdf_bytes(
                        await _read_limited_httpx_response(response, max_bytes),
                        max_bytes,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < 3:
                    await asyncio.sleep(backoff_seconds * (2**attempt))
                    continue
                raise OpenAccessUnavailable(str(exc)) from exc

    raise OpenAccessUnavailable("request failed without a response")


async def _read_limited_httpx_response(
    response: httpx.Response,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise OpenAccessUnavailable("too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_pdf_with_browser(url: str, timeout: float, max_bytes: int) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise OpenAccessUnavailable("playwright is not installed") from exc

    timeout_ms = max(1, int(timeout * 1000))
    last_error: OpenAccessUnavailable | None = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()
                response_task = asyncio.create_task(
                    page.wait_for_event(
                        "response",
                        predicate=_looks_like_pdf_response,
                        timeout=timeout_ms,
                    ),
                )
                download_task = asyncio.create_task(
                    page.wait_for_event("download", timeout=timeout_ms),
                )
                try:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001 - browser API wraps navigation failures.
                    response = None
                    last_error = OpenAccessUnavailable(str(exc))
                try:
                    if response is not None:
                        try:
                            return await _pdf_bytes_from_playwright_response(response, max_bytes)
                        except OpenAccessUnavailable as exc:
                            last_error = exc
                    remaining = [task for task in (response_task, download_task) if not task.done()]
                    if remaining:
                        done, _pending = await asyncio.wait(
                            remaining,
                            timeout=timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    else:
                        done = {response_task, download_task}
                    for task in done:
                        try:
                            event = task.result()
                        except Exception as exc:  # noqa: BLE001 - surface browser fallback failure.
                            last_error = OpenAccessUnavailable(str(exc))
                            continue
                        if hasattr(event, "path"):
                            return await _pdf_bytes_from_playwright_download(event, max_bytes)
                        return await _pdf_bytes_from_playwright_response(event, max_bytes)
                finally:
                    for task in (response_task, download_task):
                        if not task.done():
                            task.cancel()
                    await context.close()
            finally:
                await browser.close()
    except OpenAccessUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - browser startup/runtime failures become fetch misses.
        raise OpenAccessUnavailable(str(exc)) from exc
    raise OpenAccessUnavailable(str(last_error or "no PDF response or download observed"))


def _looks_like_pdf_response(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).lower()
    return "pdf" in content_type


async def _pdf_bytes_from_playwright_response(response: Any, max_bytes: int) -> bytes:
    status = int(getattr(response, "status", 0) or 0)
    if status < 200 or status >= 300:
        raise OpenAccessUnavailable(f"browser HTTP {status}")
    headers = getattr(response, "headers", {}) or {}
    content_length = headers.get("content-length")
    if content_length is not None and _too_large(str(content_length), max_bytes):
        raise OpenAccessUnavailable("too large")
    try:
        data = await response.body()
    except Exception as exc:  # noqa: BLE001 - browser API wraps protocol failures.
        raise OpenAccessUnavailable(str(exc)) from exc
    return _validate_pdf_bytes(data, max_bytes)


async def _pdf_bytes_from_playwright_download(download: Any, max_bytes: int) -> bytes:
    try:
        path_text = await download.path()
    except Exception as exc:  # noqa: BLE001 - browser API wraps protocol failures.
        raise OpenAccessUnavailable(str(exc)) from exc
    if not path_text:
        raise OpenAccessUnavailable("download path unavailable")
    path = Path(path_text)
    if path.stat().st_size > max_bytes:
        raise OpenAccessUnavailable("too large")
    return _validate_pdf_bytes(path.read_bytes(), max_bytes)


def _validate_pdf_bytes(data: bytes, max_bytes: int) -> bytes:
    if len(data) > max_bytes:
        raise OpenAccessUnavailable("too large")
    if not data.startswith(b"%PDF-"):
        raise OpenAccessUnavailable("response did not start with PDF magic bytes")
    sha256_bytes(data)
    return data


def _too_large(content_length: str, max_bytes: int) -> bool:
    try:
        return int(content_length) > max_bytes
    except ValueError:
        return False
