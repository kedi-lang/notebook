from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel

from kedi_notebook import model_dependencies as dependencies


def test_existing_provider_does_not_install(monkeypatch: pytest.MonkeyPatch) -> None:
    model = TestModel()
    monkeypatch.setattr(dependencies, "infer_model", lambda _: model)
    monkeypatch.setattr(dependencies, "_install_provider_extra", pytest.fail)
    assert dependencies.resolve_notebook_model("openrouter:example/model") is model


def test_missing_provider_is_installed_once_across_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = TestModel()
    installed: list[str] = []

    def infer(name: str) -> TestModel:
        assert name == "openrouter:example/model"
        if not installed:
            raise ImportError('Please install `pip install "pydantic-ai-slim[openai]"`')
        return model

    monkeypatch.setattr(dependencies, "infer_model", infer)
    monkeypatch.setattr(dependencies, "_install_provider_extra", installed.append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                dependencies.resolve_notebook_model,
                ["openrouter:example/model", "openrouter:example/model"],
            )
        )
    assert results == [model, model]
    assert installed == ["openai"]


@pytest.mark.parametrize(
    "error",
    [
        ImportError("Internal import failed"),
        ImportError("Install pydantic-ai-slim[cli]"),
        ValueError("API key is missing"),
    ],
)
def test_unrelated_errors_do_not_install(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def infer(_: str) -> TestModel:
        raise error

    monkeypatch.setattr(dependencies, "infer_model", infer)
    monkeypatch.setattr(dependencies, "_install_provider_extra", pytest.fail)
    with pytest.raises(type(error), match=str(error).replace("[", r"\[").replace("]", r"\]")):
        dependencies.resolve_notebook_model("openrouter:example/model")


def test_failed_installation_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def infer(_: str) -> TestModel:
        raise ImportError("Install pydantic-ai-slim[google]")

    def install(extra: str) -> None:
        attempts.append(extra)
        raise RuntimeError("Dependency conflict")

    monkeypatch.setattr(dependencies, "infer_model", infer)
    monkeypatch.setattr(dependencies, "_install_provider_extra", install)
    with pytest.raises(RuntimeError, match="Dependency conflict"):
        dependencies.resolve_notebook_model("google:example")
    assert attempts == ["google"]


@pytest.mark.parametrize("uv", ["/tools/uv", None])
def test_install_targets_server_and_preserves_existing_versions(
    monkeypatch: pytest.MonkeyPatch,
    uv: str | None,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: uv)
    monkeypatch.setattr(dependencies.metadata, "version", lambda _: "2.36.0")
    monkeypatch.setattr(
        dependencies.metadata,
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": "pydantic-ai-slim"}, version="2.36.0"),
            SimpleNamespace(metadata={"Name": "pydantic"}, version="2.13.4"),
        ],
    )

    def bootstrap(argv: list[str], *, operation: str) -> None:
        assert "openai provider" in operation
        assert argv[-1] == "pydantic-ai-slim[openai]==2.36.0"
        constraints = Path(argv[argv.index("--constraint") + 1]).read_text()
        assert set(constraints.splitlines()) == {
            "pydantic-ai-slim==2.36.0",
            "pydantic==2.13.4",
        }
        calls.append(argv)

    monkeypatch.setattr(dependencies, "_run_bootstrap", bootstrap)
    dependencies._install_provider_extra("openai")
    assert len(calls) == 1
    if uv:
        assert calls[0][:5] == [uv, "pip", "install", "--python", sys.executable]
    else:
        assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]
