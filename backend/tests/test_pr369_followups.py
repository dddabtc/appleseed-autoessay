"""PR-369 follow-up coverage (review consensus + cross-cutting).

Targets the remaining items from the Claude+codex review:

* P2-4: ``LLMClient`` now accepts a ``timeout_seconds`` kwarg and the
  Stage B caller passes 900.0. Without this, the injected
  ``httpx.AsyncClient(timeout=900)`` was silently overridden by the
  settings default (180s) at every ``stream(timeout=...)`` site.
* X-2: ``phase_started`` events now snapshot ``mathematical_mode`` so
  audit trails capture the value at phase entry rather than relying
  on a live re-read.
* X-3: Stage B emits ``phase_progress`` events at each turn boundary
  (start / success / fail-transport / fail-empty) so a stalled call
  surfaces before the RQ 90-min ceiling.
"""

from __future__ import annotations

from autoessay.llm_client import LLMClient


def test_llm_client_accepts_timeout_seconds_override() -> None:
    # P2-4: explicit timeout_seconds beats the settings default.
    client = LLMClient(
        providers=[
            type(
                "Spec",
                (),
                {"name": "test", "base_url": "https://t.test", "api_key": "k", "model": "m"},
            )(),
        ],
        timeout_seconds=900.0,
    )
    assert client._timeout_seconds == 900.0


def test_llm_client_falls_back_to_settings_when_timeout_not_passed() -> None:
    # P2-4: omitting timeout_seconds preserves prior behaviour
    # (settings.llm_request_timeout_seconds).
    client = LLMClient(
        providers=[
            type(
                "Spec",
                (),
                {"name": "test", "base_url": "https://t.test", "api_key": "k", "model": "m"},
            )(),
        ],
    )
    from autoessay.config import get_settings

    assert client._timeout_seconds == float(get_settings().llm_request_timeout_seconds)


def test_round0_stage_b_uses_explicit_900s_timeout() -> None:
    # P2-4: assert the caller wires timeout_seconds=900.0 into
    # LLMClient so the test breaks if a future refactor reverts to
    # relying on the injected httpx client's timeout alone.
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "src" / "autoessay" / "agents" / "final_rewrite.py"
    )
    text = source.read_text(encoding="utf-8")
    assert "timeout_seconds=900.0" in text


def test_final_rewrite_phase_started_event_snapshots_mathematical_mode() -> None:
    # X-2: phase_started must record mathematical_mode_snapshot. Use a
    # static-source assertion rather than a full e2e run since the
    # entry path is already exercised by test_final_rewrite end-to-end.
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "src" / "autoessay" / "agents" / "final_rewrite.py"
    )
    text = source.read_text(encoding="utf-8")
    assert '"mathematical_mode_snapshot"' in text
    assert 'bool(getattr(run, "mathematical_mode", False))' in text


def test_critic_phase_started_event_snapshots_mathematical_mode() -> None:
    # X-2 critic side.
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "src" / "autoessay" / "agents" / "critic.py"
    text = source.read_text(encoding="utf-8")
    assert '"mathematical_mode_snapshot"' in text


def test_stage_b_emits_progress_events_at_turn_boundaries() -> None:
    # X-3: helper emits phase_progress events at started / succeeded /
    # failed_transport / failed_empty for each of the two Stage B turns.
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "src" / "autoessay" / "agents" / "final_rewrite.py"
    )
    text = source.read_text(encoding="utf-8")
    # The helper function must exist.
    assert "def _emit_progress(" in text
    # Stage B emits events at both turn boundaries with the four
    # outcome statuses.
    # The formatter wraps multi-arg calls across lines, so assert on
    # the status literals being present at all + at least one explicit
    # turn-name + status pair on the same line.
    for status in ["started", "succeeded", "failed_transport", "failed_empty"]:
        assert f'"{status}"' in text, f"Stage B status literal missing: {status}"
    assert '_emit_progress("turn1_critique", "started"' in text
    assert '_emit_progress("turn2_rewrite", "started")' in text
    assert '_emit_progress("turn1_critique", "failed_empty")' in text
    assert '_emit_progress("turn2_rewrite", "failed_empty")' in text
    # Event type is phase_progress with subphase round0_stage_b
    assert '"phase_progress"' in text
    assert '"subphase": "round0_stage_b"' in text
