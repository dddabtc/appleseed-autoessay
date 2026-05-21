from pathlib import Path

import pytest
from conftest import seed_integrity_ready_run
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents import integrity as integrity_module
from autoessay.agents.integrity import run_integrity
from autoessay.clients.integrity import NormalizedScanResult, document_hash
from autoessay.config import get_settings
from autoessay.harness import HookContext, HookRegistry


def test_integrity_harness_strips_private_sections_before_pre_tool(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = seed_integrity_ready_run(app_session, tmp_path, "run_integrity_privacy")
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
    captured_payloads: list[dict[str, object]] = []

    def capture_pre_tool(ctx: HookContext) -> HookContext:
        payload = ctx.run_metadata.get("request_payload")
        if isinstance(payload, dict):
            captured_payloads.append(dict(payload))
        return ctx

    async def fake_scan(text: str, kind: str) -> NormalizedScanResult:
        return NormalizedScanResult(
            vendor="originality_ai",
            scan_type=kind,
            document_hash=document_hash(text),
            status="complete",
            score=0.1,
            scan_id=f"scan-{kind}",
            raw_response={"scan_id": f"scan-{kind}", "status": "complete"},
        )

    hooks = HookRegistry()
    hooks.register_pre_tool("capture_pre_tool", capture_pre_tool)
    monkeypatch.setattr(integrity_module.originality, "scan", fake_scan)

    with app_session() as session:
        run_integrity(run_id, session, hooks=hooks)

    assert captured_payloads
    serialized = "\n".join(str(payload) for payload in captured_payloads)
    assert "Deposit insurance changed bank behavior" in serialized
    assert "quoted prior passage" not in serialized
    assert "Sensitive bibliography entry" not in serialized
