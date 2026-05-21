"""PR-263b — OpenLibrary client unit tests.

Validates the client's contract:
- ``_normalize_isbn`` handles 10/13-digit + Xx suffix + dashes
- response parser pulls title/authors/publisher/year reliably
- malformed responses return None instead of raising
- ``OpenLibraryClient.lookup_isbn`` injects an httpx mock and
  exercises happy-path + error branches
- ``metadata_to_normalized_source`` produces a well-formed
  ``NormalizedSource`` with the correct provenance + risk_flags
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from autoessay.clients.common import AccessStatus
from autoessay.clients.openlibrary import (
    OPENLIBRARY_BOOKS_URL,
    OpenLibraryBookMetadata,
    OpenLibraryClient,
    _extract_authors,
    _extract_publish_year,
    _extract_publisher,
    _normalize_isbn,
    metadata_to_normalized_source,
    parse_openlibrary_response,
)

# ----- _normalize_isbn --------------------------------------------


def test_normalize_isbn13_digits_only_passthrough() -> None:
    assert _normalize_isbn("9787101048420") == "9787101048420"


def test_normalize_isbn13_with_dashes_strips_them() -> None:
    assert _normalize_isbn("978-7-101-04842-0") == "9787101048420"


def test_normalize_isbn10_with_x_suffix_uppercases() -> None:
    assert _normalize_isbn("0-306-40615-x") == "030640615X"


def test_normalize_isbn_rejects_wrong_length() -> None:
    assert _normalize_isbn("123") is None
    assert _normalize_isbn("12345678901234567") is None


def test_normalize_isbn_rejects_isbn13_with_x() -> None:
    """ISBN-13 cannot have ``X`` check digit (only ISBN-10 can)."""
    assert _normalize_isbn("978710104842X") is None


def test_normalize_isbn_empty_returns_none() -> None:
    assert _normalize_isbn("") is None


# ----- response field extractors ----------------------------------


def test_extract_authors_from_structured_field() -> None:
    record = {"authors": [{"name": "郑振铎"}, {"name": "Joe Smith"}]}
    assert _extract_authors(record) == ["郑振铎", "Joe Smith"]


def test_extract_authors_falls_back_to_by_statement() -> None:
    record = {"by_statement": "by 王重民 and others"}
    assert _extract_authors(record) == ["by 王重民 and others"]


def test_extract_authors_drops_malformed_entries() -> None:
    """Authors list with non-dict entries / missing names should
    skip those entries silently."""
    record = {"authors": [{"name": "Valid"}, "not a dict", {"key": "no name"}]}
    assert _extract_authors(record) == ["Valid"]


def test_extract_authors_empty_returns_empty_list() -> None:
    assert _extract_authors({}) == []
    assert _extract_authors({"authors": []}) == []


def test_extract_publisher_from_first_publisher() -> None:
    record = {"publishers": [{"name": "中华书局"}, {"name": "Other"}]}
    assert _extract_publisher(record) == "中华书局"


def test_extract_publisher_missing_returns_none() -> None:
    assert _extract_publisher({}) is None
    assert _extract_publisher({"publishers": []}) is None


def test_extract_publish_year_from_pure_year() -> None:
    assert _extract_publish_year({"publish_date": "2011"}) == 2011


def test_extract_publish_year_from_full_date() -> None:
    assert _extract_publish_year({"publish_date": "2011-05-01"}) == 2011


def test_extract_publish_year_from_natural_language() -> None:
    assert _extract_publish_year({"publish_date": "May 2011"}) == 2011


def test_extract_publish_year_no_year_returns_none() -> None:
    assert _extract_publish_year({"publish_date": "无年代"}) is None
    assert _extract_publish_year({}) is None


# ----- parse_openlibrary_response ---------------------------------


def test_parse_full_record_returns_metadata() -> None:
    payload = {
        "ISBN:9787101048420": {
            "title": "中国俗文学史",
            "authors": [{"name": "郑振铎"}],
            "publishers": [{"name": "中华书局"}],
            "publish_date": "2011",
            "url": "https://openlibrary.org/books/OL12345M/zh",
        },
    }
    out = parse_openlibrary_response("9787101048420", payload)
    assert out is not None
    assert out.title == "中国俗文学史"
    assert out.authors == ["郑振铎"]
    assert out.publisher == "中华书局"
    assert out.publish_year == 2011
    assert out.url == "https://openlibrary.org/books/OL12345M/zh"


def test_parse_missing_isbn_key_returns_none() -> None:
    payload = {"ISBN:9999999999999": {"title": "X"}}
    assert parse_openlibrary_response("9787101048420", payload) is None


def test_parse_missing_title_returns_none() -> None:
    """A record with no title can't be cited — drop it."""
    payload = {
        "ISBN:9787101048420": {
            "authors": [{"name": "郑振铎"}],
        },
    }
    assert parse_openlibrary_response("9787101048420", payload) is None


