from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final

_ENVIRONMENT_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
        self._values = self._read()
        self._previous_environment: dict[str, str | None] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def apply_to_environment(self) -> None:
        for name, value in self._values.items():
            self._remember_environment(name)
            os.environ[name] = value

    def set(self, name: str, value: str) -> None:
        self.set_many({name: value})

    def set_many(self, values: Mapping[str, str]) -> tuple[str, ...]:
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
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise RuntimeError("Notebook secret store cannot be a symbolic link")
        os.chmod(self.path, 0o600)
        if self.path.stat().st_size > _MAX_FILE_BYTES:
            raise RuntimeError("Notebook secret store is larger than 1 MB")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Notebook secret store could not be read") from exc
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
        if len(values) > _MAX_SECRETS:
            raise ValueError(f"At most {_MAX_SECRETS} notebook environment values may be stored")
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
                json.dump(
                    {"version": 1, "secrets": values},
                    stream,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
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
