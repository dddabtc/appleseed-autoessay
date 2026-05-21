"""PR-J9 v1: strict Pydantic output schemas for the LLM-driven
canonical / frontier literature mining surface.

Two LLM calls per scout run (codex round-1 amendment 3.1 — option B
chosen so canon mining and frontier mining can run in different
reasoning frames):

  call 1 — canon: 5 consensus + 3 disagreement axes (each with 1-2
                  representative_works); aimed at "established
                  scholarly canon".
  call 2 — frontier: 5 current hot directions (each with 1
                     representative_work); aimed at "active scholarly
                     directions in the past ~5-10 years".

Each work is a ``CanonicalWork`` (renamed from the original draft's
``CanonicalArticle`` per codex amendment 2 — humanities canon
includes monographs / book chapters as much as journal articles;
strict article-only would lose Amsden/Wade/Cumings-class books).

Schemas use ``extra="forbid"`` to make schema drift trigger
``run_llm_step``'s corrective-suffix retry. Per-field whitespace
normalize + blank reject keeps a leading-edge LLM that emits
all-whitespace strings from polluting the surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field, StrictStr, validator

# ----------------------------------------------------------------------
# Helpers — match the per-field normalization pattern from
# backend/src/autoessay/agents/_framework_lens_schema.py (PR-C2c).
# ----------------------------------------------------------------------


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _reject_blank(value: str, *, field_label: str) -> str:
    cleaned = _normalize_whitespace(value)
    if not cleaned:
        raise ValueError(f"{field_label} must be non-empty after whitespace normalize")
    return cleaned


# ----------------------------------------------------------------------
# CanonicalWork — single literature item the LLM names
# ----------------------------------------------------------------------


CanonicalBucket = Literal["consensus", "disagreement", "frontier"]


class CanonicalWork(BaseModel):
    """One LLM-named scholarly work. Verified by Crossref / OpenAlex
    roundtrip in ``_canonical_mining.verify_canonical_via_lit_clients``;
    works the verifier can't confirm with high confidence are dropped
    + audited as ``hallucinated_canonical``.
    """

    title: StrictStr = Field(min_length=4, max_length=400)
    first_author: StrictStr = Field(min_length=2, max_length=200)
    year: int | None = Field(default=None)
    doi: StrictStr | None = Field(default=None, min_length=4, max_length=200)
    journal_or_publisher: StrictStr | None = Field(default=None, max_length=300)
    rationale: StrictStr | None = Field(default=None, max_length=400)

    @validator("year")
    def _year_range(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1500 or value > 2100:
            raise ValueError("year must be between 1500 and 2100")
        return value

    @validator("title", "first_author")
    def _required_text_must_have_content(cls, value: str) -> str:
        return _reject_blank(value, field_label="text field")

    @validator("doi", "journal_or_publisher", "rationale")
    def _optional_text_normalize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _normalize_whitespace(value)
        return cleaned or None

    class Config:
        extra = "forbid"
        allow_mutation = False


# ----------------------------------------------------------------------
# Consensus / Disagreement / Frontier item shapes
# ----------------------------------------------------------------------


class ConsensusItem(BaseModel):
    """One major scholarly consensus statement on the topic, with
    1-2 most-cited / earliest / most-influential representative works."""

    statement: StrictStr = Field(min_length=10, max_length=600)
    representative_works: list[CanonicalWork] = Field(min_items=1, max_items=2)

    @validator("statement")
    def _statement_normalize(cls, value: str) -> str:
        return _reject_blank(value, field_label="statement")

    class Config:
        extra = "forbid"
        allow_mutation = False


class DisagreementItem(BaseModel):
    """One major scholarly disagreement axis on the topic, with one
    representative work from each side (so 2 works total)."""

    axis_description: StrictStr = Field(min_length=10, max_length=600)
    representative_works: list[CanonicalWork] = Field(min_items=2, max_items=2)

    @validator("axis_description")
    def _axis_normalize(cls, value: str) -> str:
        return _reject_blank(value, field_label="axis_description")

    class Config:
        extra = "forbid"
        allow_mutation = False


class FrontierItem(BaseModel):
    """One current frontier direction (≤5-10 years old), with the
    most influential recent work in that direction. NOT older canon —
    frontier emphasizes recent citation velocity, not absolute
    citation count."""

    direction: StrictStr = Field(min_length=10, max_length=600)
    representative_works: list[CanonicalWork] = Field(min_items=1, max_items=2)
    why_frontier: StrictStr = Field(min_length=10, max_length=400)

    @validator("direction", "why_frontier")
    def _text_normalize(cls, value: str) -> str:
        return _reject_blank(value, field_label="direction or why_frontier")

    class Config:
        extra = "forbid"
        allow_mutation = False


# ----------------------------------------------------------------------
# Top-level outputs (one per LLM call)
# ----------------------------------------------------------------------


class CanonicalSourcesOutput(BaseModel):
    """Output of LLM call 1 (canon mode): consensus + disagreement.
    Empty arrays acceptable for narrow / nascent topics where the LLM
    legitimately doesn't have canon to draw on (codex round-1
    amendment 5: degrade open rather than fabricate)."""

    consensus_findings: list[ConsensusItem] = Field(min_items=0, max_items=5)
    major_disagreements: list[DisagreementItem] = Field(min_items=0, max_items=3)

    class Config:
        extra = "forbid"
        allow_mutation = False


class FrontierSourcesOutput(BaseModel):
    """Output of LLM call 2 (frontier mode): 5 current hot directions."""

    frontier_hotspots: list[FrontierItem] = Field(min_items=0, max_items=5)

    class Config:
        extra = "forbid"
        allow_mutation = False


# ----------------------------------------------------------------------
# Helpers for downstream consumers
# ----------------------------------------------------------------------


def iter_canonical_works(
    output: CanonicalSourcesOutput,
) -> Iterable[tuple[CanonicalBucket, str, CanonicalWork]]:
    """Yield (bucket, rationale_seed, work) for every work in the canon
    output. Caller uses ``rationale_seed`` (the parent statement /
    axis_description) as the ``canonical_rationale`` field on the
    eventual ``NormalizedSource`` after Crossref verification."""
    for consensus_item in output.consensus_findings:
        for work in consensus_item.representative_works:
            yield ("consensus", consensus_item.statement, work)
    for disagreement_item in output.major_disagreements:
        for work in disagreement_item.representative_works:
            yield ("disagreement", disagreement_item.axis_description, work)


def iter_frontier_works(
    output: FrontierSourcesOutput,
) -> Iterable[tuple[CanonicalBucket, str, CanonicalWork]]:
    """Yield (bucket, rationale_seed, work) for every work in the
    frontier output. Uses ``why_frontier`` as the rationale seed
    (more informative than ``direction`` alone for a UI tooltip)."""
    for item in output.frontier_hotspots:
        for work in item.representative_works:
            yield ("frontier", item.why_frontier, work)


__all__ = [
    "CanonicalBucket",
    "CanonicalSourcesOutput",
    "CanonicalWork",
    "ConsensusItem",
    "DisagreementItem",
    "FrontierItem",
    "FrontierSourcesOutput",
    "iter_canonical_works",
    "iter_frontier_works",
]
