"""Resolve DOI and landing-page URLs to direct PDF URLs.

This layer deliberately stops at URL resolution. The caller must pass the
returned URL to :mod:`autoessay.clients.pdf_fetcher` for PDF download,
content-type validation, size limits, and checksum handling.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from autoessay.config import get_settings

MAX_HTML_BYTES = 512 * 1024
MAX_BROWSER_CLICKS = 3
LOGIN_URL_TERMS = (
    "login",
    "signin",
    "sign-in",
    "sso",
    "shibboleth",
    "oauth",
    "cas/login",
)
PDF_TEXT_TERMS = (
    "pdf",
    "download pdf",
    "full text pdf",
    "article pdf",
    "view pdf",
    "下载pdf",
    "下载 pdf",
    "全文pdf",
)


class FulltextResolutionError(RuntimeError):
    """Raised when no safe direct PDF URL can be found."""


@dataclass(frozen=True)
class FulltextResolutionCandidate:
    url: str
    kind: str


@dataclass(frozen=True)
class FulltextResolution:
    pdf_url: str
    method: str
    source_url: str
    diagnostics: list[dict[str, object]] = field(default_factory=list)


@dataclass
class _ResolvedLink:
    url: str
    method: str
    label: str


async def resolve_fulltext_pdf_url(
    candidates: Sequence[FulltextResolutionCandidate],
    *,
    timeout: float = 12.0,
    max_html_bytes: int = MAX_HTML_BYTES,
    max_browser_clicks: int = MAX_BROWSER_CLICKS,
) -> FulltextResolution:
    """Resolve DOI/landing-page candidates to a direct PDF URL.

    The resolver is bounded by response size, timeout, and click count. It does
    not attempt authentication flows and ignores login/account URLs.
    """

    diagnostics: list[dict[str, object]] = []
    normalized = _dedupe_candidates(candidates)
    if not normalized:
        raise FulltextResolutionError("no fulltext candidates")

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            "User-Agent": "appleseed-autoessay-fulltext-resolver/1.0",
        },
    ) as client:
        for candidate in normalized:
            if not _is_safe_http_url(candidate.url):
                diagnostics.append(_diag(candidate, "skipped", "unsafe_url"))
                continue
            try:
                resolved = await _resolve_with_httpx(
                    client,
                    candidate,
                    max_html_bytes=max_html_bytes,
                    diagnostics=diagnostics,
                )
            except httpx.HTTPError as exc:
                diagnostics.append(_diag(candidate, "miss", str(exc)))
                continue
            except FulltextResolutionError as exc:
                diagnostics.append(_diag(candidate, "miss", str(exc)))
                continue
            return FulltextResolution(
                pdf_url=resolved.url,
                method=resolved.method,
                source_url=candidate.url,
                diagnostics=diagnostics,
            )

    if get_settings().pdf_fetch_browser_fallback:
        for candidate in normalized:
            if not _is_safe_http_url(candidate.url):
                continue
            try:
                resolved = await _resolve_with_browser(
                    candidate.url,
                    timeout=timeout,
                    max_clicks=max_browser_clicks,
                )
            except FulltextResolutionError as exc:
                diagnostics.append(_diag(candidate, "browser_miss", str(exc)))
                continue
            diagnostics.append(
                _diag(candidate, "resolved", resolved.method, pdf_url=resolved.url),
            )
            return FulltextResolution(
                pdf_url=resolved.url,
                method=resolved.method,
                source_url=candidate.url,
                diagnostics=diagnostics,
            )

    raise FulltextResolutionError(_failure_summary(diagnostics))


async def _resolve_with_httpx(
    client: httpx.AsyncClient,
    candidate: FulltextResolutionCandidate,
    *,
    max_html_bytes: int,
    diagnostics: list[dict[str, object]],
) -> _ResolvedLink:
    response = await client.get(candidate.url)
    diagnostics.append(
        _diag(candidate, "http", f"HTTP {response.status_code}", final_url=str(response.url)),
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise FulltextResolutionError(f"HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type:
        return _ResolvedLink(str(response.url), "http_pdf_response", "content-type")
    if "html" not in content_type and "xml" not in content_type and content_type:
        raise FulltextResolutionError(f"content-type is not HTML/PDF: {content_type}")
    content_length = response.headers.get("content-length")
    if content_length is not None and _too_large(content_length, max_html_bytes):
        raise FulltextResolutionError("landing page too large")
    html = _limited_text(response.content, max_html_bytes, response.encoding or "utf-8")
    parser = _PdfLinkParser()
    parser.feed(html)
    for link in parser.links(str(response.url)):
        if not _is_safe_pdf_target(link.url):
            diagnostics.append(
                _diag(candidate, "skipped_link", "unsafe_or_login", pdf_url=link.url),
            )
            continue
        diagnostics.append(_diag(candidate, "resolved", link.method, pdf_url=link.url))
        return link
    raise FulltextResolutionError("no PDF link found in landing HTML")


async def _resolve_with_browser(url: str, *, timeout: float, max_clicks: int) -> _ResolvedLink:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FulltextResolutionError("playwright is not installed") from exc

    timeout_ms = max(1, int(timeout * 1000))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(accept_downloads=True)
                response_task: asyncio.Task[Any] | None = None
                download_task: asyncio.Task[Any] | None = None
                try:
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
                        raise FulltextResolutionError(str(exc)) from exc
                    if response is not None and _looks_like_pdf_response(response):
                        return _ResolvedLink(response.url, "browser_pdf_response", "content-type")
                    for href in await _browser_pdf_hrefs(page):
                        if _is_safe_pdf_target(href):
                            return _ResolvedLink(href, "browser_dom_pdf_link", "dom")
                    clicked = 0
                    for selector in await _browser_pdf_button_selectors(page):
                        if clicked >= max_clicks:
                            break
                        clicked += 1
                        try:
                            await page.locator(selector).first.click(timeout=min(timeout_ms, 3000))
                        except Exception:
                            continue
                        event = await _first_browser_pdf_event(
                            response_task=response_task,
                            download_task=download_task,
                            timeout=min(timeout, 5.0),
                        )
                        if event is not None:
                            url_from_event = _url_from_browser_event(event)
                            if url_from_event and _is_safe_pdf_target(url_from_event):
                                return _ResolvedLink(
                                    url_from_event,
                                    "browser_pdf_click",
                                    selector,
                                )
                    event = await _first_browser_pdf_event(
                        response_task=response_task,
                        download_task=download_task,
                        timeout=1.0,
                    )
                    if event is not None:
                        url_from_event = _url_from_browser_event(event)
                        if url_from_event and _is_safe_pdf_target(url_from_event):
                            return _ResolvedLink(url_from_event, "browser_pdf_event", "event")
                finally:
                    tasks = [task for task in (response_task, download_task) if task is not None]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    await context.close()
            finally:
                await browser.close()
    except FulltextResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert browser runtime errors to resolver misses.
        raise FulltextResolutionError(str(exc)) from exc
    raise FulltextResolutionError("browser did not expose a direct PDF URL")


async def _browser_pdf_hrefs(page: Any) -> list[str]:
    raw = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href], area[href], link[href]'))
            .map((node) => ({
              href: node.href || node.getAttribute('href') || '',
              text: [
                node.textContent || '',
                node.getAttribute('title') || '',
                node.getAttribute('aria-label') || '',
                node.getAttribute('type') || '',
                node.getAttribute('rel') || ''
              ].join(' ')
            }))""",
    )
    if not isinstance(raw, list):
        return []
    hrefs: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "")
        text = str(item.get("text") or "")
        if href and (_looks_like_pdf_url(href) or _mentions_pdf(text)):
            hrefs.append(href)
    return hrefs


