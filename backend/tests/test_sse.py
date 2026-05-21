import json
import time
from concurrent.futures import ThreadPoolExecutor

from conftest import seed_project
from fastapi.testclient import TestClient

from autoessay.main import app
from autoessay.models import Run
from autoessay.state_machine import append_event


def test_run_events_stream_receives_new_event(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    run_id = "run_sse"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="TOPIC_ENTERED",
                baseline_hash="test",
            ),
        )
        session.commit()

    client = TestClient(app)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_append_event_after_stream_starts, app_session, run_id)
    with client.stream("GET", f"/api/runs/{run_id}/events?close_after_event=true") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        data_line = _next_data_line(response)
    future.result(timeout=2)
    executor.shutdown(wait=False, cancel_futures=True)

    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["event_type"] == "manual_event"
    assert payload["payload"] == {"message": "hello"}


def _next_data_line(response) -> str:  # type: ignore[no-untyped-def]
    for line in response.iter_lines():
        if line.startswith("data: "):
            return line
    raise AssertionError("stream ended before a data line was received")


def _append_event_after_stream_starts(app_session, run_id: str) -> None:  # type: ignore[no-untyped-def]
    time.sleep(0.1)
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        append_event(session, run, "manual_event", {"message": "hello"})
        session.commit()
