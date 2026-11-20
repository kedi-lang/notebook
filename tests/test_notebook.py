from __future__ import annotations

import asyncio
import base64
import builtins
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from kedi_notebook import NotebookSecretStore, NotebookSessionManager, discover_host_pythons
from kedi_notebook import host_environment as host_environment_module
from kedi_notebook import runtime as notebook_runtime
from kedi_notebook.bridge import BridgeRun
from kedi_notebook.host_environment import HostEnvironmentManager, PreparedHostEnvironment
from kedi_notebook.pyright import PyrightServer
from kedi_notebook.sandbox_worker import handle_request
from kedi_notebook.server import _notebook_server, create_app, serve_cli


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
        "OPENROUTER_API_KEY=not-a-real-key\nKEDI_ADAPTER_MODEL=openrouter:test/imported-model\n",
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
            json={"cellId": "resume", "source": "> show: `value + 22`"},
        )

    assert captured.status_code == 200
    assert "do-not-export-this" not in captured.text
    assert resumed.status_code == 200
    assert resumed.json()["stdout"] == "42\n"


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


@pytest.mark.parametrize(
    ("version", "releaselevel", "message"),
    [
        ([3, 14, 0], "candidate", "requires a final Python release"),
        ([3, 10, 20], "final", "requires Python 3.11 or newer"),
    ],
)
def test_discover_host_pythons_rejects_unsupported_explicit_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: list[int],
    releaselevel: str,
    message: str,
) -> None:
    executable = tmp_path / "python3.14"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    original_metadata = notebook_runtime._python_metadata

    def metadata(path: Path) -> dict[str, object] | None:
        if path == executable:
            return {"version": version, "releaselevel": releaselevel}
        return original_metadata(path)

    monkeypatch.setattr(notebook_runtime, "_python_metadata", metadata)

    with pytest.raises(ValueError, match=message):
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


def test_host_notebook_installs_only_missing_provider_without_replaying_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai.models.test import TestModel

    from kedi_notebook import model_dependencies

    installed: list[str] = []
    model = TestModel(call_tools=[], custom_output_text="HELLO")

    def infer(name: str) -> TestModel:
        assert name == "openrouter:example/model"
        if not installed:
            raise ImportError("Install pydantic-ai-slim[openai]")
        return model

    monkeypatch.setattr(model_dependencies, "infer_model", infer)
    monkeypatch.setattr(model_dependencies, "_install_provider_extra", installed.append)
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        result = session.execute(
            cell_id="provider",
            source="""```
counter = globals().get("counter", 0) + 1
print("before-model", counter)
```
> model: openrouter:example/model
[output] << Reply with HELLO.
> show: <output>
> show: <counter>
""",
        )
    finally:
        manager.close_all()

    assert result["ok"] is True
    assert result["stdout"] == "before-model 1\nHELLO\n1\n"
    assert installed == ["openai"]


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
        second = session.execute(cell_id="result", source="> show: `value + 2`")
    finally:
        manager.close_all()

    assert first["ok"] is True
    assert first["executionCount"] == 1
    assert first["stdout"] == "host ready\n"
    assert second["ok"] is True
    assert second["executionCount"] == 2
    assert second["stdout"] == "42\n"


# Inline Python calls these as it would in normal Kedi; each pair is (source, stdout).
PROCEDURE_CALL_CELLS = [
    ("[base: int] = `40`\n@add_two() -> int:\n  = `base + 2`\n", ""),
    ("> show: `[add_two() for _ in range(2)]`", "[42, 42]\n"),
    (
        "@greet(name: str) -> str:\n  = Merhaba <name>\n\n> show: `greet('Kedi') + '!'`",
        "Merhaba Kedi!\n",
    ),
    # The procedure's writes stay; the caller writes back only the bindings it changed.
    (
        "[count: int] = `0`\n@bump() -> int:\n  [count] := `count + 1`\n  = `count`\n\n"
        "> show: `bump() + bump()`\n> show: `count`",
        "3\n2\n",
    ),
    # Output keeps its order, and the caller keeps its own Python names across the call.
    (
        "@loud() -> int:\n  `print('inside')`\n  = `1`\n\n"
        "```\nlocal = 5\nprint('before')\nloud()\nprint('after', local, base)\n```",
        "before\ninside\nafter 5 40\n",
    ),
    (
        "@boom() -> int:\n  = `1 // 0`\n\n"
        "```\ntry:\n    boom()\nexcept Exception as error:\n    print(type(error).__name__)\n```",
        "KediProcedureError\n",
    ),
]


