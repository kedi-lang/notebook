"""Regression tests for kedi-lang/kedi#215 (notebook repo).

Each test fails on the base commit (9aa4d50) and passes with the fix.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kedi_notebook.secrets import NotebookSecretStore
from kedi_notebook.server import create_app

PROCESS_CONTROL_NAMES = [
    "PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONHOME",
    "PYTHONWARNINGS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "UV_INDEX_URL",
    "UV_PYTHON",
    "PIP_INDEX_URL",
    "PIP_CONFIG_FILE",
    "NODE_OPTIONS",
    "NODE_PATH",
    "VIRTUAL_ENV",
    "HOME",
    "SHELL",
    "BASH_ENV",
    "ENV",
]


@pytest.mark.parametrize("name", PROCESS_CONTROL_NAMES)
def test_secret_store_rejects_process_control_names(tmp_path: Path, name: str) -> None:
    store = NotebookSecretStore(tmp_path / "secrets.json")
    saved = os.environ.get(name)
    try:
        with pytest.raises(ValueError, match="controls the notebook process"):
            store.set(name, "audit215-value")
        assert name not in store.names
        assert os.environ.get(name) != "audit215-value"
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


def test_secret_api_rejects_process_control_name_and_keeps_provider_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    secret_store = NotebookSecretStore(tmp_path / "secrets.json")
    app = create_app(cwd=tmp_path, secret_store=secret_store)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        rejected = client.put(
            "/api/notebook/secrets",
            json={"name": "PYTHONPATH", "value": "/tmp/audit215"},
        )
        accepted = client.put(
            "/api/notebook/secrets",
            json={"name": "OPENAI_API_KEY", "value": "not-a-real-key"},
        )

    assert rejected.status_code == 400
    assert "controls the notebook process" in rejected.json()["error"]
    assert accepted.status_code == 200
    assert accepted.json()["configured"] == ["OPENAI_API_KEY"]
    assert os.environ["OPENAI_API_KEY"] == "not-a-real-key"
    assert os.environ.get("PYTHONPATH") != "/tmp/audit215"


def test_secret_store_rejects_process_control_names_from_dotenv_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    (tmp_path / "mixed.env").write_text(
        "OPENAI_API_KEY=not-a-real-key\nLD_PRELOAD=/tmp/audit215.so\n",
        encoding="utf-8",
    )
    secret_store = NotebookSecretStore(tmp_path / "secrets.json")
    app = create_app(cwd=tmp_path, secret_store=secret_store)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        imported = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "mixed.env"},
        )

    assert imported.status_code == 400
    assert "controls the notebook process" in imported.json()["error"]
    assert "OPENAI_API_KEY" not in secret_store.names
    assert os.environ.get("LD_PRELOAD") != "/tmp/audit215.so"


def test_secret_store_refuses_to_load_process_control_names_from_disk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(
        '{"version": 1, "secrets": {"PYTHONPATH": "/tmp/audit215"}}',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="invalid entries"):
        NotebookSecretStore(path)


def test_dotenv_import_rejects_paths_outside_cwd(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaked.env").write_text("LEAKED_MARKER=traversed\n", encoding="utf-8")
    (project / ".env").write_text("LOCAL_MARKER=inside\n", encoding="utf-8")
    secret_store = NotebookSecretStore(tmp_path / "secrets.json")
    app = create_app(cwd=project, secret_store=secret_store)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        dotdot = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "../outside/leaked.env"},
        )
        absolute = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "/etc/passwd"},
        )
        local = client.post(
            "/api/notebook/secrets/import-dotenv",
            json={"path": "./.env"},
        )

    assert dotdot.status_code == 400
    assert "inside the notebook directory" in dotdot.json()["error"]
    assert absolute.status_code == 400
    assert "inside the notebook directory" in absolute.json()["error"]
    assert local.status_code == 200
    assert local.json()["imported"] == ["LOCAL_MARKER"]
    assert "LEAKED_MARKER" not in secret_store.names


def _run_live_notebook_server(tmp_path: Path, app=None):
    import socket
    import threading
    import time

    import uvicorn

    app = app or create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"


def test_chunked_body_over_limit_gets_413(tmp_path: Path) -> None:
    import httpx

    server, thread, base = _run_live_notebook_server(tmp_path)
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            oversized = client.post(
                f"{base}/api/notebook/sessions",
                content=_chunked_json({"mode": "browser", "pad": "B" * 10_000_000}),
                headers={"Content-Type": "application/json"},
            )
            within_limit = client.post(
                f"{base}/api/notebook/sessions",
                content=_chunked_json({"mode": "browser"}),
                headers={"Content-Type": "application/json"},
            )
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert oversized.status_code == 413
    assert oversized.json()["error"] == "Request body is too large"
    # A small chunked body still reaches the app (replay works).
    assert within_limit.status_code == 200
    assert within_limit.json()["mode"] == "browser"


def _chunked_json(payload: dict):
    """A chunked (no Content-Length) JSON request body."""
    import json

    body = json.dumps(payload).encode()
    for index in range(0, len(body), 65_536):
        yield body[index : index + 65_536]


def test_json_result_shows_non_finite_floats_as_text() -> None:
    from kedi_notebook.runtime import _json_result

    assert _json_result(float("nan")) == {
        "kind": "json",
        "type": "float",
        "value": "NaN",
    }
    assert _json_result(float("inf")) == {
        "kind": "json",
        "type": "float",
        "value": "Infinity",
    }
    assert _json_result(float("-inf")) == {
        "kind": "json",
        "type": "float",
        "value": "-Infinity",
    }
    assert _json_result({"value": float("nan"), "items": [float("inf")]}) == {
        "kind": "json",
        "type": "dict",
        "value": {"value": "NaN", "items": ["Infinity"]},
    }
    assert _json_result(1.5) == {"kind": "json", "type": "float", "value": 1.5}


def test_execute_cell_returns_nan_result_instead_of_failing(
    tmp_path: Path,
) -> None:
    from kedi_notebook.runtime import _json_result

    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )
    manager = app.state.notebook_manager

    class StubSession:
        id = "stub-nan"

        def touch(self) -> None:
            pass

        def close(self) -> None:
            pass

        def execute(self, *, cell_id, source):
            return {
                "ok": True,
                "cellId": cell_id,
                "attempt": 1,
                "result": _json_result(float("nan")),
            }

    manager._sessions["stub-nan"] = StubSession()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/notebook/sessions/stub-nan/cells/execute",
            json={"cellId": "c1", "source": "1.0/0.0"},
        )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "kind": "json",
        "type": "float",
        "value": "NaN",
    }


def test_host_worker_ignores_forged_fd1_reply(tmp_path: Path) -> None:
    import json
    import sys

    from kedi_notebook.runtime import HostPythonBridge

    bridge = HostPythonBridge(sys.executable, cwd=tmp_path)
    try:
        forged = "__KEDI_NOTEBOOK_RESPONSE__" + json.dumps(
            {
                "ok": True,
                "result": "FORGED-BY-USER-CODE",
                "env": {},
                "stdout": "",
                "stderr": "",
            }
        )
        code = f"import os\nos.write(1, ({forged!r} + chr(10)).encode())\nx = 'real-result'\n"
        reply = bridge.request(
            {"action": "execute_block", "code": code, "syncNames": ["x"]},
            timeout=10,
        )
        # The genuine response, not the forgery written to fd 1.
        assert reply.get("result") is None
        assert reply.get("env", {}).get("x") == "real-result"
        assert "FORGED-BY-USER-CODE" not in json.dumps(reply)
        # The next request is not desynchronized either.
        following = bridge.request(
            {"action": "evaluate_inline", "code": "1+1", "syncNames": []},
            timeout=10,
        )
        assert following.get("result") == 2
    finally:
        bridge.close()


def _stub_stream_session(tmp_path: Path):
    import sys
    import threading

    from kedi_notebook.runtime import HostPython, NotebookSession

    session = object.__new__(NotebookSession)
    session.python = HostPython(
        id="python-1", executable=sys.executable, version="3.12.0", label="stub"
    )
    session._state_lock = threading.Lock()
    session._closed = False
    session._active_terminal_process = None
    session.cwd = tmp_path
    return session


def _live_thread_idents() -> set[int]:
    import threading

    return {thread.ident for thread in threading.enumerate() if thread.is_alive()}


def _open_fd_count() -> int:
    import os

    return len(os.listdir("/proc/self/fd"))


def test_stream_host_process_releases_threads_and_fds(tmp_path: Path) -> None:
    import subprocess
    import time

    # (a) A grandchild holds the pipe open past the timeout.
    session = _stub_stream_session(tmp_path)
    threads_before = _live_thread_idents()
    fds_before = _open_fd_count()
    with pytest.raises(subprocess.TimeoutExpired):
        list(
            session._stream_host_process(
                ["/bin/sh", "-lc", "sleep 30 & echo hi"],
                command="sleep 30 & echo hi",
                timeout=2,
            )
        )
    time.sleep(2)
    assert _live_thread_idents() <= threads_before
    assert _open_fd_count() <= fds_before

    # (b) The consumer abandons a fast-producing stream.
    session = _stub_stream_session(tmp_path)
    threads_before = _live_thread_idents()
    fds_before = _open_fd_count()
    generator = session._stream_host_process(
        ["/bin/sh", "-lc", "seq 1 200000"],
        command="seq 1 200000",
        timeout=30,
    )
    first = next(generator)
    assert first["type"] == "output"
    generator.close()
    time.sleep(2)
    assert _live_thread_idents() <= threads_before
    assert _open_fd_count() <= fds_before


def test_sandbox_worker_keeps_multiline_string_values_intact() -> None:
    from kedi_notebook import sandbox_worker as worker

    response = worker.handle_request(
        {
            "action": "execute_block",
            "code": 'x = """\nalpha\nbeta\n"""',
            "syncNames": ["x"],
            "env": {},
        }
    )
    assert response["ok"] is True
    assert response["env"]["x"] == "\nalpha\nbeta\n"


def test_sandbox_worker_turns_cyclic_values_into_references() -> None:
    from kedi_notebook import sandbox_worker as worker

    response = worker.handle_request(
        {
            "action": "execute_block",
            "code": "x = {}\nx['self'] = x",
            "syncNames": ["x"],
            "env": {},
        }
    )
    assert response["ok"] is True
    encoded = response["env"]["x"]
    assert set(encoded) == {"__kedi_notebook_ref__"}
    assert worker._from_json(encoded) is worker._OBJECT_REGISTRY[encoded["__kedi_notebook_ref__"]]


def test_sandbox_worker_object_registry_is_bounded() -> None:
    from kedi_notebook import sandbox_worker as worker

    first = worker.handle_request(
        {
            "action": "execute_block",
            "code": "v0 = lambda: 0",
            "syncNames": ["v0"],
            "env": {},
        }
    )
    first_handle = first["env"]["v0"]["__kedi_notebook_ref__"]
    for index in range(1, 1_100):
        worker.handle_request(
            {
                "action": "execute_block",
                "code": f"v{index} = lambda: {index}",
                "syncNames": [f"v{index}"],
                "env": {},
            }
        )
    assert len(worker._OBJECT_REGISTRY) <= worker._MAX_OBJECT_REGISTRY
    assert first_handle not in worker._OBJECT_REGISTRY


def _pyodide_driver_namespace() -> dict:
    """Execute the DRIVER_SETUP Python shipped in pyodide-worker.js.

    The ``js`` module it imports from is stubbed; the driver code itself is
    the exact text from the shipped file.
    """
    import re
    import sys
    import types

    static_root = Path(__file__).parent.parent / "src" / "kedi_notebook" / "static"
    source = (static_root / "pyodide-worker.js").read_text(encoding="utf-8")
    match = re.search(r"const DRIVER_SETUP = String\.raw`(.*?)`;\n", source, re.S)
    assert match is not None, "DRIVER_SETUP not found in pyodide-worker.js"
    js_module = types.ModuleType("js")
    js_module.kediExchange = lambda message: "{}"
    js_module.kediStreamOutput = lambda stream, text: None
    sys.modules["js"] = js_module
    namespace: dict = {}
    exec(compile(match.group(1), "<pyodide-driver>", "exec"), namespace)
    return namespace


def test_pyodide_driver_setup_matches_sandbox_worker_fixes() -> None:
    driver = _pyodide_driver_namespace()
    handle = driver["_handle_kedi_request"]

    multiline = handle(
        {
            "action": "execute_block",
            "code": 'x = """\nalpha\nbeta\n"""',
            "syncNames": ["x"],
            "env": {},
        }
    )
    assert multiline["ok"] is True
    assert multiline["env"]["x"] == "\nalpha\nbeta\n"

    cyclic = handle(
        {
            "action": "execute_block",
            "code": "x = {}\nx['self'] = x",
            "syncNames": ["x"],
            "env": {},
        }
    )
    assert cyclic["ok"] is True
    assert set(cyclic["env"]["x"]) == {"__kedi_notebook_ref__"}

    registry = driver["_OBJECT_REGISTRY"]
    first = handle(
        {
            "action": "execute_block",
            "code": "v0 = lambda: 0",
            "syncNames": ["v0"],
            "env": {},
        }
    )
    first_handle = first["env"]["v0"]["__kedi_notebook_ref__"]
    for index in range(1, 1_100):
        handle(
            {
                "action": "execute_block",
                "code": f"v{index} = lambda: {index}",
                "syncNames": [f"v{index}"],
                "env": {},
            }
        )
    assert len(registry) <= driver["_MAX_OBJECT_REGISTRY"]
    assert first_handle not in registry


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("hover", "/api/lsp/hover", {"source": "x = 1", "line": 0, "character": 0}),
        (
            "completion",
            "/api/lsp/completion",
            {"source": "x = 1", "line": 0, "character": 0},
        ),
        (
            "references",
            "/api/lsp/references",
            {"source": "x = 1", "line": 0, "character": 0, "includeDeclaration": True},
        ),
        (
            "prepare_rename",
            "/api/lsp/prepare-rename",
            {"source": "x = 1", "line": 0, "character": 0},
        ),
        (
            "rename",
            "/api/lsp/rename",
            {"source": "x = 1", "line": 0, "character": 0, "newName": "y"},
        ),
    ],
)
def test_lsp_endpoints_fall_back_to_kedi_when_pyright_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    payload: dict,
) -> None:
    from kedi_notebook import pyright as pyright_module

    def failing(self, *args, **kwargs):
        raise RuntimeError(f"Pyright request timed out: {method}")

    monkeypatch.setattr(pyright_module.PyrightServer, method, failing)
    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )

    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 200
    assert response.json()["provider"] == "kedi"


def test_secret_store_keeps_concurrent_updates(tmp_path: Path) -> None:
    store = NotebookSecretStore(tmp_path / "secrets.json")
    barrier = threading.Barrier(4)

    def setter(index: int) -> None:
        barrier.wait()
        store.set_many({f"T{index}K{number}": "v" for number in range(50)})

    threads = [threading.Thread(target=setter, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.names) == 200
    assert len(NotebookSecretStore(tmp_path / "secrets.json").names) == 200


def test_secret_store_refuses_a_write_it_could_not_read_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "secrets.json"
    store = NotebookSecretStore(path)
    with pytest.raises(ValueError, match="exceed 1 MB"):
        store.set_many({f"big{index}": "B" * 128_000 for index in range(9)})
    # The rejected write left the store readable instead of bricking it.
    assert NotebookSecretStore(path).names == ()


def test_secret_store_refuses_to_read_through_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.json"
    victim.write_text(
        '{"version": 1, "secrets": {"SOME_KEY": "some-value"}}',
        encoding="utf-8",
    )
    link = tmp_path / "secrets.json"
    link.symlink_to(victim)
    with pytest.raises(RuntimeError, match="symbolic link"):
        NotebookSecretStore(link)
    assert victim.read_text(encoding="utf-8").startswith('{"version": 1')


def _endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if route.path == path)


def test_terminal_disconnect_closes_the_session(tmp_path: Path) -> None:
    import asyncio

    from kedi_notebook.server import ExecuteCellPayload

    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )
    manager = app.state.notebook_manager
    closed: list[str] = []

    class StubSession:
        id = "stub-term"

        def touch(self) -> None:
            pass

        def close(self) -> None:
            closed.append(self.id)

        def stream_terminal(self, *, cell_id, source):
            yield {"type": "output", "stream": "stdout", "text": "chunk-1\n"}
            yield {"type": "output", "stream": "stdout", "text": "chunk-2\n"}
            yield {
                "type": "result",
                "ok": True,
                "cellId": cell_id,
                "attempt": 1,
                "stdout": "",
                "runtimeReset": True,
            }

    manager._sessions["stub-term"] = StubSession()
    endpoint = _endpoint(app, "/api/notebook/sessions/{session_id}/terminal/execute")
    response = endpoint("stub-term", ExecuteCellPayload(cellId="c1", source="!echo hi"))
    stream = response.body_iterator
    first = asyncio.run(stream.__anext__())
    assert "chunk-1" in first
    # A client disconnect closes the response stream mid-flight; the
    # session must be reset instead of surviving until TTL cleanup.
    asyncio.run(stream.aclose())

    assert closed == ["stub-term"]
    assert "stub-term" not in manager._sessions


def test_package_install_disconnect_stops_the_stream(tmp_path: Path) -> None:
    import asyncio

    from kedi_notebook.server import PackageInstallPayload

    app = create_app(
        cwd=tmp_path,
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )
    manager = app.state.notebook_manager
    stream_closed: list[bool] = []

    class StubSession:
        id = "stub-install"

        def touch(self) -> None:
            pass

        def close(self) -> None:
            pass

        def stream_package_install(self, packages):
            try:
                yield {"type": "output", "stream": "stdout", "text": "chunk-1\n"}
                yield {"type": "result", "ok": True, "returnCode": 0, "error": None}
            finally:
                stream_closed.append(True)

    manager._sessions["stub-install"] = StubSession()
    endpoint = _endpoint(app, "/api/notebook/sessions/{session_id}/packages/install")
    response = endpoint("stub-install", PackageInstallPayload(packages=["requests"]))
    stream = response.body_iterator
    first = asyncio.run(stream.__anext__())
    assert "chunk-1" in first
    asyncio.run(stream.aclose())

    assert stream_closed == [True]


def test_bridge_manager_is_removed() -> None:
    from kedi_notebook import bridge

    assert not hasattr(bridge, "BridgeManager")
    assert "BridgeManager" not in bridge.__all__
