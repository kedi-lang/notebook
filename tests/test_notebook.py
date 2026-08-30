from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kedi_notebook import NotebookSecretStore, NotebookSessionManager, discover_host_pythons
from kedi_notebook import host_environment as host_environment_module
from kedi_notebook import runtime as notebook_runtime
from kedi_notebook.bridge import BridgeRun
from kedi_notebook.host_environment import HostEnvironmentManager, PreparedHostEnvironment
from kedi_notebook.pyright import PyrightServer
from kedi_notebook.sandbox_worker import handle_request
from kedi_notebook.server import create_app, serve_cli


class _PassthroughHostEnvironment:
    def prepare(
        self,
        *,
        executable: str,
        version: str,
        cwd: Path,
    ) -> PreparedHostEnvironment:
        del version, cwd
        python = Path(executable)
        return PreparedHostEnvironment(executable, str(python.parent.parent))


_PASSTHROUGH_HOST_ENVIRONMENT = _PassthroughHostEnvironment()


@pytest.fixture(autouse=True)
def _isolate_notebook_secret_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "KEDI_NOTEBOOK_SECRETS_PATH",
        str(tmp_path / ".kedi-notebook-secrets.json"),
    )


def test_create_app_loads_project_dotenv_for_new_runtime_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "KEDI_ADAPTER_MODEL=openrouter:test/notebook-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KEDI_ADAPTER_MODEL", raising=False)

    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = app.state.notebook_manager.create(mode="browser")
    try:
        profile = session._session._compiler.default_agent_profile
        assert profile.model == "openrouter:test/notebook-model"
    finally:
        session.close()


def test_create_app_dotenv_does_not_override_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "KEDI_ADAPTER_MODEL=openrouter:test/notebook-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEDI_ADAPTER_MODEL", "existing-model")

    create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )

    assert os.environ["KEDI_ADAPTER_MODEL"] == "existing-model"


def test_notebook_secret_store_writes_private_atomic_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state" / "secrets.json"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = NotebookSecretStore(path)

    store.set("OPENROUTER_API_KEY", "not-a-real-key")
    loaded = NotebookSecretStore(path)

    assert loaded.names == ("OPENROUTER_API_KEY",)
    assert os.environ["OPENROUTER_API_KEY"] == "not-a-real-key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_notebook_secret_api_never_returns_values_and_resets_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEDI_ADAPTER_MODEL", raising=False)
    secret_store = NotebookSecretStore(tmp_path / "secrets.json")
    app = create_app(cwd=tmp_path, secret_store=secret_store)
    old_session = app.state.notebook_manager.create(mode="browser")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        saved = client.put(
            "/api/notebook/secrets",
            json={"name": "KEDI_ADAPTER_MODEL", "value": "openrouter:test/secret-model"},
        )
        listed = client.get("/api/notebook/secrets")

    assert saved.status_code == 200
    assert saved.json() == {
        "ok": True,
        "configured": ["KEDI_ADAPTER_MODEL"],
        "runtimeReset": True,
    }
    assert listed.json() == {"ok": True, "configured": ["KEDI_ADAPTER_MODEL"]}
    assert "secret-model" not in saved.text
    assert "secret-model" not in listed.text
    with pytest.raises(KeyError, match="not found"):
        app.state.notebook_manager.get(old_session.id)
    session = app.state.notebook_manager.create(mode="browser")
    try:
        assert session._session._compiler.default_agent_profile.model == (
            "openrouter:test/secret-model"
        )
    finally:
        session.close()