def test_host_notebook_python_calls_kedi_procedures(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        results = [
            session.execute(cell_id=f"cell-{index}", source=source)
            for index, (source, _) in enumerate(PROCEDURE_CALL_CELLS)
        ]
        failed = session.execute(cell_id="failed", source="> show: `boom()`")
        after = session.execute(cell_id="after", source="> show: `add_two()`")
    finally:
        manager.close_all()

    assert [(result["ok"], result["stdout"]) for result in results] == [
        (True, stdout) for _, stdout in PROCEDURE_CALL_CELLS
    ]
    # An unhandled failure is the Kedi error itself, with the procedure in its trace.
    assert failed["ok"] is False
    assert "in procedure boom" in failed["error"]
    # Python 3.11 words the message differently.
    assert "ZeroDivisionError:" in failed["error"]
    assert failed["diagnostic"]["line"] == 1
    assert (after["ok"], after["stdout"]) == (True, "42\n")


def test_host_notebook_input_fails_fast_and_keeps_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reading the bridge's own input used to block until the execution timeout.
    monkeypatch.setattr(notebook_runtime, "_EXECUTION_TIMEOUT", 20)
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    child = (
        "import subprocess, sys\n"
        "code = 'import sys; print(len(sys.stdin.read()))'\n"
        "print(subprocess.run([sys.executable, '-c', code], capture_output=True, text=True).stdout)"
    )
    try:
        prompt = session.execute(cell_id="prompt", source="```\nname = input('Name: ')\n```")
        reader = session.execute(cell_id="child", source=f"```\n{child}\n```")
        after = session.execute(cell_id="after", source="```\nprint('still here')\n```")
    finally:
        manager.close_all()

    assert prompt["ok"] is False
    assert "EOFError: Standard input is not available" in prompt["error"]
    assert (reader["ok"], reader["stdout"]) == (True, "0\n\n")
    assert (after["ok"], after["stdout"]) == (True, "still here\n")


def test_host_notebook_imports_filesystem_and_reads_written_file(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        result = session.execute(
            cell_id="filesystem",
            source=(
                "> import: filesystem\n"
                '> show: <write_text_file(`"note.txt"`, `"notebook value"`)>\n'
                '> show: <read_text_file(`"note.txt"`)>'
            ),
        )
    finally:
        manager.close_all()

    assert result["ok"] is True
    assert str(result["stdout"]).endswith("notebook value\n")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "notebook value"


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
            source="`items.append(3)`\n> show: `items`",
        )
    finally:
        stop.set()
        manager.close_all()
        worker.join(timeout=2)

    assert first["ok"] is True
    assert second["stdout"] == "[1, 2, 3]\n"


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
        result = session.execute(cell_id="broken", source="> show: `1 / 0`")
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
            source="> show: `(__import__('time').sleep(5), 1)[1]`",
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
                source="> show: `(__import__('time').sleep(30), 1)[1]`",
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


@pytest.mark.parametrize("parallel", [False, True])
def test_interrupt_cancels_pending_model_and_allows_new_session(
    tmp_path: Path, parallel: bool
) -> None:
    from kedi.agent_adapter.adapters.pydantic import PydanticAdapter
    from kedi.engine import SequentialEngine, ThreadEngine
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.models.function import FunctionModel

    entered, cleaned = threading.Event(), threading.Event()

    async def respond(messages, info) -> ModelResponse:
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleaned.set()
        raise AssertionError("The cancelled model must not produce a result")

    engine = ThreadEngine(max_workers=1) if parallel else SequentialEngine()
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
        interactive_options={"adapter": PydanticAdapter(FunctionModel(respond)), "engine": engine},
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(
            session.execute(
                cell_id="model", source=">> The value is [value: str].\n> show: <value>"
            )
        )
    )
    closer = threading.Thread(target=lambda: manager.close(session.id))
    worker.start()
    try:
        assert entered.wait(5)
        closer.start()
        closer.join(timeout=5)
        worker.join(timeout=5)
        assert not closer.is_alive()
        assert not worker.is_alive()
        assert cleaned.is_set()
        assert result["ok"] is False
        assert result["runtimeReset"] is True
        assert "interrupted" in str(result["error"])
        fresh = manager.create(mode="host", python_id=manager.pythons[0].id)
        rerun = fresh.execute(cell_id="next", source="> show: `41 + 1`")
        assert rerun["ok"] is True
        assert rerun["stdout"] == "42\n"
    finally:
        manager.close_all()
        worker.join(timeout=35)
        if closer.ident is not None:
            closer.join(timeout=35)
        engine.shutdown()


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
            source="> show: `'x' * 250000`",
        )
    finally:
        manager.close_all()

    assert len(str(terminal["stdout"])) < 201_000
    assert str(terminal["stdout"]).endswith("[output truncated by Kedi Notebook]")
    assert str(value["stdout"]).endswith("[output truncated by Kedi Notebook]")
    assert len(str(value["stdout"])) < 201_000


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
    source = "@add(left: int, right: int) -> int:\n  = `left + right`\n\n> show: <add(1, 2)>"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        diagnostics = client.post("/api/lsp/diagnostics", json={"source": source})
        signature = client.post(
            "/api/lsp/signature",
            json={"source": source, "line": 3, "character": 13},
        )
        definition = client.post(
            "/api/lsp/definition",
            json={"source": source, "line": 3, "character": 10},
        )

    assert diagnostics.status_code == 200
    assert diagnostics.json()["diagnostics"] == []
    assert signature.status_code == 200
    assert signature.json()["signature"]["signatures"][0]["label"].startswith("@add(")
    assert definition.status_code == 200
    assert definition.json()["definition"]["range"]["start"]["line"] == 0


