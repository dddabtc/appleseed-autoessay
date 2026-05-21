"""PR-C2c: strict Pydantic output schema + structural integrity gate
for the framework_lens LLM enrichment path.

Schema is intentionally narrower than the on-disk artifact written by
``framework_lens.compose_framework_lens``: it covers ONLY the LLM
output (the ``signals`` array). The runner overlays it onto the rest
of the artifact (``schema_version``, ``synthesizer_input_ref``,
``paper_mode``) before write.

``extra="forbid"`` is the C2c amendment: harness `validate_response`
treats unknown keys as a schema violation and triggers retry +
fallback per the C2c plan (HANDOFF §11.7.1 implementation amendment 3).

The ``framework_lens_integrity`` function is the structural gate that
runs in the post-LLM hook (E in the round-1 codex review):

  * source_id ∈ eligible_source_ids (the actually-passed-to-prompt set)
  * lens_name not a generic placeholder (banned-literal list)
  * lens_name != source_id (after normalization)
  * lens_name has ≥1 alpha char (rejects pure punctuation / digits)
  * key_concepts non-empty after whitespace normalize
  * dedup lens_name within one payload
  * paper_mode-aware: theory_article + non-empty eligible inputs MUST
    have signals.length >= 1 (codex amendment E for theory_article
    semantics — empty lens artifact for a theory paper is silently
    wrong)
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, Field, StrictStr, validator


def _normalize_whitespace(value: str) -> str:
    """Collapse runs of whitespace to a single space and strip ends.
    Bare ``" ".join(s.split())`` matches the convention used by the
    synthesizer / proposal models."""
    return " ".join(value.split())


def _reject_blank(value: str, *, field_label: str) -> str:
    cleaned = _normalize_whitespace(value)
    if not cleaned:
        raise ValueError(f"{field_label} must be non-empty after whitespace normalize")
    return cleaned


# Banned ``lens_name`` placeholders. Literal-equality match (after
# whitespace normalize + casefold) — NOT substring scan, because the
# applicability_to_kernel field is naturally going to mention phrases
# like "theoretical framework". Kept narrow: only known LLM cop-out
# placeholders. Codex amendment D extended the original 5 with
# untitled/unknown/placeholder/sample/example/general/numbered variants.
LENS_NAME_BANNED_LITERAL: frozenset[str] = frozenset(
    {
        # original C2c proposed
        "lens 1",
        "lens 2",
        "lens 3",
        "default lens",
        "default theory",
        "default framework",
        "generic theory",
        "generic framework",
        "generic lens",
        "theory 1",
        "theory 2",
        "theory 3",
        "unnamed lens",
        "unnamed theory",
        "unnamed framework",
        "n/a",
        "none",
        "tbd",
        # codex round-1 amendment D additions
        "untitled lens",
        "untitled theory",
        "untitled framework",
        "unknown lens",
        "unknown theory",
        "unknown framework",
        "placeholder lens",
        "placeholder theory",
        "placeholder framework",
        "sample lens",
        "example lens",
        "framework 1",
        "framework 2",
        "framework 3",
        "general theory",
        "general framework",
    }
)


class LensSignalOutput(BaseModel):
    """One LLM-produced lens signal. Single object in
    ``FrameworkLensSignalsOutput.signals``."""

    lens_name: StrictStr = Field(min_length=2, max_length=120)
    key_concepts: list[StrictStr] = Field(min_items=1, max_items=8)
    source_id: StrictStr = Field(min_length=1)
    applicability_to_kernel: StrictStr = Field(min_length=10, max_length=600)

    @validator("lens_name")
    def _lens_name_normalize(cls, value: str) -> str:
        return _reject_blank(value, field_label="lens_name")

    @validator("source_id")
    def _source_id_normalize(cls, value: str) -> str:
        return _reject_blank(value, field_label="source_id")

    @validator("applicability_to_kernel")
    def _applicability_normalize(cls, value: str) -> str:
        return _reject_blank(value, field_label="applicability_to_kernel")

    @validator("key_concepts", each_item=True)
    def _concept_non_empty(cls, value: str) -> str:
        cleaned = _normalize_whitespace(value)
        if not cleaned:
            raise ValueError("key_concept must be non-empty after whitespace normalize")
        return cleaned

    class Config:
        extra = "forbid"
        allow_mutation = False


class FrameworkLensSignalsOutput(BaseModel):
    """Top-level LLM output. ``signals`` may be empty; the
    ``framework_lens_integrity`` gate handles the paper_mode-specific
    "must have ≥1 signal" check (codex round-1 amendment C / E)."""

    signals: list[LensSignalOutput] = Field(min_items=0, max_items=8)

    class Config:
        extra = "forbid"
        allow_mutation = False


def framework_lens_integrity(
    payload: FrameworkLensSignalsOutput,
    *,
    eligible_source_ids: Iterable[str],
    paper_mode: str,
    eligible_lens_input_present: bool,
) -> list[str]:
    """Structural integrity gate. Returns a list of human-readable
    error strings — empty list means accept.

    Caller (post_llm hook in agents/framework_lens.py) wraps the
    non-empty result into ``HookResult(verdict=REJECTED_SCHEMA_VIOLATION,
    annotations={'errors': [...], 'message': '...'})`` so the harness's
    corrective-suffix retry knows what to fix.

    Parameters
    ----------
    payload: parsed LLM output (already passed Pydantic schema).
    eligible_source_ids: source_ids that were ACTUALLY passed to the
        LLM in the prompt. Codex round-1 amendment F: this set must
        equal the truncated set the prompt saw, not the full
        theoretical_lens shortlist. Otherwise we'd reject the LLM for
        citing a source it never saw.
    paper_mode: ``run.paper_mode`` value (case_analysis / theory_article
        / etc). Codex round-1 amendment E: theory_article + non-empty
        eligible inputs MUST have ≥1 signal.
    eligible_lens_input_present: True iff at least one theoretical_lens
        source / claim was available before truncation. Used together
        with paper_mode to gate the "empty signals for theory_article"
        rejection — if there were truly zero lens inputs, the upstream
        agent runner already routed the run to FAILED_FIXABLE before
        the LLM call.
    """
    eligible_set = frozenset(_normalize_whitespace(sid).casefold() for sid in eligible_source_ids)
    errors: list[str] = []

    if paper_mode == "theory_article" and eligible_lens_input_present and len(payload.signals) == 0:
        errors.append(
            "paper_mode=theory_article with eligible theoretical_lens inputs "
            "must produce at least one signal; LLM returned an empty list"
        )

    seen_lens_names: set[str] = set()
    for i, sig in enumerate(payload.signals):
        nm_normalized = _normalize_whitespace(sig.lens_name).casefold()
        sid_normalized = _normalize_whitespace(sig.source_id).casefold()

        if nm_normalized in LENS_NAME_BANNED_LITERAL:
            errors.append(
                f"signals[{i}].lens_name='{sig.lens_name}' is banned "
                "(generic placeholder; use a specific framework name)"
            )

        if nm_normalized == sid_normalized:
            errors.append(
                f"signals[{i}].lens_name must differ from source_id "
                "(use the framework's actual name, e.g. 'Bourdieu: habitus')"
            )

        if not any(ch.isalpha() for ch in sig.lens_name):
            errors.append(
                f"signals[{i}].lens_name='{sig.lens_name}' must contain "
                "alphabetic characters (not pure punctuation / digits)"
            )

        if sid_normalized not in eligible_set:
            errors.append(
                f"signals[{i}].source_id='{sig.source_id}' not in eligible "
                f"theoretical_lens sources ({len(eligible_set)} available "
                "in this prompt)"
            )

        if not sig.key_concepts:
            errors.append(f"signals[{i}].key_concepts must be non-empty")

        if nm_normalized in seen_lens_names:
            errors.append(
                f"signals[{i}].lens_name='{sig.lens_name}' duplicates an "
                "earlier signal in this payload"
            )
        seen_lens_names.add(nm_normalized)

    return errors


__all__ = [
    "FrameworkLensSignalsOutput",
    "LENS_NAME_BANNED_LITERAL",
    "LensSignalOutput",
    "framework_lens_integrity",
]
