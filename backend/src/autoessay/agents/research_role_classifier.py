"""PR-C1.a: research_role classifier.

Tags every shortlisted source with one of 4 tiers so the
synthesizer's dual-track logic and PR-C2's framework_lens can
operate on a clean partition:

- ``primary_source``           — evidentiary item (archive,
                                  fieldwork transcript, manuscript,
                                  statute, contemporary witness).
- ``secondary_argument``       — published scholarship arguing a
                                  position about the topic
                                  (DEFAULT for backfilled rows).
- ``theoretical_lens``         — framework-level work used as a
                                  conceptual lens (Bourdieu,
                                  Skinner, social-network theory…).
- ``methodological_reference`` — work cited only for a method.

Codex C1 round-1 amendment: classification is **per-run-context**,
not per-source. Bourdieu can be theoretical_lens for one run and
primary_source for a Bourdieu-as-subject paper.

The stub mode is deterministic by ``source_id`` prefix so the
playwright suite can rely on stable assertions:

- ``archive_*`` / ``primary_*`` → ``primary_source``
- ``theory_*``  / ``lens_*``    → ``theoretical_lens``
- ``method_*``                  → ``methodological_reference``
- everything else               → ``secondary_argument``
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

RESEARCH_ROLES: tuple[str, ...] = (
    "primary_source",
    "secondary_argument",
    "theoretical_lens",
    "methodological_reference",
)

DEFAULT_RESEARCH_ROLE = "secondary_argument"


_STUB_ENV_FLAG = "AUTOESSAY_RESEARCH_ROLE_CLASSIFIER_STUB"


def _stub_role_for(source_id: str) -> str:
    """Deterministic fallback. Identical inputs always map to the
    same role; no randomness.
    """
    sid = source_id.lower()
    if sid.startswith(("archive_", "primary_")):
        return "primary_source"
    if sid.startswith(("theory_", "lens_")):
        return "theoretical_lens"
    if sid.startswith("method_"):
        return "methodological_reference"
    return DEFAULT_RESEARCH_ROLE


def is_stub_enabled() -> bool:
    return os.environ.get(_STUB_ENV_FLAG, "0") == "1"


def classify_sources(
    sources: Iterable[Any],
    *,
    paper_mode: str,
    research_kernel: Mapping[str, object] | None,
    stub: bool | None = None,
) -> dict[str, str]:
    """Returns ``{source_id: role}`` for each input source.

    ``sources`` items must expose ``.source_id`` (NormalizedSource,
    SourceRecord, or any duck-typed equivalent).

    ``paper_mode`` and ``research_kernel`` are the per-run context
    that the LLM call would use to disambiguate (e.g. theoretical
    lens vs primary source for the same work in different topics).
    The stub mode ignores them — callers MUST still pass them so a
    later non-stub implementation drops in without API changes.
    """
    use_stub = stub if stub is not None else is_stub_enabled()
    if use_stub:
        return {s.source_id: _stub_role_for(s.source_id) for s in sources}
    # Non-stub path: real LLM classification call. Keep this branch
    # small for now — the LLM-driven implementation lands in a
    # follow-up commit once we've validated the wire-shape via stub
    # in playwright. For now, fall back to deterministic stub so
    # the column is populated even when the flag is off.
    # TODO(PR-C1.a follow-up): replace with litellm call against
    # paper_mode + kernel context.
    del paper_mode, research_kernel  # unused in current fallback
    return {s.source_id: _stub_role_for(s.source_id) for s in sources}


def is_valid_role(role: str) -> bool:
    return role in RESEARCH_ROLES