def test_notebook_lsp_routes_expose_completion_references_and_rename(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path)
    source = "[value: int] = `1`\n[value] := `value + 1`\n> show: <value>"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        completion = client.post(
            "/api/lsp/completion",
            json={"source": source, "line": 2, "character": 10},
        )
        references = client.post(
            "/api/lsp/references",
            json={
                "source": source,
                "line": 2,
                "character": 10,
                "includeDeclaration": True,
            },
        )
        prepared = client.post(
            "/api/lsp/prepare-rename",
            json={"source": source, "line": 2, "character": 10},
        )
        renamed = client.post(
            "/api/lsp/rename",
            json={"source": source, "line": 2, "character": 10, "newName": "count"},
        )

    assert completion.status_code == 200
    assert "value" in {item["label"] for item in completion.json()["items"]}
    # The use inside `value + 1` is the same binding, so it is found and renamed too.
    expected = [(0, 1), (1, 1), (1, 12), (2, 9)]
    assert sorted(_range_starts(references.json()["references"])) == expected
    assert prepared.json()["rename"]["range"]["start"]["line"] == 2
    assert sorted(_range_starts(renamed.json()["edits"])) == expected
    assert {edit["newText"] for edit in renamed.json()["edits"]} == {"count"}


def _range_starts(items: list[dict]) -> list[tuple[int, int]]:
    return [(item["range"]["start"]["line"], item["range"]["start"]["character"]) for item in items]


def test_notebook_directive_completion_preserves_source_edits(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/lsp/completion",
            json={"source": "  > mo", "line": 0, "character": 6},
        )
        assert response.status_code == 200
        item = next(item for item in response.json()["items"] if item["label"] == "model")
        assert item["detail"] == "Select the model for this lexical scope"
        assert item["filterText"] == "> model"
        assert item["textEdit"] == {
            "range": {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 6}},
            "newText": "> model: ",
        }
        for source in ("> model:", "> model: ", "`", "``", "```"):
            response = client.post(
                "/api/lsp/completion",
                json={"source": source, "line": 0, "character": len(source)},
            )
            assert response.status_code == 200
            assert response.json()["items"] == []


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


