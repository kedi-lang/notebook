from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kedi_notebook import NotebookSessionManager, discover_host_pythons
from kedi_notebook.sandbox_worker import handle_request
from kedi_notebook.server import create_app


def test_discover_host_pythons_prioritizes_explicit_interpreter() -> None:
    pythons = discover_host_pythons([sys.executable])

    assert pythons
    assert pythons[0].explicit is True
    assert Path(pythons[0].executable).resolve() == Path(sys.executable).resolve()
    assert pythons[0].version.startswith(f"{sys.version_info.major}.{sys.version_info.minor}.")


def test_discover_host_pythons_rejects_missing_explicit_interpreter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        discover_host_pythons([tmp_path / "missing-python"])


def test_discover_host_pythons_preserves_explicit_virtualenv_path(tmp_path: Path) -> None:
    executable = tmp_path / "python"
    executable.symlink_to(sys.executable)

    pythons = discover_host_pythons([executable])

    assert pythons[0].explicit is True
    assert pythons[0].executable == str(executable.absolute())


def test_host_notebook_session_keeps_kedi_and_selected_python_state(tmp_path: Path) -> None:
    manager = NotebookSessionManager(cwd=tmp_path, explicit_pythons=[sys.executable])
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        first = session.execute(
            cell_id="setup",
            source="[value: int] = `40`\n`print('host ready')`",
        )
        second = session.execute(cell_id="result", source="= `value + 2`")
    finally:
        manager.close_all()

    assert first["ok"] is True
    assert first["executionCount"] == 1
    assert first["stdout"] == "host ready\n"
    assert second["ok"] is True
    assert second["executionCount"] == 2
    assert second["result"] == {"kind": "json", "type": "int", "value": 42}


def test_host_notebook_terminal_uses_selected_python_and_working_directory(
    tmp_path: Path,
) -> None:
    manager = NotebookSessionManager(cwd=tmp_path, explicit_pythons=[sys.executable])
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        result = session.execute_terminal(
            cell_id="terminal",
            source=(
                '!python -c "import pathlib,sys; print(sys.executable); print(pathlib.Path.cwd())"'
            ),
        )
    finally:
        manager.close_all()

    assert result["ok"] is True
    assert result["executionCount"] == 1
    lines = str(result["stdout"]).splitlines()
    assert Path(lines[0]).resolve() == Path(sys.executable).resolve()
    assert Path(lines[1]) == tmp_path


def test_host_notebook_terminal_streams_output_before_process_finishes(
    tmp_path: Path,
) -> None:
    manager = NotebookSessionManager(cwd=tmp_path, explicit_pythons=[sys.executable])
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    started = time.perf_counter()
    try:
        events = session.stream_terminal(
            cell_id="terminal",
            source=(
                "!python -u -c \"import time; print('first', end='', flush=True); "
                "time.sleep(1.5); print('second', flush=True)\""
            ),
        )
        first = next(events)
        first_elapsed = time.perf_counter() - started
        remaining = list(events)
    finally:
        manager.close_all()

    assert first == {"type": "output", "stream": "stdout", "text": "first"}
    assert first_elapsed < 1.0
    streamed = "".join(str(event["text"]) for event in remaining if event["type"] == "output")
    assert streamed == "second\n"
    assert remaining[-1]["type"] == "result"
    assert remaining[-1]["ok"] is True
    assert remaining[-1]["stdout"] == "firstsecond\n"


def test_terminal_cell_requires_bang_on_each_command(tmp_path: Path) -> None:
    manager = NotebookSessionManager(cwd=tmp_path, explicit_pythons=[sys.executable])
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        result = session.execute_terminal(
            cell_id="terminal",
            source="!echo first\necho second",
        )
    finally:
        manager.close_all()

    assert result["ok"] is False
    assert "line 2 must begin with '!'" in str(result["error"])


def test_browser_notebook_session_uses_long_lived_bridge(tmp_path: Path) -> None:
    manager = NotebookSessionManager(cwd=tmp_path)
    session = manager.create(mode="browser")
    stop = threading.Event()

    def serve_python() -> None:
        while not stop.is_set():
            request = session.next_browser_request(timeout=0.05)
            if request is None:
                continue
            request_id = request.pop("id")
            session.submit_browser_response(request_id, handle_request(request))

    worker = threading.Thread(target=serve_python)
    worker.start()
    try:
        first = session.execute(cell_id="setup", source="[items: list[int]] = `[1, 2]`")
        second = session.execute(
            cell_id="append",
            source="`items.append(3)`\n= `items`",
        )
    finally:
        stop.set()
        manager.close_all()
        worker.join(timeout=2)

    assert first["ok"] is True
    assert second["result"] == {
        "kind": "json",
        "type": "list",
        "value": [1, 2, 3],
    }


