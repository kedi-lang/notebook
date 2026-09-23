"""Install a missing provider SDK before the notebook's first model request."""

from __future__ import annotations

import importlib
import re
import shutil
import sys
import tempfile
import threading
from importlib import metadata
from pathlib import Path

from pydantic_ai.models import Model, infer_model

from .host_environment import _run_bootstrap

_INSTALL_LOCK = threading.Lock()
_EXTRA_HINT = re.compile(r"pydantic-ai-slim\[([a-z][a-z0-9-]*)\]")
_PROVIDER_EXTRAS = frozenset(
    {
        "anthropic",
        "bedrock",
        "bedrock-mantle",
        "cerebras",
        "cohere",
        "crusoe",
        "google",
        "groq",
        "huggingface",
        "mistral",
        "openai",
        "openrouter",
        "snowflake",
        "xai",
        "zai",
    }
)


def resolve_notebook_model(model: str) -> Model:
    try:
        return infer_model(model)
    except ImportError as exc:
        if _missing_provider_extra(exc) is None:
            raise

    with _INSTALL_LOCK:
        # A concurrent notebook may have completed the same installation.
        try:
            return infer_model(model)
        except ImportError as exc:
            extra = _missing_provider_extra(exc)
            if extra is None:
                raise
        _install_provider_extra(extra)
        importlib.invalidate_caches()
        return infer_model(model)


def _missing_provider_extra(error: ImportError) -> str | None:
    match = _EXTRA_HINT.search(str(error))
    if match is None or match[1] not in _PROVIDER_EXTRAS:
        return None
    return match[1]


def _install_provider_extra(extra: str) -> None:
    requirement = f"pydantic-ai-slim[{extra}]=={metadata.version('pydantic-ai-slim')}"
    with tempfile.TemporaryDirectory(prefix="kedi-notebook-provider-") as directory:
        constraints = Path(directory) / "constraints.txt"
        # Already-imported packages cannot safely change version in a running server.
        constraints.write_text(
            "\n".join(
                sorted(
                    {
                        f"{name}=={dist.version}"
                        for dist in metadata.distributions()
                        if (name := dist.metadata["Name"])
                    }
                )
            )
            + "\n",
            encoding="utf-8",
        )
        uv = shutil.which("uv")
        argv = (
            [uv, "pip", "install", "--python", sys.executable]
            if uv is not None
            else [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
        )
        _run_bootstrap(
            [*argv, "--constraint", str(constraints), requirement],
            operation=f"install the {extra} provider in the notebook server environment",
        )