async def _browser_pdf_button_selectors(page: Any) -> list[str]:
    raw = await page.evaluate(
        """() => Array.from(document.querySelectorAll('button, [role="button"]'))
            .filter((node) => {
              const text = [
                node.textContent || '',
                node.getAttribute('title') || '',
                node.getAttribute('aria-label') || ''
              ].join(' ');
              return /pdf|全文|下载/i.test(text);
            })
            .slice(0, 5)
            .map((node, index) => {
              const value = `autoessay-resolver-${index}`;
              node.setAttribute('data-autoessay-resolver-index', value);
              return `[data-autoessay-resolver-index="${value}"]`;
            })""",
    )
    return [str(item) for item in raw] if isinstance(raw, list) else []


async def _first_browser_pdf_event(
    *,
    response_task: asyncio.Task[Any],
    download_task: asyncio.Task[Any],
    timeout: float,
) -> Any | None:
    pending = [task for task in (response_task, download_task) if not task.done()]
    if pending:
        done, _ = await asyncio.wait(
            pending,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    else:
        done = {response_task, download_task}
    for task in done:
        try:
            return task.result()
        except Exception:
            continue
    return None


def _url_from_browser_event(event: Any) -> str | None:
    raw = getattr(event, "url", None)
    if isinstance(raw, str) and raw:
        return raw
    try:
        raw = event.url
    except Exception:
        return None
    return raw if isinstance(raw, str) and raw else None


def _looks_like_pdf_response(response: Any) -> bool:
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).lower()
    return "pdf" in content_type


