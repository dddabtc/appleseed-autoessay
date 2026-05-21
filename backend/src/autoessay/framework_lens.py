"""PR-C2.a: framework_lens helpers + skip semantics.

The framework-lens phase sits between synthesizer and ideator
(see ``state_machine.PIPELINE_STATES``). It produces
``synthesis/framework_lens.json`` — a list of LensSignal entries
naming the theoretical lenses that apply to this run's kernel,
which the ideator then references when proposing angle cards.

## Skip semantics (codex C2 round-1 amendment 2)

The phase is OPTIONAL. Skip when ALL three are true:

- ``synthesis/synthesizer.json::theoretical_lens_track`` is empty
- shortlist.json contains no ``research_role=theoretical_lens`` source
- ``run.paper_mode`` is NOT ``theory_article``

Otherwise run. The ``theory_article`` mode is non-skippable —
if a theory_article run reaches USER_FIELD_REVIEW with no lens
inputs, the phase fails fixable with guidance to add lens-tagged
sources.

## Schema

``synthesis/framework_lens.json``:

  {
    "schema_version": 2,
    "synthesizer_input_ref": {
      "synthesizer_pv_id": "pv_...",
      "synthesizer_artifact_hash": "<sha256 hex>"
    },
    "signals": [
      {
        "lens_name": "Bourdieu's habitus / field theory",
        "key_concepts": ["habitus", "cultural capital"],
        "source_id": "openalex_W123",
        "applicability_to_kernel": "..."
      },
      ...
    ]
  }
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

# Env name preserved for back-compat (existing deployments / scripts read
# AUTOESSAY_FRAMEWORK_LENS_STUB directly via ``Settings`` now). Kept as a
# module constant so callers that want the literal name can grep for it.
_STUB_ENV_FLAG = "AUTOESSAY_FRAMEWORK_LENS_STUB"
FRAMEWORK_LENS_ARTIFACT_PATH = "synthesis/framework_lens.json"
SYNTHESIZER_ARTIFACT_PATH = "synthesis/synthesizer.json"


@dataclass(frozen=True)
class LensSignal:
    lens_name: str
    key_concepts: tuple[str, ...]
    source_id: str
    applicability_to_kernel: str


def is_stub_enabled() -> bool:
    """Returns True when the framework_lens phase should produce its
    deterministic stub artifact (no LLM call). Reads the centralized
    ``Settings.framework_lens_stub`` flag, which is bound to the
    ``AUTOESSAY_FRAMEWORK_LENS_STUB`` env variable.

    Tests that flip the env at runtime must also call
    ``get_settings.cache_clear()`` so the next read picks up the new
    value (see backend/tests/test_framework_lens_stub_setting.py).
    """
    from autoessay.config import get_settings

    return bool(get_settings().framework_lens_stub)


def has_theoretical_lens_inputs(
    *,
    dual_track: Mapping[str, object] | None,
    shortlist: Sequence[Mapping[str, object]],
) -> bool:
    """True iff EITHER the dual-track theoretical_lens_track has
    claims OR shortlist contains a source tagged
    research_role=theoretical_lens. Either is sufficient lens
    input for the phase to run."""
    if isinstance(dual_track, Mapping):
        track = dual_track.get("theoretical_lens_track")
        if isinstance(track, list) and len(track) > 0:
            return True
    for entry in shortlist:
        if isinstance(entry, Mapping) and entry.get("research_role") == "theoretical_lens":
            return True
    return False


def should_run_framework_lens(
    *,
    paper_mode: str,
    dual_track: Mapping[str, object] | None,
    shortlist: Sequence[Mapping[str, object]],
) -> bool:
    """The skip-vs-run decision (codex C2 round-1 amendment 2).

    Returns True when the phase MUST or SHOULD run; False when
    it can be skipped (USER_FIELD_REVIEW transitions directly to
    IDEATOR_RUNNING). theory_article mode bypasses the
    skip-when-empty case so a theory paper without lens inputs
    is forced into the failure-fixable surface for user
    correction (see ``framework_lens_skip_failure_guidance``).
    """
    if paper_mode == "theory_article":
        return True
    return has_theoretical_lens_inputs(
        dual_track=dual_track,
        shortlist=shortlist,
    )


def framework_lens_skip_failure_guidance(paper_mode: str) -> str:
    """Copy for the FAILED_FIXABLE event when a theory_article
    run reaches the lens phase with zero lens inputs."""
    if paper_mode == "theory_article":
        return (
            "理论论文模式需要至少一个『理论镜框 / theoretical_lens』来源。"
            "请回到「文献」页，将至少一个来源调整为『理论镜框』层级，再"
            "重新运行。Theory_article requires at least one source tagged "
            "as theoretical_lens; adjust a source's tier on the Sources "
            "tab and rerun."
        )
    return ""


def _stub_signals(
    shortlist: Sequence[Mapping[str, object]],
) -> list[LensSignal]:
    """Deterministic stub: one LensSignal per theoretical_lens-tagged
    source. No randomness — same input always yields same output."""
    out: list[LensSignal] = []
    for entry in shortlist:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("research_role") != "theoretical_lens":
            continue
        sid = str(entry.get("source_id") or "")
        title = str(entry.get("title") or sid)
        venue = str(entry.get("venue") or "")
        # Synthesize a plausible lens_name from title + venue. Real
        # implementation will call an LLM; the stub keeps shape stable.
        out.append(
            LensSignal(
                lens_name=title.split(":")[0].strip() or sid,
                key_concepts=(title.split(":")[0].strip() or sid,),
                source_id=sid,
                applicability_to_kernel=(
                    f"Stubbed signal from source {sid} in venue {venue or 'unknown'}."
                ),
            ),
        )
    return out


def compose_framework_lens(
    *,
    shortlist: Sequence[Mapping[str, object]],
    dual_track: Mapping[str, object] | None,
    paper_mode: str,
    synthesizer_input_ref: Mapping[str, object] | None = None,
    stub: bool | None = None,
) -> dict[str, object]:
    """Build the ``synthesis/framework_lens.json`` payload.

    Returns the dict ready for ``json.dump``. The stub path is
    deterministic; the non-stub path is a TODO that falls back
    to the stub for now so the phase remains usable in dev
    environments without an LLM key configured.
    """
    use_stub = stub if stub is not None else is_stub_enabled()
    # Reserved for non-stub LLM enrichment later.
    del dual_track
    # TODO(C2.a follow-up): when use_stub is False, replace this with
    # an LLM enrichment pass over the stub signals using paper_mode
    # + kernel context. For now both paths produce the deterministic
    # stub so the artifact shape is stable in dev environments.
    _ = use_stub
    signals = _stub_signals(shortlist)
    return {
        "schema_version": 2,
        "paper_mode": paper_mode,
        "synthesizer_input_ref": dict(synthesizer_input_ref or {}),
        "signals": [asdict(s) | {"key_concepts": list(s.key_concepts)} for s in signals],
    }


def write_framework_lens(
    run_dir: Path,
    payload: Mapping[str, object],
) -> Path:
    """Atomic write of ``synthesis/framework_lens.json``.

    Caller is the agent; it has already ensured ``synthesis/``
    exists (the synthesizer phase ran first).
    """
    target = run_dir / FRAMEWORK_LENS_ARTIFACT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def read_framework_lens(run_dir: Path) -> dict[str, object] | None:
    """Returns the ``framework_lens.json`` payload if it exists,
    None otherwise. Used by the ideator and by the API
    endpoint."""
    p = run_dir / FRAMEWORK_LENS_ARTIFACT_PATH
    if not p.exists():
        return None
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def lens_names_from_payload(
    payload: Mapping[str, object] | None,
) -> set[str]:
    """The set of valid lens names for referential-integrity
    validation by the ideator (codex C2 round-1 amendment 6: angle
    cards may only name lenses present in framework_lens.json)."""
    if not isinstance(payload, Mapping):
        return set()
    signals = payload.get("signals")
    if not isinstance(signals, Iterable):
        return set()
    out: set[str] = set()
    for sig in signals:
        if isinstance(sig, Mapping) and isinstance(sig.get("lens_name"), str):
            out.add(str(sig["lens_name"]))
    return out


def build_synthesizer_input_ref(
    run_dir: Path,
    *,
    synthesizer_pv_id: str | None,
) -> dict[str, object]:
    """Immutable input pointer embedded in schema v2 lens artifacts.

    The pv id captures the branch/version lineage when available; the
    artifact hash still anchors legacy or pre-backfill runs that have
    ``synthesizer.json`` on disk but no synthesizer RunHead row.
    """
    synth_path = run_dir / SYNTHESIZER_ARTIFACT_PATH
    return {
        "synthesizer_pv_id": synthesizer_pv_id,
        "synthesizer_artifact_hash": _sha256_file(synth_path) if synth_path.exists() else None,
    }


def resolve_framework_lens_summary_ref(
    run_dir: Path,
    *,
    synthesizer_payload: Mapping[str, object] | None = None,
) -> str | None:
    """Compatibility reader for the legacy forward hook.

    New runs derive the summary ref from the lens-owned artifact. Old
    runs that only have ``synthesizer.json::framework_lens_summary_ref``
    still surface that value through the API.
    """
    if read_framework_lens(run_dir) is not None:
        return FRAMEWORK_LENS_ARTIFACT_PATH
    if isinstance(synthesizer_payload, Mapping):
        legacy = synthesizer_payload.get("framework_lens_summary_ref")
        if legacy:
            return str(legacy)
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
