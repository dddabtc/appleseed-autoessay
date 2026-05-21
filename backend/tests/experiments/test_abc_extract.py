from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from autoessay.experiments.abc_extract import KernelMetadata, dump_front_half_package


def test_dump_front_half_package_records_missing_optional_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "synthesis").mkdir()
    (run_dir / "discovery" / "scout_report.md").write_text(
        "# Scout\n\nfindings\n", encoding="utf-8"
    )
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps([{"source_id": "S1", "title": "Source"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "synthesis" / "claims.jsonl").write_text(
        '{"claim_id":"c1","source_id":"S1"}\n',
        encoding="utf-8",
    )
    (run_dir / "exports").mkdir()
    (run_dir / "exports" / "manuscript.md").write_text("must not leak", encoding="utf-8")

    paths = dump_front_half_package(
        run_dir=run_dir,
        results_dir=tmp_path / "results",
        kernel_id="hist-01",
        a_run_id="run-a",
        metadata=KernelMetadata(
            title="清末海关统计表中的口岸层级变化与地方财政想象",
            research_kernel={"tentative_question": "表格如何塑造财政想象？"},
            target_journal="《历史研究》",
        ),
    )

    package = json.loads(paths.package_json.read_text(encoding="utf-8"))
    by_path = {artifact["path"]: artifact for artifact in package["artifacts"]}
    assert by_path["synthesis/tension_extraction.json"] == {
        "path": "synthesis/tension_extraction.json",
        "present": False,
        "reason": "missing_or_skipped",
    }
    assert by_path["synthesis/framework_lens.json"]["present"] is False
    package_md = paths.package_md.read_text(encoding="utf-8")
    assert "must not leak" not in package_md
    assert "清末海关统计表" in package_md
    assert (
        paths.package_sha256.read_text(encoding="utf-8").strip()
        == sha256(package_md.encode("utf-8")).hexdigest()
    )


def test_dump_front_half_package_only_reads_allowlisted_paths(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "sources").mkdir()
    (run_dir / "synthesis").mkdir()
    (run_dir / "drafts" / "v001").mkdir(parents=True)
    (run_dir / "drafts" / "v001" / "manuscript.md").write_text("late draft", encoding="utf-8")

    paths = dump_front_half_package(
        run_dir=run_dir,
        results_dir=tmp_path / "results",
        kernel_id="hist-02",
    )

    package_md = paths.package_md.read_text(encoding="utf-8")
    package_json = paths.package_json.read_text(encoding="utf-8")
    assert "late draft" not in package_md
    assert "late draft" not in package_json