class _PdfLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._links: list[dict[str, str]] = []
        self._active_anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag in {"meta", "link"}:
            self._collect_meta_or_link(tag, attr_map)
            return
        if tag == "a":
            href = attr_map.get("href", "")
            if href:
                self._active_anchor = {
                    "href": href,
                    "text": " ".join(
                        value
                        for key in ("title", "aria-label", "download", "type")
                        if (value := attr_map.get(key, ""))
                    ),
                }
            return
        if tag == "button":
            button_href = _button_url(attr_map)
            if button_href:
                self._links.append(
                    {
                        "href": button_href,
                        "text": " ".join(attr_map.values()),
                        "method": "html_button_attr",
                    },
                )

    def handle_data(self, data: str) -> None:
        if self._active_anchor is not None:
            self._active_anchor["text"] = f"{self._active_anchor.get('text', '')} {data}".strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_anchor is not None:
            self._links.append({**self._active_anchor, "method": "html_anchor"})
            self._active_anchor = None

    def links(self, base_url: str) -> list[_ResolvedLink]:
        ranked: list[tuple[int, _ResolvedLink]] = []
        for item in self._links:
            href = item.get("href", "")
            if not href:
                continue
            text = item.get("text", "")
            absolute_url = urljoin(base_url, href)
            if not (_looks_like_pdf_url(absolute_url) or _mentions_pdf(text)):
                continue
            score = 0
            if item.get("method") in {"html_meta", "html_link"}:
                score += 10
            if _looks_like_pdf_url(absolute_url):
                score += 5
            if _mentions_pdf(text):
                score += 3
            ranked.append(
                (
                    -score,
                    _ResolvedLink(
                        absolute_url,
                        str(item.get("method") or "html_link"),
                        text[:120],
                    ),
                ),
            )
        return [link for _score, link in sorted(ranked, key=lambda pair: pair[0])]

    def _collect_meta_or_link(self, tag: str, attrs: dict[str, str]) -> None:
        href = attrs.get("href", "")
        content = attrs.get("content", "")
        label = " ".join(
            attrs.get(key, "") for key in ("name", "property", "rel", "type", "title", "aria-label")
        )
        if (
            tag == "meta"
            and content
            and ("citation_pdf_url" in label.casefold() or _looks_like_pdf_url(content))
        ):
            self._links.append({"href": content, "text": label, "method": "html_meta"})
        if tag == "link" and href and ("pdf" in label.casefold() or _looks_like_pdf_url(href)):
            self._links.append({"href": href, "text": label, "method": "html_link"})


def _button_url(attrs: dict[str, str]) -> str | None:
    for key in ("href", "data-href", "data-url", "data-pdf-url", "data-download-url"):
        value = attrs.get(key)
        if value:
            return value
    onclick = attrs.get("onclick", "")
    match = re.search(r"""(?:location(?:\.href)?|window\.open)\(['"]([^'"]+)['"]""", onclick)
    if match:
        return match.group(1)
    return None


def _dedupe_candidates(
    candidates: Sequence[FulltextResolutionCandidate],
) -> list[FulltextResolutionCandidate]:
    deduped: list[FulltextResolutionCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = candidate.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(FulltextResolutionCandidate(url=url, kind=candidate.kind))
    return deduped


def _is_safe_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not _is_login_url(url)


def _is_safe_pdf_target(url: str) -> bool:
    return _is_safe_http_url(url) and not _is_login_url(url)


def _is_login_url(url: str) -> bool:
    folded = url.casefold()
    return any(term in folded for term in LOGIN_URL_TERMS)


def _looks_like_pdf_url(url: str) -> bool:
    folded = url.casefold()
    return ".pdf" in folded or "pdf" in urlparse(url).path.casefold()


def _mentions_pdf(text: str) -> bool:
    folded = re.sub(r"\s+", " ", text.casefold())
    return any(term in folded for term in PDF_TEXT_TERMS)


def _limited_text(data: bytes, max_bytes: int, encoding: str) -> str:
    if len(data) > max_bytes:
        raise FulltextResolutionError("landing page too large")
    return data.decode(encoding, errors="replace")


def _too_large(content_length: str, max_bytes: int) -> bool:
    try:
        return int(content_length) > max_bytes
    except ValueError:
        return False


def _diag(
    candidate: FulltextResolutionCandidate,
    status: str,
    message: str,
    *,
    final_url: str | None = None,
    pdf_url: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_url": candidate.url,
        "candidate_type": candidate.kind,
        "status": status,
        "message": message,
    }
    if final_url:
        payload["final_url"] = final_url
    if pdf_url:
        payload["pdf_url"] = pdf_url
    return payload


def _failure_summary(diagnostics: Iterable[dict[str, object]]) -> str:
    messages = [str(item.get("message")) for item in diagnostics if item.get("message")]
    return "; ".join(messages[-3:]) or "no PDF URL resolved"
