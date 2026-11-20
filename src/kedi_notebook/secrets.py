from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final

_ENVIRONMENT_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Names that steer the notebook process itself (interpreter, loader, package
# installer, shell, home directory). Storing one as a "secret" would let a
# notebook API call rewrite how the server and its worker processes start,
# so they are rejected wherever a secret is accepted.
_PROCESS_CONTROL_NAMES: Final = frozenset(
    {
        "PATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "VIRTUAL_ENV",
        "HOME",
        "SHELL",
        "BASH_ENV",
        "ENV",
    }
)
_PROCESS_CONTROL_PREFIXES: Final = ("PYTHON", "LD_", "DYLD_", "UV_", "PIP_")
_MAX_FILE_BYTES: Final = 1_000_000
_MAX_SECRETS: Final = 256
_MAX_VALUE_CHARS: Final = 128_000


class NotebookSecretStore:
    """Persist notebook environment values without exposing them to the browser."""

    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or os.environ.get("KEDI_NOTEBOOK_SECRETS_PATH")
        self.path = (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".kedi" / "notebook" / "secrets.json"
        )
        self._lock = threading.RLock()
        self._previous_environment: dict[str, str | None] = {}
        self._values = self._read()

    @property
    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._values))

    def apply_to_environment(self) -> None:
        with self._lock:
            for name, value in self._values.items():
                self._remember_environment(name)
                os.environ[name] = value

    def set(self, name: str, value: str) -> None:
        self.set_many({name: value})

    def set_many(self, values: Mapping[str, str]) -> tuple[str, ...]:
        with self._lock:
            if not values:
                raise ValueError("No environment values were provided")
            for name, value in values.items():
                _validate_name(name)
                _validate_value(value)
            updated = dict(self._values)
            updated.update(values)
            self._write(updated)
            self._values = updated
            for name, value in values.items():
                self._remember_environment(name)
                os.environ[name] = value
            return tuple(sorted(values))

    def delete(self, name: str) -> bool:
        with self._lock:
            _validate_name(name)
            if name not in self._values:
                return False
            updated = dict(self._values)
            del updated[name]
            self._write(updated)
            self._values = updated
            previous = self._previous_environment.pop(name, None)
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
            return True

    def _remember_environment(self, name: str) -> None:
        if name not in self._previous_environment:
            self._previous_environment[name] = os.environ.get(name)

    def _read(self) -> dict[str, str]:
        with self._lock:
            if not self.path.exists():
                return {}
            if self.path.is_symlink():
                raise RuntimeError("Notebook secret store cannot be a symbolic link")
            # Open with O_NOFOLLOW so the path cannot be swapped for a
            # symlink between the check above and the read.
            try:
                descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                raise RuntimeError("Notebook secret store could not be read") from exc
            stream = None
            try:
                os.fchmod(descriptor, 0o600)
                if os.fstat(descriptor).st_size > _MAX_FILE_BYTES:
                    raise RuntimeError("Notebook secret store is larger than 1 MB")
                stream = os.fdopen(descriptor, "r", encoding="utf-8")
                payload = json.load(stream)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Notebook secret store could not be read") from exc
            finally:
                if stream is not None:
                    stream.close()
                else:
                    os.close(descriptor)
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise RuntimeError("Notebook secret store has an unsupported format")
            raw_values = payload.get("secrets")
            if not isinstance(raw_values, dict) or len(raw_values) > _MAX_SECRETS:
                raise RuntimeError("Notebook secret store has invalid entries")
            values: dict[str, str] = {}
            for name, value in raw_values.items():
                if not isinstance(name, str) or not isinstance(value, str):
                    raise RuntimeError("Notebook secret store has invalid entries")
                try:
                    _validate_name(name)
                    _validate_value(value)
                except ValueError as exc:
                    raise RuntimeError("Notebook secret store has invalid entries") from exc
                values[name] = value
            return values

    def _write(self, values: dict[str, str]) -> None:
        with self._lock:
            if len(values) > _MAX_SECRETS:
                raise ValueError(
                    f"At most {_MAX_SECRETS} notebook environment values may be stored"
                )
            # Serialize first: _read refuses files over 1 MB, so a write
            # that would cross it must fail here instead of bricking the
            # store for the next load.
            payload = (
                json.dumps(
                    {"version": 1, "secrets": values},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            if len(payload.encode("utf-8")) > _MAX_FILE_BYTES:
                raise ValueError("Notebook secret store would exceed 1 MB")
            parent = self.path.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
            if self.path.exists() and self.path.is_symlink():
                raise RuntimeError("Notebook secret store cannot be a symbolic link")

            descriptor, temporary_name = tempfile.mkstemp(prefix=".secrets-", dir=parent)
            temporary_path = Path(temporary_name)
            try:
                os.chmod(temporary_path, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
                os.chmod(self.path, 0o600)
                _sync_directory(parent)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise


def _validate_name(name: str) -> None:
    if not _ENVIRONMENT_NAME.fullmatch(name):
        raise ValueError("Environment name must be a valid identifier")
    if name in _PROCESS_CONTROL_NAMES or name.startswith(_PROCESS_CONTROL_PREFIXES):
        raise ValueError(
            f"Environment name {name!r} controls the notebook process and "
            "cannot be stored as a secret"
        )


def _validate_value(value: str) -> None:
    if not value:
        raise ValueError("Environment value cannot be empty")
    if len(value) > _MAX_VALUE_CHARS:
        raise ValueError(f"Environment value cannot exceed {_MAX_VALUE_CHARS} characters")
    if "\x00" in value:
        raise ValueError("Environment value cannot contain a null character")


def _sync_directory(path: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