def test_parse_empty_payload_returns_none() -> None:
    assert parse_openlibrary_response("9787101048420", {}) is None


# ----- OpenLibraryClient.lookup_isbn ------------------------------


def _build_mock_transport(
    responses: dict[str, tuple[int, dict[str, Any] | str]],
) -> httpx.MockTransport:
    """Build an httpx MockTransport that returns the configured
    response for each ISBN. Key = full querystring fragment
    ``ISBN:9787101048420``."""

    def handler(request: httpx.Request) -> httpx.Response:
        bibkeys = request.url.params.get("bibkeys", "")
        status, body = responses.get(bibkeys, (404, {}))
        if isinstance(body, dict):
            return httpx.Response(status_code=status, json=body)
        return httpx.Response(status_code=status, text=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_lookup_isbn_happy_path_returns_metadata() -> None:
    transport = _build_mock_transport(
        {
            "ISBN:9787101048420": (
                200,
                {
                    "ISBN:9787101048420": {
                        "title": "中国俗文学史",
                        "authors": [{"name": "郑振铎"}],
                        "publishers": [{"name": "中华书局"}],
                        "publish_date": "2011",
                    },
                },
            ),
        },
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        out = await client.lookup_isbn("978-7-101-04842-0")
    assert out is not None
    assert out.title == "中国俗文学史"
    assert out.authors == ["郑振铎"]


@pytest.mark.asyncio
async def test_lookup_isbn_malformed_input_returns_none_no_request() -> None:
    """Bad ISBN should never hit the network."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        out = await client.lookup_isbn("not-an-isbn")
    assert out is None
    assert call_count == 0


@pytest.mark.asyncio
async def test_lookup_isbn_404_returns_none() -> None:
    transport = _build_mock_transport(
        {"ISBN:9787101048420": (404, {})},
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        out = await client.lookup_isbn("9787101048420")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_isbn_invalid_json_returns_none() -> None:
    transport = _build_mock_transport(
        {"ISBN:9787101048420": (200, "not json {{{")},
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        out = await client.lookup_isbn("9787101048420")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_isbn_empty_payload_returns_none() -> None:
    """OpenLibrary returns ``{}`` when the ISBN is unknown — caller
    should see ``None`` not raise."""
    transport = _build_mock_transport(
        {"ISBN:9787101048420": (200, {})},
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        out = await client.lookup_isbn("9787101048420")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_isbn_network_error_returns_none() -> None:
    """When the transport raises an httpx error (timeout / connect
    failure), the lookup must return None instead of bubbling."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated timeout")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        out = await client.lookup_isbn("9787101048420")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_isbn_uses_correct_url_and_params() -> None:
    """Spot-check: the request goes to the books endpoint with the
    ``bibkeys=ISBN:{isbn}&format=json&jscmd=data`` triple."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url).split("?")[0]
        seen["bibkeys"] = request.url.params.get("bibkeys")
        seen["format"] = request.url.params.get("format")
        seen["jscmd"] = request.url.params.get("jscmd")
        return httpx.Response(
            200,
            json={
                "ISBN:9787101048420": {
                    "title": "T",
                    "authors": [{"name": "A"}],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenLibraryClient(http_client=http)
        await client.lookup_isbn("9787101048420")
    assert seen["url"] == OPENLIBRARY_BOOKS_URL
    assert seen["bibkeys"] == "ISBN:9787101048420"
    assert seen["format"] == "json"
    assert seen["jscmd"] == "data"


# ----- metadata_to_normalized_source ------------------------------


def test_normalized_source_carries_metadata_only_flags() -> None:
    md = OpenLibraryBookMetadata(
        isbn="9787101048420",
        title="中国俗文学史",
        authors=["郑振铎"],
        publisher="中华书局",
        publish_year=2011,
        url=None,
    )
    src = metadata_to_normalized_source(md)
    assert src.source_id == "openlibrary:isbn-9787101048420"
    assert src.title == "中国俗文学史"
    assert src.authors == ["郑振铎"]
    assert src.year == 2011
    assert src.venue == "中华书局"
    assert src.access_status == AccessStatus.METADATA_ONLY
    assert src.source_client == "openlibrary"
    assert src.verified_by == "openlibrary"
    assert src.provenance == "llm_canon"
    assert "metadata_only_no_full_text" in src.risk_flags
    assert src.doi is None
    assert src.pdf_url is None
    assert src.abstract is None


def test_normalized_source_passes_through_canonical_metadata() -> None:
    """When the caller supplies bucket / rationale (e.g. from the
    shadow_baseline rationale), they show up on the output."""
    md = OpenLibraryBookMetadata(
        isbn="9787101048420",
        title="X",
        authors=["A"],
        publisher=None,
        publish_year=None,
        url=None,
    )
    src = metadata_to_normalized_source(
        md,
        canonical_bucket="frontier",
        canonical_rationale="shadow baseline classic ref",
    )
    assert src.canonical_bucket == "frontier"
    assert src.canonical_rationale == "shadow baseline classic ref"
