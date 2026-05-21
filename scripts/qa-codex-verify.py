#!/usr/bin/env python3
"""PR-249b — codex critic verifier wrapper.

Reads per-phase artifact dumps written by real-paper.spec.ts
(PR-249a), feeds each artifact + the matching versioned rubric
into a codex CLI subprocess, and writes a structured JSON
verdict per phase.

Codex round-1 design verdict (2026-05-06, AGREE-w-amend):
- Q3=A: critic verifier is independent of the spec (this script).
- Q4=B: spec failures do NOT auto-push code; this verifier emits
  bug reports for the operator to resolve in a follow-up PR.
- Q5=C: per-phase critic JSON, single + full-run entry points.
- Codex amendment 4: rubric returns {pass | fail | needs_review}
  with a stable JSON schema so qa-loop.py (PR-249c, future) can
  drive the auto-fix loop.
- Codex amendment 5: support both single-phase (--phase X) and
  full-run (--all) entry points; partial re-verification doesn't
  require re-running the 11-min UI walk.

Inputs
------
--artifact-dir  tmp/qa-artifacts/<run-id>
--phase         single phase to verify (omit for --all)
--all           verify every phase artifact under --artifact-dir
--rubric-dir    docs/qa/critic-prompts (default)
--rubric-version  v01 (default; bump when changing rubric)
--codex-model   gpt-5.5 (default)
--codex-cmd     codex (default; binary path)
--out-dir       <artifact-dir>/verdicts (default)
--dry-run       print prompts without invoking codex

Output
------
<out-dir>/<phase>.json — structured verdict per phase. Schema:

```json
{
  "qa_id": "FR-XX.YY.ZZ",
  "phase": "synthesizer",
  "rubric_version": "v01",
  "verdict": "pass" | "fail" | "needs_review",
  "score_0_5": 0..5,
  "design_requirement_gaps": ["..."],
  "evidence_quotes": ["..."],
  "follow_up_actions": ["..."]
}
```

Exit code
---------
0  every verified phase returned `pass`
1  any phase returned `fail`
2  any phase returned `needs_review` (and none failed)
3  setup error (missing rubric, malformed artifact, codex CLI not
   found)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


VERDICT_VALUES = {"pass", "fail", "needs_review"}


def _read_rubric(rubric_dir: Path, version: str, phase: str) -> str:
    """Per-phase rubric > generic fallback."""
    candidate = rubric_dir / f"{version}-{phase}.md"
    if candidate.exists():
        return candidate.read_text()
    generic = rubric_dir / f"{version}-generic.md"
    if generic.exists():
        return generic.read_text()
    raise FileNotFoundError(
        f"no rubric for phase={phase!r} (looked for {candidate} and {generic})"
    )


def _load_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _build_prompt(rubric: str, artifact: dict[str, Any]) -> str:
    """Combine rubric + artifact into the codex prompt.

    The artifact JSON is wrapped in a fenced block; the rubric's
    instructions tell codex to output a single JSON verdict object.
    Trim the artifact if it's huge (drafter manuscripts can be 30k+
    bytes — soft cap at ~8k chars to keep the prompt manageable).
    """
    artifact_str = json.dumps(artifact, ensure_ascii=False, indent=2)
    if len(artifact_str) > 8000:
        artifact_str = artifact_str[:8000] + "\n... [truncated]"
    return (
        f"{rubric}\n\n"
        "## Phase artifact under review\n\n"
        "```json\n"
        f"{artifact_str}\n"
        "```\n\n"
        "Output a single JSON verdict object as specified above. "
        "Do not include prose before or after the JSON.\n"
    )


def _invoke_codex(
    codex_cmd: str,
    model: str,
    prompt: str,
    dry_run: bool = False,
) -> str:
    if dry_run:
        return json.dumps(
            {
                "qa_id": "DRY-RUN",
                "phase": "dry-run",
                "rubric_version": "dry",
                "verdict": "needs_review",
                "score_0_5": 0,
                "design_requirement_gaps": ["dry-run: codex not invoked"],
                "evidence_quotes": [],
                "follow_up_actions": ["pass --no-dry-run to invoke codex"],
            }
        )
    if not shutil.which(codex_cmd):
        raise FileNotFoundError(
            f"codex CLI not found on PATH: {codex_cmd!r}. "
            "Install codex or pass --codex-cmd /path/to/codex."
        )
    proc = subprocess.run(
        [
            codex_cmd,
            "exec",
            "-m",
            model,
            "-c",
            "model_reasoning_effort=xhigh",
            "-s",
            "read-only",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"codex exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def _extract_verdict_json(stdout: str) -> dict[str, Any]:
    """Codex's stdout includes the prompt echo + reasoning + the JSON.
    Find the last fenced JSON block or the first balanced {...}."""
    # Try fenced block first.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stdout, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Fallback: last balanced top-level JSON object.
    starts = [i for i, c in enumerate(stdout) if c == "{"]
    for start in reversed(starts):
        depth = 0
        for i in range(start, len(stdout)):
            if stdout[i] == "{":
                depth += 1
            elif stdout[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = stdout[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    raise ValueError(
        "could not extract a JSON verdict from codex stdout (first 500 chars: "
        f"{stdout[:500]!r})"
    )


def _normalize_verdict(
    raw: dict[str, Any], phase: str, rubric_version: str
) -> dict[str, Any]:
    """Apply the locked schema, fill missing fields with safe defaults."""
    v = (raw.get("verdict") or "").strip().lower()
    if v not in VERDICT_VALUES:
        v = "needs_review"
    score = raw.get("score_0_5", 0)
    if not isinstance(score, (int, float)) or not (0 <= score <= 5):
        score = 0
    gaps = raw.get("design_requirement_gaps") or []
    quotes = raw.get("evidence_quotes") or []
    follow_ups = raw.get("follow_up_actions") or []
    return {
        "qa_id": raw.get("qa_id") or f"FR-XX.{phase}",
        "phase": phase,
        "rubric_version": rubric_version,
        "verdict": v,
        "score_0_5": int(score),
        "design_requirement_gaps": list(gaps)[:50],
        "evidence_quotes": list(quotes)[:20],
        "follow_up_actions": list(follow_ups)[:30],
    }


def _verify_one(
    artifact_path: Path,
    rubric_dir: Path,
    rubric_version: str,
    out_dir: Path,
    codex_cmd: str,
    model: str,
    dry_run: bool,
) -> dict[str, Any]:
    artifact = _load_artifact(artifact_path)
    phase = artifact.get("meta", {}).get("phase") or artifact_path.stem
    print(f"[verify] {phase} (artifact={artifact_path.name})")
    rubric = _read_rubric(rubric_dir, rubric_version, phase)
    prompt = _build_prompt(rubric, artifact)
    stdout = _invoke_codex(codex_cmd, model, prompt, dry_run=dry_run)
    raw = _extract_verdict_json(stdout)
    verdict = _normalize_verdict(raw, phase, rubric_version)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{phase}.json"
    out_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"  → {verdict['verdict']} (score {verdict['score_0_5']}/5) → {out_path}")
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--phase", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--rubric-dir", default=Path("docs/qa/critic-prompts"), type=Path
    )
    parser.add_argument("--rubric-version", default="v01")
    parser.add_argument("--codex-model", default="gpt-5.5")
    parser.add_argument("--codex-cmd", default="codex")
    parser.add_argument("--out-dir", default=None, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.all and not args.phase:
        print("must pass either --all or --phase <name>", file=sys.stderr)
        return 3

    phase_dir = args.artifact_dir / "phase-outputs"
    if not phase_dir.exists():
        print(f"no phase-outputs dir under {args.artifact_dir}", file=sys.stderr)
        return 3

    out_dir = args.out_dir or (args.artifact_dir / "verdicts")

    if args.all:
        artifact_paths = sorted(phase_dir.glob("*.json"))
    else:
        # Match either "<NN>-<phase>.json" or "<phase>.json".
        candidates = [
            *sorted(phase_dir.glob(f"*-{args.phase}.json")),
            phase_dir / f"{args.phase}.json",
        ]
        artifact_paths = [p for p in candidates if p.exists()]
        if not artifact_paths:
            print(f"no artifact found for phase={args.phase!r}", file=sys.stderr)
            return 3

    verdicts: list[dict[str, Any]] = []
    try:
        for path in artifact_paths:
            verdicts.append(
                _verify_one(
                    path,
                    args.rubric_dir,
                    args.rubric_version,
                    out_dir,
                    args.codex_cmd,
                    args.codex_model,
                    dry_run=args.dry_run,
                )
            )
    except FileNotFoundError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        return 3

    fail_n = sum(1 for v in verdicts if v["verdict"] == "fail")
    review_n = sum(1 for v in verdicts if v["verdict"] == "needs_review")
    pass_n = sum(1 for v in verdicts if v["verdict"] == "pass")
    print(f"\nsummary: pass={pass_n} fail={fail_n} needs_review={review_n}")
    if fail_n:
        return 1
    if review_n:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