def test_notebook_secret_api_imports_relative_dotenv_without_returning_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KEDI_ADAPTER_MODEL", raising=False)
    (tmp_path / "notebook.env").write_text(
        "OPENROUTER_API_KEY=not-a-real-key\n"
        "KEDI_ADAPTER_MODEL=openrouter:test/imported-model\n",
        encoding="utf-8",
    )
    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        imported = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "notebook.env"},
        )

    assert imported.status_code == 200
    assert imported.json()["imported"] == ["KEDI_ADAPTER_MODEL", "OPENROUTER_API_KEY"]
    assert "not-a-real-key" not in imported.text
    assert "imported-model" not in imported.text
    assert os.environ["OPENROUTER_API_KEY"] == "not-a-real-key"
    session = app.state.notebook_manager.create(mode="browser")
    try:
        assert session._session._compiler.default_agent_profile.model == (
            "openrouter:test/imported-model"
        )
    finally:
        session.close()


def test_notebook_api_snapshots_and_restores_kedi_environment_without_secrets(
    tmp_path: Path,
) -> None:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.put(
            "/api/notebook/secrets",
            json={"name": "PROVIDER_API_KEY", "value": "do-not-export-this"},
        )
        python_id = client.get("/api/notebook/runtimes").json()["host"][0]["id"]
        created = client.post(
            "/api/notebook/sessions",
            json={"mode": "host", "pythonId": python_id},
        ).json()
        client.post(
            f"/api/notebook/sessions/{created['sessionId']}/cells/execute",
            json={"cellId": "setup", "source": "[value: int] = `20`"},
        )
        captured = client.post(
            f"/api/notebook/sessions/{created['sessionId']}/snapshot",
        )
        client.delete(f"/api/notebook/sessions/{created['sessionId']}")
        restored = client.post(
            "/api/notebook/sessions/restore",
            json={
                "mode": "host",
                "pythonId": python_id,
                "snapshot": captured.json()["snapshot"],
            },
        ).json()
        resumed = client.post(
            f"/api/notebook/sessions/{restored['sessionId']}/cells/execute",
            json={"cellId": "resume", "source": "= `value + 22`"},
        )

    assert captured.status_code == 200
    assert "do-not-export-this" not in captured.text
    assert resumed.status_code == 200
    assert resumed.json()["result"]["value"] == 42


def test_notebook_secret_api_rejects_invalid_name_and_missing_dotenv(tmp_path: Path) -> None:
    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        invalid_name = client.put(
            "/api/notebook/secrets",
            json={"name": "NOT VALID", "value": "secret"},
        )
        missing = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "missing.env"},
        )
        oversized_value = client.put(
            "/api/notebook/secrets",
            json={"name": "SECRET_VALUE", "value": "sensitive-prefix" + "x" * 128_000},
        )

    assert invalid_name.status_code == 400
    assert invalid_name.json()["error"].endswith("valid identifier")
    assert missing.status_code == 400
    assert missing.json()["error"].endswith("does not exist")
    assert oversized_value.status_code == 422
    assert oversized_value.json() == {"ok": False, "error": "Invalid request payload"}
    assert "sensitive-prefix" not in oversized_value.text


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


def test_discover_host_pythons_rejects_explicit_prerelease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "python3.14"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    original_metadata = notebook_runtime._python_metadata

    def metadata(path: Path) -> dict[str, object] | None:
        if path == executable:
            return {"version": [3, 14, 0], "releaselevel": "candidate"}
        return original_metadata(path)

    monkeypatch.setattr(notebook_runtime, "_python_metadata", metadata)

    with pytest.raises(ValueError, match="requires a final Python release"):
        discover_host_pythons([executable])


def test_host_environment_is_named_for_notebook_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "kedi-source"
    source_root.mkdir()
    manager = HostEnvironmentManager(
        root=tmp_path / "environments",
        source_root=source_root,
    )
    created: list[Path] = []
    installed: list[Path] = []

    def create_environment(base_executable: Path, environment: Path) -> None:
        del base_executable
        python = environment / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        created.append(environment)

    monkeypatch.setattr(manager, "_create_environment", create_environment)
    monkeypatch.setattr(manager, "_install_kedi", installed.append)

    first = manager.prepare(
        executable=sys.executable,
        version="3.11.9",
        cwd=tmp_path / "project",
    )
    second = manager.prepare(
        executable=sys.executable,
        version="3.11.9",
        cwd=tmp_path / "project",
    )

    assert first == second
    assert Path(first.directory).name.startswith("kedi-notebook-py3.11-")
    assert created and len(created) == 1
    assert len(installed) == 1
    assert installed[0].name == "python"


