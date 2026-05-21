"""PR-C1.a: evidence ledger unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoessay import evidence_ledger as ledger


def test_compute_claim_id_is_deterministic() -> None:
    a = ledger.compute_claim_id("source_a", "claim text", "Smith 1990")
    b = ledger.compute_claim_id("source_a", "claim text", "Smith 1990")
    assert a == b
    assert len(a) == 16


def test_compute_claim_id_changes_with_any_field() -> None:
    base = ledger.compute_claim_id("source_a", "claim text", "Smith 1990")
    assert ledger.compute_claim_id("source_b", "claim text", "Smith 1990") != base
    assert ledger.compute_claim_id("source_a", "different", "Smith 1990") != base
    assert ledger.compute_claim_id("source_a", "claim text", "Jones 2010") != base


def test_compute_claim_id_normalizes_whitespace() -> None:
    a = ledger.compute_claim_id("source_a", "  claim text  ", "Smith 1990")
    b = ledger.compute_claim_id("source_a", "claim text", "Smith 1990")
    assert a == b


def test_claim_row_emits_kind_and_deterministic_id() -> None:
    row = ledger.claim_row(
        source_id="source_a",
        claim_text="The reform of 1898 reorganized the salt monopoly.",
        citation_target="archive_qing_001",
        confidence=0.85,
    )
    assert row["kind"] == "claim"
    assert row["source_id"] == "source_a"
    assert row["citation_target"] == "archive_qing_001"
    assert row["confidence"] == 0.85
    assert isinstance(row["claim_id"], str) and len(row["claim_id"]) == 16


def test_override_row_defaults_recorded_at() -> None:
    row = ledger.override_row(
        source_id="source_a",
        claim_id="abc",
        action="attribute_to_user",
        user="zhaodali78",
    )
    assert row["kind"] == "override"
    assert row["claim_id"] == "abc"
    assert row["recorded_at"]


def test_append_rows_creates_synthesis_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    rows = [
        ledger.claim_row(
            source_id="source_a",
            claim_text="x",
            citation_target="t",
            confidence=0.5,
        )
    ]
    written = ledger.append_rows(run_dir, rows)
    assert written == 1
    assert (run_dir / "synthesis" / "evidence_ledger.jsonl").exists()


def test_append_rows_is_idempotent_for_claim_kind(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    rows = [
        ledger.claim_row(
            source_id="source_a",
            claim_text="x",
            citation_target="t",
            confidence=0.5,
        )
    ]
    assert ledger.append_rows(run_dir, rows) == 1
    # Re-append same claim — should be skipped.
    assert ledger.append_rows(run_dir, rows) == 0
    # File still has just one row.
    contents = (run_dir / "synthesis" / "evidence_ledger.jsonl").read_text()
    assert contents.strip().count("\n") == 0


def test_append_rows_appends_overrides_unconditionally(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    o1 = ledger.override_row(
        source_id="src",
        claim_id="abc",
        action="attribute_to_user",
        user="u",
        recorded_at="2026-05-03T00:00:00+00:00",
    )
    o2 = ledger.override_row(
        source_id="src",
        claim_id="abc",
        action="cite_normally",
        user="u",
        recorded_at="2026-05-03T01:00:00+00:00",
    )
    assert ledger.append_rows(run_dir, [o1]) == 1
    # Same source_id + claim_id + action — but it's an override, so
    # idempotency does NOT apply. It's recorded with its own
    # recorded_at.
    assert ledger.append_rows(run_dir, [o2]) == 1


def test_read_rows_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert ledger.read_rows(tmp_path / "run_doesnt_exist") == []


def test_fold_overrides_keeps_latest_per_source_claim(tmp_path: Path) -> None:
    rows = [
        ledger.override_row(
            source_id="src1",
            claim_id="claim_a",
            action="attribute_to_user",
            user="u",
            recorded_at="2026-05-03T00:00:00+00:00",
        ),
        ledger.override_row(
            source_id="src1",
            claim_id="claim_a",
            action="cite_normally",
            user="u",
            recorded_at="2026-05-03T01:00:00+00:00",
        ),
        ledger.override_row(
            source_id="src1",
            claim_id=None,
            action="attribute_to_user",
            user="u",
            recorded_at="2026-05-03T00:30:00+00:00",
        ),
    ]
    folded = ledger.fold_overrides(rows)
    # Per-claim latest wins.
    assert folded[("src1", "claim_a")]["action"] == "cite_normally"
    # Source-wide override is keyed under (src1, None).
    assert folded[("src1", None)]["action"] == "attribute_to_user"


def test_existing_claim_ids_only_counts_claim_kind(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    rows = [
        ledger.claim_row(
            source_id="src",
            claim_text="t1",
            citation_target="x",
            confidence=0.5,
        ),
        ledger.override_row(
            source_id="src",
            claim_id="something",
            action="attribute_to_user",
            user="u",
        ),
        ledger.event_row(event_type="extraction_failed", payload={"src": "src"}),
    ]
    ledger.append_rows(run_dir, rows)
    ids = ledger.existing_claim_ids(run_dir)
    # Only the claim row's id is in the set.
    assert len(ids) == 1


def test_jsonl_file_is_valid_json_per_line(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    ledger.append_rows(
        run_dir,
        [
            ledger.claim_row(
                source_id="src",
                claim_text="alpha",
                citation_target="t",
                confidence=0.5,
            ),
            ledger.claim_row(
                source_id="src",
                claim_text="beta",
                citation_target="t",
                confidence=0.5,
            ),
        ],
    )
    text = (run_dir / "synthesis" / "evidence_ledger.jsonl").read_text(encoding="utf-8")
    for line in text.strip().split("\n"):
        # each line is valid JSON
        obj = json.loads(line)
        assert obj["kind"] == "claim"


@pytest.mark.parametrize(
    "kind, payload",
    [
        ("claim_row", {"text": "alpha"}),
        ("override_row", {"action": "attribute_to_user"}),
    ],
)
def test_row_factories_produce_kind_field(kind: str, payload: dict[str, str]) -> None:
    if kind == "claim_row":
        row = ledger.claim_row(
            source_id="x",
            claim_text=payload["text"],
            citation_target="y",
            confidence=0.5,
        )
        assert row["kind"] == "claim"
    elif kind == "override_row":
        row = ledger.override_row(
            source_id="x",
            claim_id=None,
            action=payload["action"],
            user="u",
        )
        assert row["kind"] == "override"