def test_browser_notebook_terminal_uses_existing_bridge(tmp_path: Path) -> None:
    manager = NotebookSessionManager(cwd=tmp_path)
    session = manager.create(mode="browser")
    release_response = threading.Event()
    response_sent = threading.Event()

    def serve_terminal() -> None:
        request = session.next_browser_request(timeout=1)
        assert request is not None
        request_id = request.pop("id")
        assert request == {"action": "execute_terminal", "command": "pip list"}
        session.submit_browser_output(request_id, stream="stdout", text="pydantic ")
        assert release_response.wait(timeout=2)
        session.submit_browser_output(request_id, stream="stdout", text="2.0\n")
        response_sent.set()
        session.submit_browser_response(
            request_id,
            {"ok": True, "stdout": "pydantic 2.0\n", "stderr": ""},
        )

    worker = threading.Thread(target=serve_terminal)
    worker.start()
    try:
        events = session.stream_terminal(cell_id="packages", source="!pip list")
        first = next(events)
        assert not response_sent.is_set()
        release_response.set()
        remaining = list(events)
    finally:
        release_response.set()
        manager.close_all()
        worker.join(timeout=2)

    assert first == {"type": "output", "stream": "stdout", "text": "pydantic "}
    assert remaining[0] == {
        "type": "output",
        "stream": "stdout",
        "text": "2.0\n",
    }
    assert remaining[-1]["ok"] is True
    assert remaining[-1]["stdout"] == "pydantic 2.0\n"
    assert remaining[-1]["executionCount"] == 1


def test_notebook_execution_error_returns_cell_diagnostic(tmp_path: Path) -> None:
    manager = NotebookSessionManager(cwd=tmp_path, explicit_pythons=[sys.executable])
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        result = session.execute(cell_id="broken", source="= `1 / 0`")
    finally:
        manager.close_all()

    assert result["ok"] is False
    assert result["cellId"] == "broken"
    assert "division by zero" in str(result["error"])
    assert result["stdout"] == ""


def test_notebook_api_defaults_to_browser_and_lists_host_python(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path, explicit_pythons=[sys.executable])

    with TestClient(app) as client:
        runtimes = client.get("/api/notebook/runtimes")
        created = client.post("/api/notebook/sessions", json={"mode": "browser"})
        session_id = created.json()["sessionId"]
        closed = client.delete(f"/api/notebook/sessions/{session_id}")

    assert runtimes.status_code == 200
    assert runtimes.json()["default"] == "browser"
    assert runtimes.json()["host"][0]["explicit"] is True
    assert created.status_code == 200
    assert created.json()["mode"] == "browser"
    assert closed.status_code == 200


def test_notebook_api_executes_host_terminal_cell(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path, explicit_pythons=[sys.executable])

    with TestClient(app) as client:
        runtimes = client.get("/api/notebook/runtimes").json()
        python_id = runtimes["host"][0]["id"]
        created = client.post(
            "/api/notebook/sessions",
            json={"mode": "host", "pythonId": python_id},
        )
        session_id = created.json()["sessionId"]
        executed = client.post(
            f"/api/notebook/sessions/{session_id}/terminal/execute",
            json={"cellId": "terminal", "source": "!python -c 'print(42)'"},
        )

    assert executed.status_code == 200
    assert executed.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in executed.text.splitlines()]
    assert events[0] == {"type": "output", "stream": "stdout", "text": "42\n"}
    assert events[-1]["type"] == "result"
    assert events[-1]["stdout"] == "42\n"


def test_notebook_static_route_serves_notebook_ui(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path, explicit_pythons=[sys.executable])

    with TestClient(app) as client:
        response = client.get("/notebook/")
        script = client.get("/notebook/notebook.js")

    assert response.status_code == 200
    assert "Kedi Notebook" in response.text
    assert 'id="runtime-select"' in response.text
    assert '<option value="terminal">Terminal</option>' in response.text
    assert script.status_code == 200
    assert "renderCell(cell, cellPosition + 1)" in script.text
    assert "cell.executionCount" not in script.text
