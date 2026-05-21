"""PR-387 regression: PR-382 ``_advance_*`` handlers built
``CheckpointDecisionRequest`` with ``decision_payload={"approve": True}``
but the proposal / integrity_review checkpoints read ``accept`` —
``_bool_from_request(request, "accept")`` returned None and the
checkpoint raised ``HTTPException(400, "accept must be true or false")``.

Original PR-382 tests covered the helpers + HTTP shape but never
called a handler end-to-end, so the field-name mismatch escaped to
prod. Caught by playwright real-paper smoke 2026-05-13: run with
auto_advance=True reached USER_PROPOSAL_REVIEW but stopped because
the coordinator's accept call 400'd.

Hardening: assert each handler's CheckpointDecisionRequest carries
the field name the receiving checkpoint actually reads. Cheaper than
a full end-to-end run-through but catches the same class of bug.
"""

from __future__ import annotations

from unittest.mock import patch

from autoessay.main import CheckpointDecisionRequest


def _captured_request(handler_name: str) -> CheckpointDecisionRequest:
    """Invoke an ``_advance_*`` handler with all side effects mocked
    and return the ``CheckpointDecisionRequest`` it passed to the
    checkpoint recorder."""
    from autoessay import auto_advance

    handler = getattr(auto_advance, handler_name)

    captured: dict[str, CheckpointDecisionRequest] = {}

    def capture(_run, request, _session, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured["request"] = request
        return None

    def capture_with_scope(_run, _scope, request, _session, *args, **kwargs):  # type: ignore[no-untyped-def]
        # source_review variant takes (run, scope, request, session)
        captured["request"] = request
        return None

    patches = [
        patch("autoessay.main._record_proposal_checkpoint", side_effect=capture),
        patch("autoessay.main._record_integrity_review_checkpoint", side_effect=capture),
        patch("autoessay.main._record_final_acceptance_checkpoint", side_effect=capture),
        patch("autoessay.main._record_external_scan_checkpoint", side_effect=capture),
        patch("autoessay.main._record_novelty_checkpoint", side_effect=capture),
        patch(
            "autoessay.main._record_source_review_checkpoint",
            side_effect=capture_with_scope,
        ),
        # next-phase starters — stub all of them so handlers don't try
        # to claim locks / fire RQ jobs.
        patch("autoessay.main.start_scout"),
        patch("autoessay.main.start_curator"),
        patch("autoessay.main.start_synthesizer"),
        patch("autoessay.main.start_framework_lens"),
        patch("autoessay.main.start_ideator"),
        patch("autoessay.main.start_drafter"),
        patch("autoessay.main.start_critic"),
        patch("autoessay.main.start_integrity"),
        patch("autoessay.main.start_exports"),
        # Don't actually emit audit events to a real session.
        patch("autoessay.auto_advance._emit_advanced"),
    ]
    for p in patches:
        p.start()
    try:
        # Build a minimal stub run + session. Each handler refreshes
        # the run before starting the next phase; we mock that out
        # by returning the same stub.
        class _Run:
            id = "test_run_pr387"
            state = "UNUSED"
            auto_advance = True

        class _Session:
            def commit(self):  # type: ignore[no-untyped-def]
                pass

            def refresh(self, _):  # type: ignore[no-untyped-def]
                pass

            def rollback(self):  # type: ignore[no-untyped-def]
                pass

        # Some handlers do heavy work loading payloads from disk —
        # those aren't covered here; we only run the field-shape
        # handlers (proposal, integrity, final_acceptance, external_scan,
        # final_acceptance also touches export_formats which is fine).
        run = _Run()
        run.state = {
            "_advance_proposal_review": "USER_PROPOSAL_REVIEW",
            "_advance_integrity_review": "USER_INTEGRITY_REVIEW",
            "_advance_final_acceptance": "USER_FINAL_ACCEPTANCE",
            "_advance_external_scan_approval": "USER_EXTERNAL_SCAN_APPROVAL",
        }.get(handler_name, "UNUSED")
        handler(_Session(), run, "test")
    finally:
        for p in patches:
            p.stop()
    return captured["request"]


def _bool_for(request: CheckpointDecisionRequest, key: str) -> bool | None:
    """Mirror ``_bool_from_request`` resolution: top-level field
    first, then decision_payload."""
    direct = getattr(request, key, None)
    if direct is not None:
        return bool(direct)
    value = request.decision_payload.get(key)
    return value if isinstance(value, bool) else None


def test_proposal_review_sends_accept_field() -> None:
    """The proposal checkpoint reads ``accept`` via
    ``_bool_from_request``. PR-382 sent only ``decision_payload={"approve": True}``
    which the checkpoint discarded → HTTPException 400."""
    request = _captured_request("_advance_proposal_review")
    assert _bool_for(request, "accept") is True


def test_integrity_review_sends_accept_field() -> None:
    """Integrity checkpoint also reads ``accept`` — same PR-382 typo."""
    request = _captured_request("_advance_integrity_review")
    assert _bool_for(request, "accept") is True


def test_final_acceptance_sends_accept_field() -> None:
    """Final acceptance checkpoint reads ``accept`` (already correct
    in PR-382 — this test pins it as a regression guard)."""
    request = _captured_request("_advance_final_acceptance")
    assert _bool_for(request, "accept") is True


def test_external_scan_skips_with_approve_false_and_skip_reason() -> None:
    """External scan is the one checkpoint that reads ``approve``
    instead of ``accept``. Auto-pilot skips it (``approve=False`` +
    a scholarly skip reason that passes the safety gate's tone check)
    so the run can progress to integrity review without burning the
    external plagiarism / AI-style scan budget."""
    request = _captured_request("_advance_external_scan_approval")
    assert _bool_for(request, "approve") is False
    skip_reason = request.decision_payload.get("skip_reason")
    assert isinstance(skip_reason, str)
    assert len(skip_reason) > 10
