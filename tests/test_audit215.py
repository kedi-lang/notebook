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
def test_secret_store_rejects_process_control_names(
    tmp_path: Path, name: str
) -> None:
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


def _run_live_notebook_server(tmp_path: Path):
    import socket
    import threading
    import time

    import uvicorn

    app = create_app(
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
    import json

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