def test_pyright_counts_template_reads_as_uses_of_bindings() -> None:
    source = (
        "@greet(name: str) -> str:\n"
        "  = Hello <name>\n"
        "\n"
        "[count: int] = `3`\n"
        ">> There are <count> items.\n"
        "[who: str] = `'Kedi'`\n"
        "[literal: str] = `'unused'`\n"
        "= <greet(<who>)> <greet(literal)>\n"
    )
    pyright = PyrightServer(timeout=10)
    try:
        diagnostics = pyright.diagnostics(source)
    finally:
        pyright.close()

    # `<greet(literal)>` passes the text "literal", so only that binding is unused.
    assert [diagnostic["message"] for diagnostic in diagnostics] == [
        'Variable "literal" is not accessed'
    ]
    assert diagnostics[0]["range"]["start"]["line"] == 6


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


def test_pyright_tracks_only_its_virtual_workspace_and_can_restart() -> None:
    pyright = PyrightServer(timeout=10)
    previous_uri = None
    for _ in range(2):
        try:
            assert pyright.diagnostics("```\nvalue: int = 1\n```") == []
            assert pyright._workspace is not None
            workspace = Path(pyright._workspace.name).resolve()
            assert (workspace / "embedded.py").read_text() == ""
            assert pyright._uri == (workspace / "embedded.py").as_uri()
            assert pyright._uri != previous_uri
            previous_uri = pyright._uri
        finally:
            pyright.close()
        assert not workspace.exists()


def test_non_loopback_cli_requires_access_token(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        serve_cli(["--host", "0.0.0.0", "--cwd", str(tmp_path), "--no-open"])


def test_notebook_server_exit_does_not_wait_for_a_bridge_long_poll(tmp_path: Path) -> None:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = _notebook_server(app, uvicorn.Config(app, log_level="warning"))
    serving = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    serving.start()
    polled: list[httpx.Response] = []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        created = httpx.post(f"{base_url}/api/notebook/sessions", json={"mode": "browser"})
        session_url = f"{base_url}/api/notebook/sessions/{created.json()['sessionId']}"
        request_url = f"{session_url}/bridge/request"
        polling = threading.Thread(target=lambda: polled.append(httpx.get(request_url, timeout=30)))
        polling.start()
        time.sleep(0.5)
    finally:
        started = time.monotonic()
        server.should_exit = True
        serving.join(timeout=30)
    polling.join(timeout=30)

    # The bridge holds this request for 20 seconds unless shutdown closes its session first.
    assert time.monotonic() - started < 5
    assert polled[0].json() == {"ok": True, "request": None}


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


def test_media_results_carry_a_preview_for_images_and_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kedi.media import Audio, Document, Image

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    image = notebook_runtime._json_result(Image.from_bytes(png, "image/png"))
    assert image == {
        "kind": "media",
        "type": "Image",
        "value": Image.from_bytes(png, "image/png").label,
        "mediaType": "image/png",
        "data": base64.b64encode(png).decode(),
    }
    clip = notebook_runtime._json_result(Audio.from_bytes(b"RIFF....WAVE", "audio/wav"))
    assert clip is not None and clip["mediaType"] == "audio/wav"

    # A label only: no browser preview, a URL, or bytes over the preview limit.
    pdf = notebook_runtime._json_result(Document.from_bytes(b"%PDF-1.4\n", "application/pdf"))
    assert pdf == {"kind": "media", "type": "Document", "value": pdf["value"]}
    linked = notebook_runtime._json_result(Image.from_url("https://example.com/cat.png"))
    assert linked is not None and "data" not in linked
    monkeypatch.setattr(notebook_runtime, "_MEDIA_PREVIEW_LIMIT", 10)
    large = notebook_runtime._json_result(Image.from_bytes(png, "image/png"))
    assert large is not None and "data" not in large and large["value"].startswith("<image")
    assert notebook_runtime._json_result({"plain": 1}) == {
        "kind": "json",
        "type": "dict",
        "value": {"plain": 1},
    }


def test_host_python_media_values_reach_kedi_and_preview(tmp_path: Path) -> None:
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/"
        "pLvAAAAAElFTkSuQmCC"
    )
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        # image() runs in the worker Python; its Image crosses to Kedi with its type.
        shown = session.execute(
            cell_id="load",
            source=(
                "> import: media\n"
                f'[photo: Image] = `image("data:image/png;base64,{png}")`\n'
                "> show: before\n"
                "> show: `photo`"
            ),
        )
        # The stored value crosses back: save() runs in the worker.
        saved = session.execute(
            cell_id="save",
            source="> import: media\n[path] = <save(`photo`, out/cat.png)>\n> show: <path>",
        )
        named = session.execute(cell_id="label", source="> show: `photo.label`")
    finally:
        manager.close_all()

    assert shown["ok"] is True and shown["stdout"] == "before\n"
    assert shown["result"] == {
        "kind": "displays",
        "items": [
            {
                "kind": "media",
                "type": "Image",
                "value": "<image data · image/png · 69 B>",
                "mediaType": "image/png",
                "data": png,
            }
        ],
    }
    assert saved["ok"] is True and str(saved["stdout"]).strip().endswith("out/cat.png")
    assert (tmp_path / "out" / "cat.png").read_bytes() == base64.b64decode(png)
    assert named["stdout"] == "<image data · image/png · 69 B>\n"