def test_host_environment_uses_uv_for_creation_and_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "kedi-source"
    (source_root / "tree-sitter-kedi").mkdir(parents=True)
    manager = HostEnvironmentManager(root=tmp_path / "envs", source_root=source_root)
    calls: list[tuple[list[str], str]] = []

    monkeypatch.setattr(host_environment_module.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        host_environment_module,
        "_run_bootstrap",
        lambda argv, *, operation: calls.append((argv, operation)),
    )

    environment = tmp_path / "runtime"
    manager._create_environment(Path(sys.executable), environment)
    manager._install_kedi(environment / "bin" / "python")

    assert calls[0][0] == [
        "/usr/bin/uv",
        "venv",
        "--python",
        sys.executable,
        "--seed",
        str(environment),
    ]
    assert calls[1][0][:5] == [
        "/usr/bin/uv",
        "pip",
        "install",
        "--python",
        str(environment / "bin" / "python"),
    ]
    assert calls[2][0] == [
        str(environment / "bin" / "python"),
        "-c",
        "import kedi",
    ]


def test_host_environment_falls_back_to_venv_and_ensurepip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HostEnvironmentManager(root=tmp_path / "envs", source_root=tmp_path)
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(host_environment_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        host_environment_module,
        "_run_bootstrap",
        lambda argv, *, operation: calls.append((argv, operation)),
    )

    environment = tmp_path / "runtime"
    manager._create_environment(Path(sys.executable), environment)

    assert calls == [
        (
            [sys.executable, "-m", "venv", "--without-pip", str(environment)],
            "create the notebook virtual environment",
        ),
        (
            [
                str(environment / "bin" / "python"),
                "-m",
                "ensurepip",
                "--upgrade",
                "--default-pip",
            ],
            "install pip into the notebook virtual environment",
        ),
    ]


