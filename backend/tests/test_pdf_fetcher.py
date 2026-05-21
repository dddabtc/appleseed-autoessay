import httpx
import pytest
import respx

from autoessay.clients.pdf_fetcher import OpenAccessUnavailable, fetch_pdf, sha256_bytes
from autoessay.config import get_settings


def _configure_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    *,
    browser_fallback: bool = True,
) -> None:
    monkeypatch.delenv("AUTOESSAY_CURATOR_STUB", raising=False)
    monkeypatch.setenv(
        "AUTOESSAY_PDF_FETCH_BROWSER_FALLBACK",
        "1" if browser_fallback else "0",
    )
    get_settings.cache_clear()


@respx.mock
async def test_fetch_pdf_returns_bytes_and_sha256(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch)
    payload = b"%PDF-1.4 test pdf"
    respx.get("https://example.test/paper.pdf").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=payload,
        ),
    )

    data = await fetch_pdf("https://example.test/paper.pdf", timeout=5.0, max_size_mb=1)

    assert data == payload
    assert sha256_bytes(data) == sha256_bytes(payload)


@respx.mock
async def test_fetch_pdf_rejects_content_type(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch, browser_fallback=False)
    respx.get("https://example.test/not-pdf").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html></html>",
        ),
    )

    with pytest.raises(OpenAccessUnavailable, match="content-type"):
        await fetch_pdf("https://example.test/not-pdf", timeout=5.0, max_size_mb=1)


@respx.mock
async def test_fetch_pdf_stops_at_max_size(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch, browser_fallback=False)
    respx.get("https://example.test/large.pdf").mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-length": str(2 * 1024 * 1024),
            },
            content=b"%PDF-1.4" + (b"x" * 1024),
        ),
    )

    with pytest.raises(OpenAccessUnavailable, match="too large"):
        await fetch_pdf("https://example.test/large.pdf", timeout=5.0, max_size_mb=1)


@respx.mock
async def test_fetch_pdf_retries_http_429(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch)

    async def no_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("autoessay.clients.pdf_fetcher.asyncio.sleep", no_sleep)
    payload = b"%PDF-1.4 retry"
    route = respx.get("https://example.test/retry.pdf").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=payload,
            ),
        ],
    )

    data = await fetch_pdf("https://example.test/retry.pdf", timeout=5.0, max_size_mb=1)

    assert data == payload
    assert route.call_count == 2


@respx.mock
async def test_fetch_pdf_uses_browser_fallback_after_httpx_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch)
    payload = b"%PDF-1.4 browser"
    respx.get("https://example.test/challenge").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>challenge</html>",
        ),
    )
    calls: list[tuple[str, float, int]] = []

    async def fake_browser(url: str, timeout: float, max_bytes: int) -> bytes:
        calls.append((url, timeout, max_bytes))
        return payload

    monkeypatch.setattr(
        "autoessay.clients.pdf_fetcher._fetch_pdf_with_browser",
        fake_browser,
    )

    data = await fetch_pdf("https://example.test/challenge", timeout=7.0, max_size_mb=1)

    assert data == payload
    assert calls == [("https://example.test/challenge", 7.0, 1024 * 1024)]


@respx.mock
async def test_fetch_pdf_browser_fallback_can_be_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch, browser_fallback=False)
    respx.get("https://example.test/challenge-disabled").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>challenge</html>",
        ),
    )
    called = False

    async def fake_browser(url: str, timeout: float, max_bytes: int) -> bytes:
        nonlocal called
        called = True
        raise AssertionError((url, timeout, max_bytes))

    monkeypatch.setattr(
        "autoessay.clients.pdf_fetcher._fetch_pdf_with_browser",
        fake_browser,
    )

    with pytest.raises(OpenAccessUnavailable, match="content-type"):
        await fetch_pdf("https://example.test/challenge-disabled", timeout=5.0, max_size_mb=1)

    assert called is False


@respx.mock
async def test_fetch_pdf_reports_browser_fallback_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_fetcher(monkeypatch)
    respx.get("https://example.test/challenge-fails").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>challenge</html>",
        ),
    )

    async def fake_browser(url: str, timeout: float, max_bytes: int) -> bytes:
        del url, timeout, max_bytes
        raise OpenAccessUnavailable("browser nope")

    monkeypatch.setattr(
        "autoessay.clients.pdf_fetcher._fetch_pdf_with_browser",
        fake_browser,
    )

    with pytest.raises(OpenAccessUnavailable, match="browser fallback failed: browser nope"):
        await fetch_pdf("https://example.test/challenge-fails", timeout=5.0, max_size_mb=1)


def test_pdf_fetch_browser_fallback_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_PDF_FETCH_BROWSER_FALLBACK", "0")
    get_settings.cache_clear()

    assert get_settings().pdf_fetch_browser_fallback is False