def test_media_results_without_kedi_media_fall_back_to_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def missing_media(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kedi.media":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_media)
    assert notebook_runtime._json_result([1]) == {"kind": "json", "type": "list", "value": [1]}


# Audit #215 item 1: process-control names must never become server secrets.
BLOCKED_PROCESS_CONTROL_NAMES = [
    "PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONHOME",
    "PYTHONBREAKPOINT",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "UV_INDEX_URL",
    "UV_PYTHON",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "NODE_OPTIONS",
    "NODE_PATH",
    "VIRTUAL_ENV",
    "HOME",
    "SHELL",
    "BASH_ENV",
    "ENV",
]


@pytest.mark.parametrize("name", BLOCKED_PROCESS_CONTROL_NAMES)
def test_notebook_secret_api_rejects_process_control_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.delenv(name, raising=False)
    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.put(
            "/api/notebook/secrets",
            json={"name": name, "value": "injected"},
        )

    assert response.status_code == 400
    assert name in response.json()["error"]
    assert "controls the notebook process" in response.json()["error"]
    assert name not in os.environ
    assert app.state.notebook_secret_store.names == ()


def test_notebook_secret_api_still_accepts_provider_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.put(
            "/api/notebook/secrets",
            json={"name": "OPENAI_API_KEY", "value": "not-a-real-key"},
        )

    assert response.status_code == 200
    assert os.environ["OPENAI_API_KEY"] == "not-a-real-key"


def test_notebook_secret_api_import_dotenv_rejects_process_control_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "import.env").write_text(
        "PYTHONPATH=/tmp/evil\nOPENAI_API_KEY=not-a-real-key\n",
        encoding="utf-8",
    )
    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "import.env"},
        )

    assert response.status_code == 400
    assert "PYTHONPATH" in response.json()["error"]
    assert "PYTHONPATH" not in os.environ
    # The import is atomic: a blocked name rejects the whole file.
    assert app.state.notebook_secret_store.names == ()
    assert "OPENAI_API_KEY" not in os.environ


def test_notebook_secret_store_drops_process_control_names_on_disk_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "secrets": {
                    "PYTHONPATH": "/tmp/evil",
                    "OPENAI_API_KEY": "not-a-real-key",
                },
            }
        ),
        encoding="utf-8",
    )

    store = NotebookSecretStore(path)
    store.apply_to_environment()

    assert store.names == ("OPENAI_API_KEY",)
    assert "PYTHONPATH" not in os.environ
    assert os.environ["OPENAI_API_KEY"] == "not-a-real-key"


