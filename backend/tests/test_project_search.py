"""Tests for ?q= title search on /api/projects and /api/runs."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from autoessay.main import app


async def _create(client: AsyncClient, title: str) -> str:
    resp = await client.post(
        "/api/projects",
        json={"title": title, "domain_id": "financial_history", "language": "en"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_substring_match_returns_matches_only(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create(client, "Banking History 1900-1949")
        b = await _create(client, "Crop Yields and Climate")
        resp = await client.get("/api/projects?q=banking")
        ids = [p["id"] for p in resp.json()]
    assert a in ids
    assert b not in ids


async def test_search_is_case_insensitive(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create(client, "Banking History")
        resp = await client.get("/api/projects?q=BANK")
        ids = [p["id"] for p in resp.json()]
    assert pid in ids


async def test_like_wildcards_in_user_input_are_escaped(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """User-typed % must match a LITERAL percent, not act as a wildcard."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with_percent = await _create(client, "Inflation rose 50% in 1923")
        no_percent = await _create(client, "Inflation rose fifty in 1923")
        resp = await client.get("/api/projects?q=50%25")  # url-encoded %
        ids = [p["id"] for p in resp.json()]
    assert with_percent in ids
    assert no_percent not in ids


async def test_underscore_wildcard_is_escaped(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with_us = await _create(client, "case_study_a")
        without = await _create(client, "case study b")
        resp = await client.get("/api/projects?q=case_study")
        ids = [p["id"] for p in resp.json()]
    # The query "case_study" must match the LITERAL underscore — so
    # "case_study_a" matches but "case study b" does NOT (the unescaped
    # _ in raw LIKE would match the space).
    assert with_us in ids
    assert without not in ids


async def test_empty_q_returns_full_list(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create(client, "essay one")
        b = await _create(client, "essay two")
        resp_blank = await client.get("/api/projects?q=")
        resp_ws = await client.get("/api/projects?q=%20%20")
        resp_none = await client.get("/api/projects")
    for resp in (resp_blank, resp_ws, resp_none):
        ids = [p["id"] for p in resp.json()]
        assert a in ids
        assert b in ids


async def test_q_max_length_returns_422(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/projects?q={'x' * 201}")
    assert resp.status_code == 422


async def test_runs_endpoint_supports_q_via_project_title(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid_a = await _create(client, "Banking History")
        pid_b = await _create(client, "Crop Yields")
        run_a = await client.post(f"/api/projects/{pid_a}/runs")
        run_b = await client.post(f"/api/projects/{pid_b}/runs")
        run_a_id = run_a.json()["id"]
        run_b_id = run_b.json()["id"]
        resp = await client.get("/api/runs?q=banking")
        ids = [r["id"] for r in resp.json()]
    assert run_a_id in ids
    assert run_b_id not in ids
