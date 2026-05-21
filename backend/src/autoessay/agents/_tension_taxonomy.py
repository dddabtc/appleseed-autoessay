"""PR-C3.a — 9-class tension taxonomy + Pydantic schema for the
tension_extraction phase.

The taxonomy is the spine of "5 类创新源" #5 ("新论证") — humanities-journal
tensions that the extraction agent identifies in the dual-track
synthesizer output, classifies, and grounds in `synthesizer.json`'s
4-track claim corpus. The drafter consumes these as scaffolding metadata
(codex round-1 #6 + round-2 amendment 6 — taxonomy class label NEVER
appears in manuscript body; "engage the underlying tension and
boundary" is the rubric).

9 classes locked by codex round-2 (round-1 had `etic_vs_emic`; codex
DROP from top-level → folded into ``discipline_subtype`` at most;
ADD ``normative_vs_descriptive`` for theory/political/philosophy/
humanities common 应然 vs 实然 tension):

    evidence_vs_theory       (renamed from empirical_vs_theoretical)
    synchronic_vs_diachronic
    material_vs_ideational   (renamed from material_vs_ideal)
    particular_vs_general    (renamed from local_vs_universal)
    continuity_vs_rupture
    agency_vs_structure
    macro_vs_micro
    center_vs_periphery
    normative_vs_descriptive (added; replaced etic_vs_emic)

Each class carries ``boundary_fields_recommended_keys`` metadata —
the LLM is asked to prefer these keys when filling out a tension's
``boundary_fields`` map but is allowed to add domain-specific keys
(it's a recommendation, not a rigid schema).

Codex round-2 amendment 1: ``class TensionClass(str, Enum)`` (Python
3.10 compat — StrEnum is 3.11+); ``tension_id`` format ``^t\\d{3}$``
+ uniqueness validator at the parent level; ``ClaimRef`` validates
the (track, source_id, claim_id) triple actually exists in the
synthesizer artifact (NOT just claim_id ∈ track) — the extraction
agent runs this gate post-LLM.

Codex round-2 amendment 2: tension owns
``synthesis/tension_extraction.json`` — does NOT mutate
``synthesizer.json``. Single-writer ownership means C3.a doesn't
need to bump synthesizer schema_version; readers join via
``ClaimRef`` triple lookup.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, root_validator, validator


class TensionClass(str, Enum):
    """9 humanities-journal tension classes (codex round-2 locked)."""

    EVIDENCE_VS_THEORY = "evidence_vs_theory"
    SYNCHRONIC_VS_DIACHRONIC = "synchronic_vs_diachronic"
    MATERIAL_VS_IDEATIONAL = "material_vs_ideational"
    PARTICULAR_VS_GENERAL = "particular_vs_general"
    CONTINUITY_VS_RUPTURE = "continuity_vs_rupture"
    AGENCY_VS_STRUCTURE = "agency_vs_structure"
    MACRO_VS_MICRO = "macro_vs_micro"
    CENTER_VS_PERIPHERY = "center_vs_periphery"
    NORMATIVE_VS_DESCRIPTIVE = "normative_vs_descriptive"


# Per-class recommended boundary_fields keys. Not a hard schema — the
# LLM extraction prompt encourages these keys but accepts domain-
# specific keys when the case demands. Codex round-1 #4: enforce flat
# dict[str, str] + max 8 keys + max 80-char value at the parent
# schema, NOT per-class enums.
TENSION_BOUNDARY_FIELDS_RECOMMENDED: dict[TensionClass, tuple[str, ...]] = {
    TensionClass.EVIDENCE_VS_THEORY: ("data_scope", "theory_axis"),
    TensionClass.SYNCHRONIC_VS_DIACHRONIC: ("time_window", "comparison_axis"),
    TensionClass.MATERIAL_VS_IDEATIONAL: ("causal_layer", "mechanism"),
    TensionClass.PARTICULAR_VS_GENERAL: ("case_scope", "generalization_claim"),
    TensionClass.CONTINUITY_VS_RUPTURE: ("period_boundaries", "transition_marker"),
    TensionClass.AGENCY_VS_STRUCTURE: ("actor_layer", "structural_layer"),
    TensionClass.MACRO_VS_MICRO: ("scale", "aggregation_level"),
    TensionClass.CENTER_VS_PERIPHERY: ("voice_position", "epistemic_authority"),
    TensionClass.NORMATIVE_VS_DESCRIPTIVE: ("normative_standard", "descriptive_claim"),
}


# Bilingual labels for UI (TensionSubview, drafter scaffold metadata).
# C3.b will render these in three locales; C3.a just registers them.
TENSION_LABELS_ZH: dict[TensionClass, str] = {
    TensionClass.EVIDENCE_VS_THEORY: "证据 vs 理论",
    TensionClass.SYNCHRONIC_VS_DIACHRONIC: "共时 vs 历时",
    TensionClass.MATERIAL_VS_IDEATIONAL: "物质 vs 观念",
    TensionClass.PARTICULAR_VS_GENERAL: "个案 vs 普遍",
    TensionClass.CONTINUITY_VS_RUPTURE: "延续 vs 断裂",
    TensionClass.AGENCY_VS_STRUCTURE: "主体 vs 结构",
    TensionClass.MACRO_VS_MICRO: "宏观 vs 微观",
    TensionClass.CENTER_VS_PERIPHERY: "中心 vs 边缘",
    TensionClass.NORMATIVE_VS_DESCRIPTIVE: "应然 vs 实然",
}
TENSION_LABELS_EN: dict[TensionClass, str] = {
    TensionClass.EVIDENCE_VS_THEORY: "evidence vs theory",
    TensionClass.SYNCHRONIC_VS_DIACHRONIC: "synchronic vs diachronic",
    TensionClass.MATERIAL_VS_IDEATIONAL: "material vs ideational",
    TensionClass.PARTICULAR_VS_GENERAL: "particular vs general",
    TensionClass.CONTINUITY_VS_RUPTURE: "continuity vs rupture",
    TensionClass.AGENCY_VS_STRUCTURE: "agency vs structure",
    TensionClass.MACRO_VS_MICRO: "macro vs micro",
    TensionClass.CENTER_VS_PERIPHERY: "center vs periphery",
    TensionClass.NORMATIVE_VS_DESCRIPTIVE: "normative vs descriptive",
}


# 4 synthesizer tracks the extraction agent's claim_refs may point at.
# Codex round-2 amendment 1: the (track, source_id, claim_id) triple
# is validated post-LLM by the agent against synthesizer.json — this
# enum is the authoritative track set. Synthesizer.py owns the actual
# layout; this lookup is for C3.a's gate alone.
SYNTHESIZER_TRACKS: tuple[str, ...] = (
    "primary_track",
    "secondary_track",
    "theoretical_lens_track",
    "methodological_track",
)


# tension_id pattern (codex round-2 amendment 1): ``t`` prefix + 3-digit
# zero-padded counter. Uniqueness checked at TensionExtractionOutput
# level.
TENSION_ID_PATTERN = re.compile(r"^t\d{3}$")


class ClaimRef(BaseModel):
    """A pointer into ``synthesis/synthesizer.json``'s 4-track claim
    corpus. Codex round-2 amendment 1: validate the full
    (track, source_id, claim_id) triple, not just claim_id ∈ track —
    the agent's post-LLM gate runs ``validate_claim_refs_against_synthesizer``
    against the actual artifact."""

    class Config:
        extra = "forbid"

    track: str = Field(..., description="One of SYNTHESIZER_TRACKS")
    source_id: str = Field(..., min_length=1, max_length=200)
    claim_id: str = Field(..., min_length=1, max_length=200)

    @validator("track")
    def _track_must_be_known(cls, value: str) -> str:
        if value not in SYNTHESIZER_TRACKS:
            raise ValueError(f"track must be one of {SYNTHESIZER_TRACKS}, got {value!r}")
        return value


class TensionPole(BaseModel):
    """One side of a 2-pole tension. Codex round-1 critical schema
    amendment: each tension has EXACTLY 2 poles (not just a single
    summary). Drafter scaffold uses these to point at which evidence
    grounds each side without resorting to the taxonomy class label."""

    class Config:
        extra = "forbid"

    label: str = Field(..., min_length=1, max_length=80)
    claim_refs: list[ClaimRef] = Field(..., min_items=1)


# Cap counts (codex round-1 #4 + round-2 amendment 1).
TENSION_BOUNDARY_FIELDS_MAX_KEYS = 8
TENSION_BOUNDARY_FIELD_VALUE_MAX_LEN = 80
TENSION_SUMMARY_MAX_LEN = 200
TENSION_DISCIPLINE_SUBTYPE_MAX_LEN = 80


class TensionEntry(BaseModel):
    class Config:
        extra = "forbid"

    tension_id: str = Field(..., description="t001, t002, ... pattern")
    class_id: TensionClass = Field(...)
    discipline_subtype: str | None = Field(
        default=None,
        max_length=TENSION_DISCIPLINE_SUBTYPE_MAX_LEN,
    )
    summary: str = Field(..., min_length=1, max_length=TENSION_SUMMARY_MAX_LEN)
    poles: list[TensionPole] = Field(..., description="Exactly 2 poles")
    boundary_fields: dict[str, str] = Field(
        default_factory=dict,
        description=f"Flat dict, ≤{TENSION_BOUNDARY_FIELDS_MAX_KEYS} keys",
    )
    research_role_align: str | None = Field(
        default=None,
        max_length=80,
    )

    @validator("tension_id")
    def _tension_id_pattern(cls, value: str) -> str:
        if not TENSION_ID_PATTERN.match(value):
            raise ValueError(f"tension_id must match {TENSION_ID_PATTERN.pattern!r}, got {value!r}")
        return value

    @validator("poles")
    def _exactly_two_poles(cls, value: list[TensionPole]) -> list[TensionPole]:
        if len(value) != 2:
            raise ValueError(f"tension must have exactly 2 poles, got {len(value)}")
        labels = {pole.label.strip().casefold() for pole in value}
        if len(labels) != 2:
            raise ValueError("tension poles must have distinct labels")
        return value

    @validator("boundary_fields")
    def _boundary_fields_size_caps(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > TENSION_BOUNDARY_FIELDS_MAX_KEYS:
            raise ValueError(
                f"boundary_fields has {len(value)} keys; max {TENSION_BOUNDARY_FIELDS_MAX_KEYS}"
            )
        for key, item_value in value.items():
            if not key or len(key) > 80:
                raise ValueError(f"boundary_fields key {key!r} length out of range")
            if not isinstance(item_value, str):
                raise TypeError(
                    f"boundary_fields[{key!r}] must be str, got {type(item_value).__name__}"
                )
            if len(item_value) > TENSION_BOUNDARY_FIELD_VALUE_MAX_LEN:
                raise ValueError(
                    f"boundary_fields[{key!r}] is {len(item_value)} chars; "
                    f"max {TENSION_BOUNDARY_FIELD_VALUE_MAX_LEN}"
                )
        return value

    @validator("discipline_subtype", pre=True)
    def _normalize_discipline_subtype(cls, value: object) -> str | None:
        # Codex round-2 amendment 5: discipline_subtype is a normalized
        # optional string, NOT an open enum. Empty / whitespace-only
        # strings collapse to None so downstream readers don't have to
        # branch on both shapes.
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("discipline_subtype must be str or None")
        text = value.strip()
        if not text:
            return None
        if len(text) > TENSION_DISCIPLINE_SUBTYPE_MAX_LEN:
            raise ValueError(
                f"discipline_subtype is {len(text)} chars; max {TENSION_DISCIPLINE_SUBTYPE_MAX_LEN}"
            )
        return text

    def boundary_fields_compact(self) -> dict[str, str]:
        """Compact-for-prompt accessor — returned as-is (already capped
        at construction time). Drafter / lens prompts inject this map
        when they consume tensions; the values are bounded so token
        budget stays predictable."""
        return dict(self.boundary_fields)


# Cap on number of tensions per extraction (codex round-1 §3.3 — too
# many dilute the manuscript's main argument; ≤8 is the soft target,
# extraction prompt asks for 3-5 for typical case_analysis runs).
TENSION_EXTRACTION_MAX_TENSIONS = 8


class TensionExtractionOutput(BaseModel):
    """Top-level artifact written to ``synthesis/tension_extraction.json``.

    Codex round-2 amendment 1: ``tension_id`` uniqueness enforced at
    the parent. Codex round-2 amendment 2: this is the SOLE writer of
    tension data — does NOT mutate ``synthesizer.json``."""

    class Config:
        extra = "forbid"

    schema_version: int = Field(default=1)
    extracted_at: str = Field(..., description="ISO-8601 UTC timestamp")
    paper_mode: str = Field(..., max_length=64)
    tensions: list[TensionEntry] = Field(
        ...,
        description=f"≤{TENSION_EXTRACTION_MAX_TENSIONS} tensions",
        max_items=TENSION_EXTRACTION_MAX_TENSIONS,
    )

    @root_validator(skip_on_failure=True)
    def _tension_ids_unique(cls, values: dict[str, Any]) -> dict[str, Any]:
        tensions = values.get("tensions") or []
        seen: set[str] = set()
        for tension in tensions:
            tid = getattr(tension, "tension_id", None)
            if tid is None:
                continue
            if tid in seen:
                raise ValueError(f"duplicate tension_id: {tid}")
            seen.add(tid)
        return values


def class_label(class_id: TensionClass, language: str) -> str:
    """Resolve a class_id → human label in ``zh`` / ``en`` /
    fallback to ``en``. Used by C3.b TensionSubview + drafter scaffold
    metadata (the scaffold metadata uses the label internally; the
    label still must NOT appear in manuscript body — codex round-1 #6
    DISAGREE-with-revisions)."""
    code = (language or "en").lower()
    if code.startswith("zh"):
        return TENSION_LABELS_ZH.get(class_id, class_id.value)
    return TENSION_LABELS_EN.get(class_id, class_id.value)


def boundary_recommended_keys(class_id: TensionClass) -> tuple[str, ...]:
    return TENSION_BOUNDARY_FIELDS_RECOMMENDED.get(class_id, ())


def validate_claim_refs_against_synthesizer(
    output: TensionExtractionOutput,
    synthesizer_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Codex round-2 amendment 1: post-LLM gate. Verify every
    ``ClaimRef`` triple (track, source_id, claim_id) actually exists
    in the synthesizer artifact's 4-track structure. Returns a list
    of drop warnings (empty list = all refs valid). Caller (the
    extraction agent) decides whether to fail the phase or surface
    these as warnings."""
    drops: list[dict[str, Any]] = []
    track_index: dict[tuple[str, str, str], bool] = {}
    for track_name in SYNTHESIZER_TRACKS:
        for claim in synthesizer_payload.get(track_name) or []:
            if not isinstance(claim, dict):
                continue
            sid = claim.get("source_id")
            cid = claim.get("claim_id")
            if isinstance(sid, str) and isinstance(cid, str):
                track_index[(track_name, sid, cid)] = True
    for tension in output.tensions:
        for pole in tension.poles:
            for ref in pole.claim_refs:
                key = (ref.track, ref.source_id, ref.claim_id)
                if key not in track_index:
                    drops.append(
                        {
                            "tension_id": tension.tension_id,
                            "pole_label": pole.label,
                            "track": ref.track,
                            "source_id": ref.source_id,
                            "claim_id": ref.claim_id,
                            "reason": "claim_ref_not_in_synthesizer",
                        },
                    )
    return drops


__all__ = [
    "ClaimRef",
    "SYNTHESIZER_TRACKS",
    "TENSION_BOUNDARY_FIELDS_MAX_KEYS",
    "TENSION_BOUNDARY_FIELD_VALUE_MAX_LEN",
    "TENSION_BOUNDARY_FIELDS_RECOMMENDED",
    "TENSION_EXTRACTION_MAX_TENSIONS",
    "TENSION_ID_PATTERN",
    "TENSION_LABELS_EN",
    "TENSION_LABELS_ZH",
    "TENSION_SUMMARY_MAX_LEN",
    "TensionClass",
    "TensionEntry",
    "TensionExtractionOutput",
    "TensionPole",
    "boundary_recommended_keys",
    "class_label",
    "validate_claim_refs_against_synthesizer",
]
