from pathlib import Path

from conftest import seed_approved_scan

from autoessay.agents import integrity as integrity_module
from autoessay.agents.integrity import run_integrity
from autoessay.clients.integrity import NormalizedScanResult, document_hash
from autoessay.config import get_settings


def test_integrity_strips_bibliography_and_block_quotes_from_vendor_payload(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_approved_scan(app_session, tmp_path, monkeypatch, "run_privacy")
    styled_path = run_dir / "drafts" / "v001" / "style" / "paper_styled.md"
    styled_path.write_text(
        styled_path.read_text(encoding="utf-8")
        + "\n> quoted prior passage must not leave local boundary\n"
        + "\n## Bibliography\n\nSensitive bibliography entry must not be sent.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    monkeypatch.setenv("ORIGINALITY_API_KEY", "test-key")
    monkeypatch.setenv("GPTZERO_API_KEY", "test-key")
    monkeypatch.setenv("COPYLEAKS_EMAIL", "test@example.com")
    monkeypatch.setenv("COPYLEAKS_API_KEY", "test-key")
    get_settings.cache_clear()
    captured_payloads: list[str] = []

    async def fake_scan(text: str, kind: str) -> NormalizedScanResult:
        captured_payloads.append(text)
        return NormalizedScanResult(
            vendor="originality",
            scan_type=kind,
            document_hash=document_hash(text),
            status="complete",
            score=0.1,
            scan_id=f"scan-{kind}",
            raw_response={"scan_id": f"scan-{kind}", "status": "complete"},
        )

    monkeypatch.setattr(integrity_module.originality, "scan", fake_scan)

    with app_session() as session:
        run_integrity(run_id, session)

    assert captured_payloads
    assert all("quoted prior passage" not in payload for payload in captured_payloads)
    assert all("Sensitive bibliography entry" not in payload for payload in captured_payloads)
