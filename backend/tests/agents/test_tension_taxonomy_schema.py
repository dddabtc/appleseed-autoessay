"""PR-C3.a — TensionExtractionOutput / TensionEntry / TensionPole /
ClaimRef Pydantic schema tests + validate_claim_refs_against_synthesizer
gate.

Covers all codex round-1 + round-2 amendments:
  * 9 TensionClass enum (codex round-2 locked: drop etic_vs_emic,
    add normative_vs_descriptive)
  * tension_id ``^t\\d{3}$`` pattern + uniqueness (round-2 #1)
  * ClaimRef triple (track, source_id, claim_id), track ∈ 4 known
    synthesizer tracks (round-2 #1)
  * Each tension exactly 2 poles, distinct labels
  * boundary_fields flat dict[str, str], ≤8 keys, ≤80-char values
  * discipline_subtype normalized optional string (round-2 #5)
  * post-LLM gate validate_claim_refs_against_synthesizer drops refs
    not present in synthesizer artifact
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from autoessay.agents._tension_taxonomy import (
    SYNTHESIZER_TRACKS,
    TENSION_BOUNDARY_FIELDS_MAX_KEYS,
    TENSION_BOUNDARY_FIELDS_RECOMMENDED,
    TENSION_EXTRACTION_MAX_TENSIONS,
    ClaimRef,
    TensionClass,
    TensionEntry,
    TensionExtractionOutput,
    boundary_recommended_keys,
    class_label,
    validate_claim_refs_against_synthesizer,
)


def _ref(track: str = "primary_track", sid: str = "src1", cid: str = "c1") -> dict:
    return {"track": track, "source_id": sid, "claim_id": cid}


def _pole(label: str, refs: list[dict] | None = None) -> dict:
    return {"label": label, "claim_refs": refs or [_ref()]}


def _entry(**overrides) -> dict:
    payload = {
        "tension_id": "t001",
        "class_id": "continuity_vs_rupture",
        "summary": "Whether late-Qing Jiangnan imprints span Tongzhi-Guangxu rupture or not",
        "poles": [
            _pole("continuity", [_ref(track="secondary_track", sid="src_a", cid="c_a")]),
            _pole("rupture", [_ref(track="primary_track", sid="src_b", cid="c_b")]),
        ],
        "boundary_fields": {"period_boundaries": "1860-1900"},
    }
    payload.update(overrides)
    return payload


# ---------- TensionClass enum ---------------------------------------


def test_tension_class_has_exactly_nine_members() -> None:
    assert len(list(TensionClass)) == 9


def test_tension_class_round2_locked_set() -> None:
    members = {c.value for c in TensionClass}
    assert members == {
        "evidence_vs_theory",
        "synchronic_vs_diachronic",
        "material_vs_ideational",
        "particular_vs_general",
        "continuity_vs_rupture",
        "agency_vs_structure",
        "macro_vs_micro",
        "center_vs_periphery",
        "normative_vs_descriptive",
    }


def test_tension_class_no_etic_vs_emic() -> None:
    """Codex round-2 #1 dropped etic_vs_emic from top-level (folds
    into discipline_subtype if needed)."""
    assert "etic_vs_emic" not in {c.value for c in TensionClass}


def test_each_class_has_recommended_boundary_keys() -> None:
    for cls in TensionClass:
        keys = TENSION_BOUNDARY_FIELDS_RECOMMENDED[cls]
        assert len(keys) >= 1
        assert all(isinstance(k, str) for k in keys)


# ---------- ClaimRef ------------------------------------------------


def test_claim_ref_accepts_known_tracks() -> None:
    for track in SYNTHESIZER_TRACKS:
        ref = ClaimRef(track=track, source_id="s", claim_id="c")
        assert ref.track == track


def test_claim_ref_rejects_unknown_track() -> None:
    with pytest.raises(ValidationError):
        ClaimRef(track="evidence_ledger", source_id="s", claim_id="c")
    with pytest.raises(ValidationError):
        ClaimRef(track="not_a_track", source_id="s", claim_id="c")


# ---------- TensionEntry --------------------------------------------


def test_tension_entry_accepts_valid_payload() -> None:
    entry = TensionEntry.parse_obj(_entry())
    assert entry.tension_id == "t001"
    assert entry.class_id is TensionClass.CONTINUITY_VS_RUPTURE
    assert len(entry.poles) == 2


def test_tension_id_must_match_pattern() -> None:
    for bad in ("t1", "T001", "t0001", "tension_001", "001"):
        with pytest.raises(ValidationError):
            TensionEntry.parse_obj(_entry(tension_id=bad))


def test_tension_must_have_exactly_two_poles() -> None:
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(
            _entry(poles=[_pole("only_one")]),
        )
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(
            _entry(poles=[_pole("a"), _pole("b"), _pole("c")]),
        )


def test_tension_poles_must_have_distinct_labels() -> None:
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(
            _entry(poles=[_pole("same"), _pole("Same")]),
        )


def test_tension_summary_capped_at_200_chars() -> None:
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(_entry(summary="x" * 201))


def test_boundary_fields_capped_at_8_keys() -> None:
    too_many = {f"k{i}": "v" for i in range(TENSION_BOUNDARY_FIELDS_MAX_KEYS + 1)}
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(_entry(boundary_fields=too_many))


def test_boundary_fields_value_capped_at_80_chars() -> None:
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(_entry(boundary_fields={"k": "x" * 81}))


def test_discipline_subtype_normalizes_empty_to_none() -> None:
    entry = TensionEntry.parse_obj(_entry(discipline_subtype="   "))
    assert entry.discipline_subtype is None


def test_discipline_subtype_preserved_when_non_empty() -> None:
    entry = TensionEntry.parse_obj(_entry(discipline_subtype="modern_chinese_history"))
    assert entry.discipline_subtype == "modern_chinese_history"


def test_class_id_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(_entry(class_id="etic_vs_emic"))
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(_entry(class_id="not_a_class"))


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        TensionEntry.parse_obj(_entry(extra_field="x"))


# ---------- TensionExtractionOutput --------------------------------


def _output(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "extracted_at": "2026-05-04T11:30:00Z",
        "paper_mode": "case_analysis",
        "tensions": [_entry()],
    }
    payload.update(overrides)
    return payload


def test_extraction_output_accepts_minimal_payload() -> None:
    out = TensionExtractionOutput.parse_obj(_output())
    assert len(out.tensions) == 1


def test_extraction_output_caps_tension_count() -> None:
    too_many = [_entry(tension_id=f"t{i:03d}") for i in range(TENSION_EXTRACTION_MAX_TENSIONS + 1)]
    with pytest.raises(ValidationError):
        TensionExtractionOutput.parse_obj(_output(tensions=too_many))


def test_extraction_output_rejects_duplicate_tension_ids() -> None:
    dup = [_entry(tension_id="t001"), _entry(tension_id="t001")]
    with pytest.raises(ValidationError):
        TensionExtractionOutput.parse_obj(_output(tensions=dup))


# ---------- class_label / boundary_recommended_keys ----------------


def test_class_label_returns_zh_for_zh_locale() -> None:
    assert "延续" in class_label(TensionClass.CONTINUITY_VS_RUPTURE, "zh")


def test_class_label_returns_en_for_en_locale() -> None:
    assert "continuity" in class_label(TensionClass.CONTINUITY_VS_RUPTURE, "en")


def test_class_label_falls_back_to_en_for_unknown_locale() -> None:
    assert class_label(TensionClass.MACRO_VS_MICRO, "ja").startswith("macro")


def test_boundary_recommended_keys_returns_tuple() -> None:
    keys = boundary_recommended_keys(TensionClass.CONTINUITY_VS_RUPTURE)
    assert keys == ("period_boundaries", "transition_marker")


# ---------- validate_claim_refs_against_synthesizer ---------------


def _synth_payload() -> dict:
    return {
        "schema_version": 1,
        "primary_track": [
            {"source_id": "src_b", "claim_id": "c_b", "text": "primary claim"},
        ],
        "secondary_track": [
            {"source_id": "src_a", "claim_id": "c_a", "text": "secondary claim"},
        ],
        "theoretical_lens_track": [],
        "methodological_track": [],
    }


def test_validate_claim_refs_passes_when_all_present() -> None:
    out = TensionExtractionOutput.parse_obj(_output())
    drops = validate_claim_refs_against_synthesizer(out, _synth_payload())
    assert drops == []


def test_validate_claim_refs_drops_missing_triples() -> None:
    out = TensionExtractionOutput.parse_obj(
        _output(
            tensions=[
                _entry(
                    poles=[
                        _pole(
                            "continuity", [_ref(track="secondary_track", sid="src_a", cid="c_a")]
                        ),
                        _pole(
                            "rupture",
                            [_ref(track="primary_track", sid="src_NOT_PRESENT", cid="c_x")],
                        ),
                    ],
                ),
            ],
        ),
    )
    drops = validate_claim_refs_against_synthesizer(out, _synth_payload())
    assert len(drops) == 1
    assert drops[0]["source_id"] == "src_NOT_PRESENT"
    assert drops[0]["reason"] == "claim_ref_not_in_synthesizer"


def test_validate_claim_refs_track_mismatch_dropped() -> None:
    """source_id + claim_id present in synthesizer BUT under a
    different track → still dropped (codex round-2 #1: full triple
    must match)."""
    out = TensionExtractionOutput.parse_obj(
        _output(
            tensions=[
                _entry(
                    poles=[
                        # src_a + c_a IS in synthesizer.secondary_track
                        # but the ref claims primary_track → drop.
                        _pole(
                            "continuity",
                            [_ref(track="primary_track", sid="src_a", cid="c_a")],
                        ),
                        _pole("rupture", [_ref(track="primary_track", sid="src_b", cid="c_b")]),
                    ],
                ),
            ],
        ),
    )
    drops = validate_claim_refs_against_synthesizer(out, _synth_payload())
    assert len(drops) == 1
    assert drops[0]["track"] == "primary_track"
    assert drops[0]["source_id"] == "src_a"
