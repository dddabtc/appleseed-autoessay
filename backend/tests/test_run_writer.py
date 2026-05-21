from autoessay.run_writer import create_run_directory


def test_create_run_directory_writes_required_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    run_dir = create_run_directory(tmp_path, "run_001", "proj_001", domain_id="financial_history")

    assert (run_dir / "run.json").is_file()
    assert (run_dir / "baseline.md").is_file()
    assert (run_dir / "CURRENT_STATUS.md").is_file()
    assert (run_dir / "ledger.jsonl").is_file()