# Audit #215 item 2: dotenv imports must stay inside the notebook directory.
def test_notebook_secret_api_import_dotenv_rejects_paths_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEAKED_VALUE", raising=False)
    monkeypatch.delenv("OUTSIDE_VALUE", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaked.env").write_text("OUTSIDE_VALUE=from-outside\n", encoding="utf-8")
    (project / ".env").write_text("LEAKED_VALUE=from-dotenv\n", encoding="utf-8")
    app = create_app(
        cwd=project,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        traversal = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "../outside/leaked.env"},
        )
        assert traversal.status_code == 400
        assert "inside the notebook directory" in traversal.json()["error"]
        # The file outside the cwd was never read.
        assert "OUTSIDE_VALUE" not in os.environ

        absolute = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "/etc/passwd"},
        )
        assert absolute.status_code == 400
        home = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "~/.env"},
        )
        assert home.status_code == 400

        inside = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "./.env"},
        )
        assert inside.status_code == 200
        assert inside.json()["imported"] == ["LEAKED_VALUE"]
        assert os.environ["LEAKED_VALUE"] == "from-dotenv"


# Audit #215 item 3: a chunked body must hit the same size limit as one with
# Content-Length.
def test_notebook_api_rejects_chunked_body_over_limit(tmp_path: Path) -> None:
    app = create_app(cwd=tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = _notebook_server(app, uvicorn.Config(app, log_level="warning"))
    serving = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    serving.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started

        def oversized_chunks() -> Iterator[bytes]:
            for _ in range(100):
                yield b"x" * 100_000  # 10 MB total, no Content-Length.

        # trust_env=False: the sandbox sets proxy variables httpx cannot parse.
        with httpx.Client(trust_env=False, timeout=30) as http:
            oversized = http.post(
                f"{base_url}/api/lsp/diagnostics",
                content=oversized_chunks(),
                headers={"Content-Type": "application/json"},
            )
            assert oversized.status_code == 413

            small = http.post(
                f"{base_url}/api/lsp/diagnostics",
                content=iter([b'{"source": ""}']),
                headers={"Content-Type": "application/json"},
            )
            assert small.status_code == 200
    finally:
        server.should_exit = True
        serving.join(timeout=10)


# Audit #215 item 6: non-finite floats are shown as text, not a failed cell.
def test_json_result_shows_nan_and_infinity_as_text() -> None:
    assert notebook_runtime._json_result(float("nan")) == {
        "kind": "json",
        "type": "float",
        "value": "NaN",
    }
    assert notebook_runtime._json_result(float("inf")) == {
        "kind": "json",
        "type": "float",
        "value": "Infinity",
    }
    assert notebook_runtime._json_result(float("-inf")) == {
        "kind": "json",
        "type": "float",
        "value": "-Infinity",
    }
    nested = notebook_runtime._json_result(
        {"a": float("nan"), "b": [1, float("inf"), {"c": float("-inf")}]}
    )
    assert nested == {
        "kind": "json",
        "type": "dict",
        "value": {"a": "NaN", "b": [1, "Infinity", {"c": "-Infinity"}]},
    }
    for value in (
        float("nan"),
        float("inf"),
        {"a": float("nan")},
        [float("-inf")],
    ):
        result = notebook_runtime._json_result(value)
        assert result is not None
        # The payload must survive Starlette's strict JSON encoding.
        json.dumps(result, allow_nan=False)


def test_json_result_keeps_repr_fallback_for_cyclic_values() -> None:
    cyclic: dict[str, object] = {"value": float("nan")}
    cyclic["self"] = cyclic

    result = notebook_runtime._json_result(cyclic)

    assert result is not None and result["kind"] == "repr"
    assert "nan" in str(result["value"])
    json.dumps(result, allow_nan=False)


def test_notebook_api_returns_nan_cell_result_instead_of_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(cwd=tmp_path)
    session = app.state.notebook_manager.create(mode="browser")
    original_execute = session._session.execute

    def execute(source: str, *, source_name: str | None = None) -> object:
        # Interactive cells cannot return a top-level value (root returns are
        # rejected), so the session result is stubbed here: the layer under
        # test is result serialization, not Kedi semantics.
        if "nan" in source:
            return float("nan")
        if "inf" in source:
            return float("inf")
        return original_execute(source, source_name=source_name)

    monkeypatch.setattr(session._session, "execute", execute)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        nan = client.post(
            f"/api/notebook/sessions/{session.id}/cells/execute",
            json={"cellId": "nan", "source": "> show: `float('nan')`"},
        )
        inf = client.post(
            f"/api/notebook/sessions/{session.id}/cells/execute",
            json={"cellId": "inf", "source": "> show: `float('inf')`"},
        )

    assert nan.status_code == 200
    assert nan.json()["ok"] is True
    assert nan.json()["result"] == {"kind": "json", "type": "float", "value": "NaN"}
    assert inf.status_code == 200
    assert inf.json()["result"] == {"kind": "json", "type": "float", "value": "Infinity"}


# Audit #215 item 7: a forged protocol reply written to fd 1 must be ignored.
def test_host_worker_ignores_forged_fd1_protocol_replies(tmp_path: Path) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        forged = session.execute(
            cell_id="forge",
            source=(
                "```\n"
                "import json, os\n"
                "os.write(1, (\n"
                "    '__KEDI_NOTEBOOK_RESPONSE__'\n"
                "    + json.dumps({'ok': True, 'result': 'FORGED-BY-USER-CODE', 'env': {}})\n"
                "    + '\\n'\n"
                ").encode())\n"
                "```"
            ),
        )
        after = session.execute(cell_id="after", source="> show: `1 + 1`")
    finally:
        manager.close_all()

    assert forged["ok"] is True
    assert "FORGED-BY-USER-CODE" not in json.dumps(forged)
    # The next request must get its own real response, not the forged one.
    assert after["ok"] is True
    assert after["stdout"] == "2\n"


# Audit #215 item 9: a timed-out or abandoned host process must not leak pump
# threads, pipe FDs or zombies.
def _process_resources() -> tuple[set[threading.Thread], int]:
    return set(threading.enumerate()), len(os.listdir("/proc/self/fd"))


def _assert_resources_released(
    before_threads: set[threading.Thread],
    before_fds: int,
    *,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        threads, fds = _process_resources()
        if threads <= before_threads and fds <= before_fds:
            return
        time.sleep(0.05)
    threads, fds = _process_resources()
    assert threads <= before_threads, f"leaked threads: {threads - before_threads}"
    assert fds <= before_fds, f"leaked FDs: {fds} > {before_fds}"


def test_stream_host_process_timeout_reaps_grandchild_holding_pipe(
    tmp_path: Path,
) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        before_threads, before_fds = _process_resources()
        started = time.monotonic()
        events = session._stream_host_process(  # noqa: SLF001 - leak under test.
            ["/bin/sh", "-lc", "sleep 30 & echo started"],
            command="sleep 30 & echo started",
            timeout=2,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            list(events)
        # The timeout is 2s; cleanup must not wait out the 30s grandchild.
        assert time.monotonic() - started < 15
        _assert_resources_released(before_threads, before_fds)
    finally:
        manager.close_all()


def test_stream_host_process_abandoned_generator_releases_threads_and_fds(
    tmp_path: Path,
) -> None:
    manager = NotebookSessionManager(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PASSTHROUGH_HOST_ENVIRONMENT,
    )
    session = manager.create(mode="host", python_id=manager.pythons[0].id)
    try:
        before_threads, before_fds = _process_resources()
        events = session._stream_host_process(  # noqa: SLF001 - leak under test.
            [
                sys.executable,
                "-u",
                "-c",
                "for i in range(100000): print('x' * 200)",
            ],
            command="fast producer",
            timeout=30,
        )
        first = next(events)
        assert first["type"] == "output"
        # Let the fast producer fill the bounded output queue first, so the
        # pumps are blocked in put() when the consumer goes away.
        time.sleep(0.5)
        events.close()  # Consumer goes away mid-stream.
        _assert_resources_released(before_threads, before_fds)
    finally:
        manager.close_all()
