"""Unit tests for stop-slop scoring.

These tests must be self-contained — they cannot depend on the stop-slop
rule bundle being present on disk, because CI runs pytest before the
docker image build that fetches the bundle. So we pass an explicit
phrases fixture and an empty structures list (structure-pattern findings
are hardcoded inside score.py and do not depend on the structures arg).
"""

from autoessay.config import get_settings
from autoessay.stop_slop.score import score_text


def test_score_flags_banned_phrases_binary_contrast_and_em_dash_overuse(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()

    # Self-contained fixture: do not depend on stop-slop bundle on disk.
    phrases = {"stakes are high"}
    structures: list = []

    text = (
        "It's not just archival scarcity, it's institutional design — and this matters "
        "because the stakes are high — full stop."
    )

    score = score_text(text, phrases, structures)

    finding_types = {str(finding["type"]) for finding in score["findings"]}  # type: ignore[index]
    assert score["total"] < 35
    assert "not_just_form" in finding_types
    assert "binary_contrast" in finding_types
    assert "em_dash_overuse" in finding_types
    assert "banned_phrase" in finding_types
