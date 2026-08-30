from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import kedi

_BOOTSTRAP_TIMEOUT = 600.0
_MARKER_NAME = ".kedi-notebook-runtime.json"


@dataclass(frozen=True)
class PreparedHostEnvironment:
    executable: str
    directory: str


class HostEnvironmentProvider(Protocol):
    def prepare(
        self,
        *,
        executable: str,
        version: str,
        cwd: Path,
    ) -> PreparedHostEnvironment: ...


class HostEnvironmentManager:
    """Create and reuse project-specific Python environments for host notebooks."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        source_root: Path | None = None,
    ) -> None:
        configured_root = os.environ.get("KEDI_NOTEBOOK_ENV_HOME")
        self.root = (
            root
            or (Path(configured_root).expanduser() if configured_root else None)
            or Path.home() / ".kedi" / "notebook" / "venvs"
        ).resolve()
        self.source_root = source_root or _kedi_source_root()
        self._locks: dict[Path, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def prepare(
        self,
        *,
        executable: str,
        version: str,
        cwd: Path,
    ) -> PreparedHostEnvironment:
        base_executable = Path(executable).expanduser().resolve(strict=True)
        environment = self.root / _environment_name(base_executable, version, cwd)
        lock = self._lock_for(environment)
        with lock:
            python = _environment_python(environment)
            marker = self._expected_marker(base_executable, version, cwd)
            if python.is_file() and _read_marker(environment) == marker:
                return PreparedHostEnvironment(str(python), str(environment))
            self._rebuild(
                environment=environment,
                base_executable=base_executable,
                marker=marker,
            )
            return PreparedHostEnvironment(
                str(_environment_python(environment)),
                str(environment),
            )

    def _lock_for(self, environment: Path) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(environment, threading.Lock())

    def _expected_marker(
        self,
        base_executable: Path,
        version: str,
        cwd: Path,
    ) -> dict[str, str]:
        return {
            "baseExecutable": str(base_executable),
            "baseVersion": version,
            "cwd": str(cwd.resolve()),
            "kediSource": str(self.source_root) if self.source_root else "",
            "kediVersion": kedi.__version__,
        }

    def _rebuild(
        self,
        *,
        environment: Path,
        base_executable: Path,
        marker: dict[str, str],
    ) -> None:
        environment.parent.mkdir(parents=True, exist_ok=True)
        temporary = environment.with_name(f".{environment.name}.{uuid4().hex}.tmp")
        try:
            self._create_environment(base_executable, temporary)
            self._install_kedi(_environment_python(temporary))
            (temporary / _MARKER_NAME).write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if environment.exists():
                shutil.rmtree(environment)
            temporary.replace(environment)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _create_environment(self, base_executable: Path, environment: Path) -> None:
        uv = shutil.which("uv")
        if uv is not None:
            _run_bootstrap(
                [
                    uv,
                    "venv",
                    "--python",
                    str(base_executable),
                    "--seed",
                    str(environment),
                ],
                operation="create the notebook virtual environment",
            )
            return
        _run_bootstrap(
            [str(base_executable), "-m", "venv", "--without-pip", str(environment)],
            operation="create the notebook virtual environment",
        )
        _run_bootstrap(
            [
                str(_environment_python(environment)),
                "-m",
                "ensurepip",
                "--upgrade",
                "--default-pip",
            ],
            operation="install pip into the notebook virtual environment",
        )

    def _install_kedi(self, environment_python: Path) -> None:
        if self.source_root is None:
            targets = [f"kedi=={kedi.__version__}"]
        else:
            grammar = self.source_root / "tree-sitter-kedi"
            if not grammar.is_dir():
                raise RuntimeError(f"Kedi source checkout is missing tree-sitter-kedi: {grammar}")
            targets = ["--editable", str(grammar), "--editable", str(self.source_root)]
        uv = shutil.which("uv")
        if uv is not None:
            argv = [uv, "pip", "install", "--python", str(environment_python), *targets]
        else:
            argv = [
                str(environment_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *targets,
            ]
        _run_bootstrap(argv, operation="install Kedi into the notebook virtual environment")
        _run_bootstrap(
            [str(environment_python), "-c", "import kedi"],
            operation="verify Kedi in the notebook virtual environment",
        )


def _environment_name(base_executable: Path, version: str, cwd: Path) -> str:
    fingerprint = hashlib.sha256(f"{base_executable}\0{cwd.resolve()}".encode()).hexdigest()[:12]
    major_minor = ".".join(version.split(".")[:2])
    return f"kedi-notebook-py{major_minor}-{fingerprint}"


def _environment_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _read_marker(environment: Path) -> dict[str, str] | None:
    try:
        value = json.loads((environment / _MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return None
    return value


def _kedi_source_root() -> Path | None:
    package_file = Path(kedi.__file__).resolve()
    for candidate in package_file.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "kedi").is_dir():
            return candidate
    return None


def _run_bootstrap(argv: list[str], *, operation: str) -> None:
    try:
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=_BOOTSTRAP_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not {operation}: {exc}") from exc
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout).strip()
    if len(detail) > 4_000:
        detail = detail[-4_000:]
    raise RuntimeError(f"Could not {operation}: {detail or 'command failed'}")


__all__ = [
    "HostEnvironmentManager",
    "HostEnvironmentProvider",
    "PreparedHostEnvironment",
]
