import json
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

from autoessay.main import app
from autoessay.phase_rerun import PHASES, RUNNING_STATES
from autoessay.state_machine import ALLOWED_TRANSITIONS, RUN_STATES

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "docs" / "state_action_matrix.json"


def _load_matrix() -> dict[str, Any]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _actions() -> list[dict[str, Any]]:
    matrix = _load_matrix()
    return list(matrix["actions"])


def _endpoint(action: dict[str, Any]) -> dict[str, Any]:
    endpoint = action.get("endpoint")
    assert isinstance(endpoint, dict), action["id"]
    return endpoint


def _api_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            routes.add((method, route.path))
    return routes


def test_matrix_schema_and_routes_are_registered() -> None:
    matrix = _load_matrix()
    assert matrix["schema_version"] == 1
    ids = [action["id"] for action in matrix["actions"]]
    assert len(ids) == len(set(ids))

    routes = _api_routes()
    for action in matrix["actions"]:
        endpoint = _endpoint(action)
        method = endpoint["method"]
        path = endpoint["path"]
        assert (method, path) in routes, action["id"]


def test_phase_start_actions_match_state_machine_transitions() -> None:
    for action in _actions():
        source_states = action.get("source_states", [])
        assert isinstance(source_states, list), action["id"]
        for source_state in source_states:
            assert source_state in RUN_STATES, action["id"]

        expected_states = action.get("expected_running_states", [])
        assert isinstance(expected_states, list), action["id"]
        for expected_state in expected_states:
            assert expected_state in RUN_STATES, action["id"]
            assert expected_state in RUNNING_STATES, action["id"]
            for source_state in source_states:
                if source_state == expected_state:
                    # Some recovery/continuation endpoints accept the
                    # running state they will keep using rather than
                    # applying a fresh ALLOWED_TRANSITIONS edge.
                    continue
                assert expected_state in ALLOWED_TRANSITIONS[source_state], (
                    action["id"],
                    source_state,
                    expected_state,
                )


def test_every_rerunnable_phase_has_a_start_action() -> None:
    matrix_phases = {
        action["phase"]
        for action in _actions()
        if action.get("kind") in {"phase_start", "checkpoint_then_phase_start"}
    }
    assert set(PHASES).issubset(matrix_phases)
    assert "proposal" in matrix_phases


def test_failed_state_recovery_contract_is_explicit() -> None:
    matrix = _load_matrix()
    failure_states = set(matrix["failure_states"])
    assert failure_states == {state for state in RUN_STATES if state.startswith("FAILED_")}

    by_id = {action["id"]: action for action in matrix["actions"]}

    assert set(by_id["retry_failed_phase"]["source_states"]) == {"FAILED_FIXABLE"}
    assert by_id["retry_failed_phase"]["ui_behavior"] == "enabled"
    assert by_id["retry_failed_needs_user_rejected"]["expected_http_status"] == 422
    assert by_id["retry_failed_policy_disabled"]["expected_http_status"] == 409
    assert by_id["retry_failed_policy_disabled"]["ui_behavior"] == "disabled"

    assert set(by_id["force_approve_failed_phase"]["source_states"]) == failure_states
    assert set(by_id["generic_failed_backedge_blocked"]["source_states"]) == failure_states
    assert by_id["generic_failed_backedge_blocked"]["target_state_pattern"] == "USER_*"
    assert by_id["generic_failed_backedge_blocked"]["expected_http_status"] == 409
