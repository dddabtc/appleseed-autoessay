import httpx
import pytest

from autoessay.clients.fulltext_resolver import (
    FulltextResolutionCandidate,
    FulltextResolutionError,
    resolve_fulltext_pdf_url,
)
from autoessay.config import get_settings


def _configure_resolver(monkeypatch: pytest.MonkeyPatch, *, browser_fallback: bool = False) -> None:
    monkeypatch.setenv(
        "AUTOESSAY_PDF_FETCH_BROWSER_FALLBACK",
        "1" if browser_fallback else "0",
    )
    get_settings.cache_clear()


async def test_resolver_finds_citation_pdf_meta(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_resolver(monkeypatch)
    respx_mock.get("https://publisher.test/article").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"""
                <html><head>
                <meta name="citation_pdf_url" content="/download/paper.pdf">
                </head></html>
            """,
        ),
    )

    result = await resolve_fulltext_pdf_url(
        [FulltextResolutionCandidate("https://publisher.test/article", "landing")],
    )

    assert result.pdf_url == "https://publisher.test/download/paper.pdf"
    assert result.method == "html_meta"
    assert any(item["status"] == "resolved" for item in result.diagnostics)


async def test_resolver_finds_anchor_with_pdf_label(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_resolver(monkeypatch)
    respx_mock.get("https://doi.org/10.1000/example").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b'<a href="/article/download?id=42">Download PDF</a>',
        ),
    )

    result = await resolve_fulltext_pdf_url(
        [FulltextResolutionCandidate("https://doi.org/10.1000/example", "doi")],
    )

    assert result.pdf_url == "https://doi.org/article/download?id=42"
    assert result.method == "html_anchor"


async def test_resolver_skips_login_pdf_links(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _configure_resolver(monkeypatch)
    respx_mock.get("https://publisher.test/article").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b'<a href="/login?next=/paper.pdf">PDF</a>',
        ),
    )

    with pytest.raises(FulltextResolutionError, match="unsafe_or_login"):
        await resolve_fulltext_pdf_url(
            [FulltextResolutionCandidate("https://publisher.test/article", "landing")],
        )


async def test_resolver_uses_browser_fallback_after_html_miss(
    respx_mock,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _configure_resolver(monkeypatch, browser_fallback=True)
    respx_mock.get("https://publisher.test/challenge").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>JS challenge</html>",
        ),
    )

    async def fake_browser(url: str, *, timeout: float, max_clicks: int):  # type: ignore[no-untyped-def]
        assert url == "https://publisher.test/challenge"
        assert timeout == 12.0
        assert max_clicks == 3
        from autoessay.clients.fulltext_resolver import _ResolvedLink

        return _ResolvedLink(
            "https://publisher.test/paper.pdf",
            "browser_dom_pdf_link",
            "dom",
        )

    monkeypatch.setattr("autoessay.clients.fulltext_resolver._resolve_with_browser", fake_browser)

    result = await resolve_fulltext_pdf_url(
        [FulltextResolutionCandidate("https://publisher.test/challenge", "landing")],
    )

    assert result.pdf_url == "https://publisher.test/paper.pdf"
    assert result.method == "browser_dom_pdf_link"
