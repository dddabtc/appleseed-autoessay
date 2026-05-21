from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from autoessay.experiments.abc_blinder import build_blindset, sanitize_blinded_manuscript


def test_sanitize_blinded_manuscript_removes_protocol_disallowed_markers() -> None:
    manuscript = """---
arm: B
generated_at: 2026-05-16T00:00:00Z
---
# 题名
Phase: scout
State: EXPORTS_DONE
Prompt: prompt.redacted.txt
Provenance: provenance.json

正文保留。Arm B used final_rewrite in the production path.
"""

    blinded = sanitize_blinded_manuscript(manuscript)

    assert "题名" in blinded
    assert "正文保留" in blinded
    assert "Arm B" not in blinded
    assert "scout" not in blinded
    assert "EXPORTS_DONE" not in blinded
    assert "prompt.redacted" not in blinded
    assert "provenance" not in blinded.lower()
    assert "2026-05-16T00:00:00Z" not in blinded
    assert "final_rewrite" not in blinded


def test_build_blindset_writes_uuid_manuscripts_and_complete_private_map(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    for kernel_id in ("hist-01", "hist-02"):
        for arm in ("A", "B", "B_prime", "C"):
            manuscript_dir = results_dir / kernel_id / arm
            manuscript_dir.mkdir(parents=True)
            manuscript_dir.joinpath("manuscript.md").write_text(
                f"# 论文 {kernel_id}\n\nArm {arm} metadata\nPhase: scout\n正文。\n",
                encoding="utf-8",
            )

    uuid_values = iter(
        [
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
            UUID("00000000-0000-0000-0000-000000000003"),
            UUID("00000000-0000-0000-0000-000000000004"),
            UUID("00000000-0000-0000-0000-000000000005"),
            UUID("00000000-0000-0000-0000-000000000006"),
            UUID("00000000-0000-0000-0000-000000000007"),
            UUID("00000000-0000-0000-0000-000000000008"),
        ]
    )

    result = build_blindset(
        results_dir=results_dir,
        uuid_factory=lambda: next(uuid_values),
    )

    assert result.blind_map_path == results_dir / "blind_map.json"
    assert len(result.submissions) == 8
    blind_map = json.loads(result.blind_map_path.read_text(encoding="utf-8"))
    assert blind_map["experiment_id"] == "abc-architecture-comparison-v1"
    assert {
        (submission["kernel_id"], submission["arm"]) for submission in blind_map["submissions"]
    } == {
        (kernel_id, arm)
        for kernel_id in ("hist-01", "hist-02")
        for arm in ("A", "B", "B_prime", "C")
    }
    for submission in blind_map["submissions"]:
        path = (
            results_dir
            / submission["kernel_id"]
            / "blinded"
            / submission["submission_uuid"]
            / "manuscript.md"
        )
        blinded = path.read_text(encoding="utf-8")
        assert "正文" in blinded
        assert "Arm " not in blinded
        assert "scout" not in blinded


def test_build_blindset_refuses_to_overwrite_private_map_without_force(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    manuscript_dir = results_dir / "hist-01" / "A"
    manuscript_dir.mkdir(parents=True)
    manuscript_dir.joinpath("manuscript.md").write_text("正文\n", encoding="utf-8")

    build_blindset(results_dir=results_dir)

    with pytest.raises(FileExistsError):
        build_blindset(results_dir=results_dir)

    rebuilt = build_blindset(results_dir=results_dir, force=True)
    assert len(rebuilt.submissions) == 1