def test_host_notebook_session_keeps_kedi_and_selected_python_state(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
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
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
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
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
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
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
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


def test_browser_bridge_bounds_queued_output() -> None:
    bridge = BridgeRun("bounded")
    events: list[dict[str, object]] = []

    def request() -> None:
        events.extend(bridge.request_events({"action": "test"}, timeout=2))

    worker = threading.Thread(target=request)
    worker.start()
    request_payload = bridge.next_request(timeout=1)
    assert request_payload is not None
    request_id = request_payload["id"]
    bridge.submit_output(request_id, stream="stdout", text="x" * 250_000)
    bridge.submit_response(request_id, {"ok": True})
    worker.join(timeout=2)

    assert not worker.is_alive()
    output = next(event for event in events if event["type"] == "output")
    assert len(str(output["text"])) == 200_000


def test_notebook_execution_error_returns_cell_diagnostic(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        result = session.execute(cell_id="broken", source="= `1 / 0`")
    finally:
        manager.close_all()

    assert result["ok"] is False
    assert result["cellId"] == "broken"
    assert "division by zero" in str(result["error"])
    assert result["diagnostic"]["source"] == f"<notebook:{session.id[:8]}:1>"
    assert result["stdout"] == ""


def test_host_execution_timeout_terminates_worker_and_requests_runtime_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notebook_runtime, "_EXECUTION_TIMEOUT", 0.1)
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    started = time.perf_counter()
    try:
        result = session.execute(
            cell_id="slow",
            source="= `(__import__('time').sleep(5), 1)[1]`",
        )
    finally:
        manager.close_all()

    assert time.perf_counter() - started < 2
    assert result["ok"] is False
    assert result["runtimeReset"] is True
    assert "exceeded 0.1 seconds" in str(result["error"])


def test_interrupt_closes_running_host_execution_without_deadlock(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    result: dict[str, object] = {}

    def execute() -> None:
        result.update(
            session.execute(
                cell_id="slow",
                source="= `(__import__('time').sleep(30), 1)[1]`",
            )
        )

    worker = threading.Thread(target=execute)
    worker.start()
    time.sleep(0.2)
    manager.close(session.id)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["ok"] is False
    assert result["runtimeReset"] is True


def test_terminal_output_and_result_payloads_are_bounded(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        terminal = session.execute_terminal(
            cell_id="large-output",
            source="!python -c \"print('x' * 250000)\"",
        )
        value = session.execute(
            cell_id="large-result",
            source="= `'x' * 250000`",
        )
    finally:
        manager.close_all()

    assert len(str(terminal["stdout"])) < 201_000
    assert str(terminal["stdout"]).endswith("[output truncated by Kedi Notebook]")
    assert value["result"]["truncated"] is True
    assert len(str(value["result"]["value"])) < 201_000


def test_session_manager_removes_stale_sessions(tmp_path: Path) -> None:
    manager = NotebookSessionManager(cwd=tmp_path)
    session = manager.create(mode="browser")
    session._last_activity -= 10  # noqa: SLF001 - lifecycle boundary under test.

    assert manager.cleanup_stale(max_age=1) == 1
    with pytest.raises(KeyError, match="not found"):
        manager.get(session.id)


def test_notebook_api_defaults_to_browser_and_lists_host_python(tmp_path: Path) -> None:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
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


def test_notebook_api_requires_token_and_same_origin(tmp_path: Path) -> None:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        access_token="notebook-secret",
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        unauthorized = client.get("/api/notebook/runtimes")
        cross_origin = client.get(
            "/api/notebook/runtimes",
            headers={
                "Authorization": "Bearer notebook-secret",
                "Origin": "https://example.com",
            },
        )
        authorized = client.get(
            "/api/notebook/runtimes",
            headers={"Authorization": "Bearer notebook-secret"},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["cross-origin-resource-policy"] == "same-origin"
    assert cross_origin.status_code == 403
    assert authorized.status_code == 200


def test_notebook_api_rejects_non_loopback_host_without_token(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path)

    with TestClient(app, base_url="http://notebook.example") as client:
        response = client.get("/api/notebook/runtimes")

    assert response.status_code == 403
    assert "not loopback" in response.json()["error"]


def test_notebook_api_rejects_large_source_and_request_body(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        source = "x" * 1_000_001
        validation = client.post(
            "/api/lsp/diagnostics",
            json={"source": source},
        )
        too_large = client.post(
            "/api/lsp/diagnostics",
            content=b"x" * 2_000_001,
            headers={"Content-Type": "application/json"},
        )

    assert validation.status_code == 422
    assert too_large.status_code == 413


def test_notebook_lsp_routes_expose_diagnostics_signature_and_definition(
    tmp_path: Path,
) -> None:
    app = create_app(cwd=tmp_path)
    source = "@add(left: int, right: int) -> int:\n  = `left + right`\n\n= <add(1, 2)>"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        diagnostics = client.post("/api/lsp/diagnostics", json={"source": source})
        signature = client.post(
            "/api/lsp/signature",
            json={"source": source, "line": 3, "character": 7},
        )
        definition = client.post(
            "/api/lsp/definition",
            json={"source": source, "line": 3, "character": 4},
        )

    assert diagnostics.status_code == 200
    assert diagnostics.json()["diagnostics"] == []
    assert signature.status_code == 200
    assert signature.json()["signature"]["signatures"][0]["label"].startswith("@add(")
    assert definition.status_code == 200
    assert definition.json()["definition"]["range"]["start"]["line"] == 0


def test_notebook_lsp_routes_expose_completion_references_and_rename(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path)
    source = "[value: int] = `1`\n[value] := `value + 1`\n= <value>"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        completion = client.post(
            "/api/lsp/completion",
            json={"source": source, "line": 2, "character": 4},
        )
        references = client.post(
            "/api/lsp/references",
            json={
                "source": source,
                "line": 2,
                "character": 4,
                "includeDeclaration": True,
            },
        )
        prepared = client.post(
            "/api/lsp/prepare-rename",
            json={"source": source, "line": 2, "character": 4},
        )
        renamed = client.post(
            "/api/lsp/rename",
            json={"source": source, "line": 2, "character": 4, "newName": "count"},
        )

    assert completion.status_code == 200
    assert "value" in {item["label"] for item in completion.json()["items"]}
    assert len(references.json()["references"]) == 3
    assert prepared.json()["rename"]["range"]["start"]["line"] == 2
    assert len(renamed.json()["edits"]) == 3


def test_notebook_host_package_api_lists_and_validates_requirements(tmp_path: Path) -> None:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        python_id = client.get("/api/notebook/runtimes").json()["host"][0]["id"]
        created = client.post(
            "/api/notebook/sessions",
            json={"mode": "host", "pythonId": python_id},
        ).json()
        packages = client.get(f"/api/notebook/sessions/{created['sessionId']}/packages")
        rejected = client.post(
            f"/api/notebook/sessions/{created['sessionId']}/packages/install",
            json={"packages": ["--index-url=https://example.invalid"]},
        )

    assert packages.status_code == 200
    assert any(item["name"].lower() == "kedi" for item in packages.json()["packages"])
    events = [json.loads(line) for line in rejected.text.splitlines()]
    assert events[-1]["ok"] is False
    assert "Invalid package requirement" in events[-1]["error"]


def test_pyright_diagnostics_map_embedded_python_back_to_kedi_source() -> None:
    pyright = PyrightServer(timeout=10)
    try:
        diagnostics = pyright.diagnostics("```\nx: int = 'bad'\n```")
    finally:
        pyright.close()

    assert len(diagnostics) == 1
    assert diagnostics[0]["source"] == "pyright"
    assert diagnostics[0]["code"] == "reportAssignmentType"
    assert diagnostics[0]["range"]["start"] == {"line": 1, "character": 9}


def test_pyright_completion_references_and_rename_map_embedded_python() -> None:
    source = "```\nmessage = 'hello'\nprint(message)\n```"
    pyright = PyrightServer(timeout=10)
    try:
        completion = pyright.completion(source, 2, 3)
        references = pyright.references(
            source,
            2,
            8,
            include_declaration=True,
        )
        prepared = pyright.prepare_rename(source, 2, 8)
        edits = pyright.rename(source, 2, 8, "greeting")
    finally:
        pyright.close()

    assert completion is not None
    assert any(item["label"] == "print" for item in completion)
    assert references is not None and len(references) == 2
    assert prepared is not None and prepared["range"]["start"]["line"] == 2
    assert edits is not None and len(edits) == 2


def test_non_loopback_cli_requires_access_token(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        serve_cli(["--host", "0.0.0.0", "--cwd", str(tmp_path), "--no-open"])


def test_notebook_api_executes_host_terminal_cell(tmp_path: Path) -> None:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
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
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/notebook/")
        script = client.get("/notebook/notebook.js")

    assert response.status_code == 200
    assert "Kedi Notebook" in response.text
    assert 'id="runtime-select"' in response.text
    assert '<option value="terminal">Terminal</option>' in response.text
    assert 'id="theme-toggle"' not in response.text
    assert script.status_code == 200
    assert "renderCell(cell, cellPosition + 1)" in script.text
    assert 'select.className = "cell-kind-select"' in script.text
    assert 'cell.hidden ? "eye" : "eye-off"' in script.text
    assert "cell.executionCount" not in script.text
