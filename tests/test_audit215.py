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
