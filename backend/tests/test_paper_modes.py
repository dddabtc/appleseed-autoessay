"""Tests for the paper-mode registry (PR-C0)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.paper_modes import (
    DEFAULT_MODE_ID,
    REGISTRY_VERSION,
    ModeNotAvailableError,
    PaperModeSpec,
    all_modes,
    assert_mode_creatable,
    get_mode,
    is_mode_id_known,
    serialize_for_api,
)


def test_registry_has_six_modes() -> None:
    modes = all_modes()
    assert len(modes) == 6
    ids = {spec.mode_id for spec in modes}
    assert ids == {
        "case_analysis",
        "empirical",
        "theory_article",
        "comparative_study",
        "review_article",
        "theory_review",
    }


def test_default_mode_is_case_analysis() -> None:
    """case_analysis is the only `available` mode at C0 ship time."""
    assert DEFAULT_MODE_ID == "case_analysis"
    spec = get_mode(DEFAULT_MODE_ID)
    assert spec is not None
    assert spec.status == "available"


def test_status_distribution_at_c0_ship() -> None:
    """Lock the status of each mode. Future PRs that promote modes
    should update this test deliberately.

    PR-C2.b Tier 4 (2026-05-03): theory_article promoted from
    coming_soon → developer_preview now that framework_lens phase
    is implemented (PR #145, #151) and lens artifact display
    + ideator referential integrity (PR #156, #157) are in. Drafter
    section_plan now consults paper_modes registry too. Mode stays
    in developer_preview (not "available") to soak the new theory
    section plan before broad release.
    """
    expected = {
        "case_analysis": "available",
        "empirical": "developer_preview",  # promoted to available in PR-C1
        "theory_article": "developer_preview",  # PR-C2 promoted from coming_soon
        "comparative_study": "coming_soon",  # promoted in PR-C4
        "review_article": "coming_soon",  # promoted in PR-C5
        "theory_review": "coming_soon",  # promoted in PR-C5b
    }
    for mode_id, status in expected.items():
        spec = get_mode(mode_id)
        assert spec is not None, mode_id
        assert spec.status == status, f"{mode_id} status drift: {spec.status} != {status}"


def test_ordering_available_first_then_preview_then_coming_soon() -> None:
    """all_modes() returns specs sorted by status tier."""
    modes = all_modes()
    statuses = [spec.status for spec in modes]
    # All `available` come before all `developer_preview` come before
    # all `coming_soon`.
    seen_preview = False
    seen_coming = False
    for status in statuses:
        if status == "available":
            assert not seen_preview and not seen_coming
        elif status == "developer_preview":
            seen_preview = True
            assert not seen_coming
        elif status == "coming_soon":
            seen_coming = True


def test_theory_article_disallows_empirical_chapters() -> None:
    spec = get_mode("theory_article")
    assert spec is not None
    assert spec.permits_empirical_chapters is False
    # Section plan has NO empirical_section_*
    assert not any("empirical_section" in s for s in spec.drafter_section_plan)


def test_review_article_disallows_empirical_chapters() -> None:
    spec = get_mode("review_article")
    assert spec is not None
    assert spec.permits_empirical_chapters is False


def test_empirical_requires_primary_material() -> None:
    spec = get_mode("empirical")
    assert spec is not None
    assert spec.primary_material_required is True


def test_case_analysis_does_not_require_primary() -> None:
    """case_analysis is the safe default precisely because it doesn't
    require primary material — covers users without ingestible
    archives."""
    spec = get_mode("case_analysis")
    assert spec is not None
    assert spec.primary_material_required is False


def test_get_mode_returns_none_for_unknown() -> None:
    assert get_mode("not_a_real_mode") is None
    assert is_mode_id_known("not_a_real_mode") is False
    assert is_mode_id_known("case_analysis") is True


def test_assert_mode_creatable_accepts_available() -> None:
    spec = assert_mode_creatable("case_analysis")
    assert isinstance(spec, PaperModeSpec)
    assert spec.mode_id == "case_analysis"


def test_assert_mode_creatable_rejects_coming_soon() -> None:
    # PR-C2.b Tier 4: theory_article moved to developer_preview;
    # comparative_study still represents the coming_soon class.
    with pytest.raises(ModeNotAvailableError, match="coming_soon"):
        assert_mode_creatable("comparative_study")


def test_assert_mode_creatable_theory_article_now_developer_preview() -> None:
    # PR-C2.b Tier 4: theory_article was promoted from coming_soon
    # to developer_preview. Without ack it should reject as preview;
    # with ack it should accept.
    with pytest.raises(ModeNotAvailableError, match="developer_preview"):
        assert_mode_creatable("theory_article")
    spec = assert_mode_creatable("theory_article", accept_developer_preview=True)
    assert spec.mode_id == "theory_article"


def test_assert_mode_creatable_rejects_preview_without_ack() -> None:
    with pytest.raises(ModeNotAvailableError, match="developer_preview"):
        assert_mode_creatable("empirical")


def test_assert_mode_creatable_accepts_preview_with_ack() -> None:
    spec = assert_mode_creatable("empirical", accept_developer_preview=True)
    assert spec.mode_id == "empirical"


def test_assert_mode_creatable_rejects_unknown_mode() -> None:
    with pytest.raises(KeyError, match="not_a_real_mode"):
        assert_mode_creatable("not_a_real_mode")


def test_serialize_for_api_shape() -> None:
    payload = serialize_for_api()
    assert payload["registry_version"] == REGISTRY_VERSION
    assert payload["default_mode_id"] == DEFAULT_MODE_ID
    modes = payload["modes"]
    assert isinstance(modes, list)
    assert len(modes) == 6
    sample = modes[0]
    assert set(sample.keys()) == {
        "mode_id",
        "label_en",
        "label_zh",
        "label_ja",
        "description_en",
        "description_zh",
        "description_ja",
        "status",
        "requires_capability",
        "permits_empirical_chapters",
        "primary_material_required",
    }
    # First mode is the only `available` one.
    assert sample["status"] == "available"
    assert sample["mode_id"] == "case_analysis"


def test_serialize_for_api_requires_capability_is_list() -> None:
    """JSON-serializable: tuple → list at the boundary."""
    payload = serialize_for_api()
    for mode in payload["modes"]:
        assert isinstance(mode["requires_capability"], list)


def test_capability_constants_match_registry() -> None:
    """Each non-available mode references at least one capability flag.
    case_analysis is the only mode with empty requires_capability."""
    for spec in all_modes():
        if spec.mode_id == "case_analysis":
            assert spec.requires_capability == ()
        else:
            assert len(spec.requires_capability) >= 1, spec.mode_id


def test_every_mode_has_three_language_label_and_description() -> None:
    """ja was added in PR-C0.b2.tests so the JA UI can render mode
    pickers without falling back to en. All 6 modes must populate
    all three language variants."""
    for spec in all_modes():
        assert spec.label_en, spec.mode_id
        assert spec.label_zh, spec.mode_id
        assert spec.label_ja, spec.mode_id
        assert spec.description_en, spec.mode_id
        assert spec.description_zh, spec.mode_id
        assert spec.description_ja, spec.mode_id


# ---------------------------------------------------------------------------
# GET /api/paper_modes integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_modes_endpoint_returns_registry(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``GET /api/paper_modes`` serializes the registry. Cached at
    frontend init; backend re-evaluates only on process restart."""
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/paper_modes")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["registry_version"] == "v1"
    assert payload["default_mode_id"] == "case_analysis"
    assert len(payload["modes"]) == 6
    # Mode list ordering is stable: available first.
    assert payload["modes"][0]["mode_id"] == "case_analysis"
    assert payload["modes"][0]["status"] == "available"
