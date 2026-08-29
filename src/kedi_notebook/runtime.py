from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Literal
from uuid import uuid4

import kedi
from kedi.executors import PlaygroundExecutor, PyodideExecutor

from .bridge import BridgeRun
from .execution import execution_error_payload

_RESPONSE_PREFIX = "__KEDI_NOTEBOOK_RESPONSE__"
_WORKER = Path(__file__).with_name("sandbox_worker.py")
_COMMON_PYTHON_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path.home() / ".local" / "bin",
)
_PYTHON_BINARY_NAME = re.compile(r"python(?:3(?:\.\d+)?)?")
_TERMINAL_TIMEOUT = 120.0


@dataclass(frozen=True)
class HostPython:
    id: str
    executable: str
    version: str
    label: str
    current: bool = False
    explicit: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class HostPythonBridge:
    """Persistent Python executor bridge running under a selected interpreter."""

    def __init__(self, executable: str, *, cwd: Path) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._process = subprocess.Popen(
            [executable, "-u", str(_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=os.environ.copy(),
        )

    def request(self, payload: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
        del timeout  # The line protocol is serialized; cancellation closes the worker.
        with self._lock:
            if self._closed:
                raise RuntimeError("Host Python worker is closed")
            process = self._process
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("Host Python worker pipes are unavailable")
            if process.poll() is not None:
                raise RuntimeError(self._worker_error("Host Python worker exited"))
            process.stdin.write(json.dumps(dict(payload), separators=(",", ":")) + "\n")
            process.stdin.flush()
            for line in process.stdout:
                if line.startswith(_RESPONSE_PREFIX):
                    response = json.loads(line.removeprefix(_RESPONSE_PREFIX))
                    if not isinstance(response, dict):
                        raise TypeError("Host Python worker response must be an object")
                    return response
            raise RuntimeError(self._worker_error("Host Python worker returned no response"))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def _worker_error(self, fallback: str) -> str:
        stderr = self._process.stderr
        detail = (
            stderr.read().strip() if stderr is not None and self._process.poll() is not None else ""
        )
        return detail or fallback


class NotebookSession:
    def __init__(
        self,
        *,
        session_id: str,
        mode: Literal["browser", "host"],
        cwd: Path,
        python: HostPython | None,
    ) -> None:
        self.id = session_id
        self.mode = mode
        self.cwd = cwd
        self.python = python
        self.bridge: BridgeRun | HostPythonBridge
        if mode == "browser":
            self.bridge = BridgeRun(session_id)
            executor = PyodideExecutor(self.bridge, timeout=120)
        else:
            if python is None:
                raise ValueError("Host execution requires a Python interpreter")
            self.bridge = HostPythonBridge(python.executable, cwd=cwd)
            executor = PlaygroundExecutor(self.bridge, timeout=120)
        self._executor = executor
        self._session = kedi.interactive(executor=executor, cwd=cwd)
        self._lock = threading.Lock()
        self._closed = False
        self._attempt = 0
        self._execution_count = 0

    def execute(self, *, cell_id: str, source: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Notebook session is closed")
            self._attempt += 1
            source_name = f"<notebook:{self.id}:{cell_id}:{self._attempt}>"
            try:
                result = self._session.execute(source, source_name=source_name)
            except BaseException as exc:
                payload = execution_error_payload(exc, source_paths={source_name})
                payload.update(
                    {
                        "cellId": cell_id,
                        "attempt": self._attempt,
                        "stdout": self._executor.drain_stdout(),
                    }
                )
                return payload
            self._execution_count += 1
            return {
                "ok": True,
                "cellId": cell_id,
                "attempt": self._attempt,
                "executionCount": self._execution_count,
                "stdout": self._executor.drain_stdout(),
                "result": _json_result(result),
            }

    def execute_terminal(self, *, cell_id: str, source: str) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        for event in self.stream_terminal(cell_id=cell_id, source=source):
            if event["type"] == "result":
                result = {key: value for key, value in event.items() if key != "type"}
        if result is None:
            raise RuntimeError("Terminal execution returned no result")
        return result

    def stream_terminal(
        self,
        *,
        cell_id: str,
        source: str,
    ) -> Iterator[dict[str, Any]]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Notebook session is closed")
            self._attempt += 1
            stdout: list[str] = []
            try:
                commands = _terminal_commands(source)
                for command in commands:
                    if self.mode == "browser":
                        if not isinstance(self.bridge, BridgeRun):
                            raise RuntimeError("Browser terminal bridge is unavailable")
                        response: Mapping[str, Any] | None = None
                        streamed = {"stdout": "", "stderr": ""}
                        for event in self.bridge.request_events(
                            {"action": "execute_terminal", "command": command},
                            timeout=_TERMINAL_TIMEOUT,
                        ):
                            if event["type"] == "output":
                                stream = str(event["stream"])
                                text = str(event["text"])
                                streamed[stream] += text
                                stdout.append(text)
                                yield event
                            else:
                                response = event["response"]
                        if response is None:
                            raise RuntimeError("Browser terminal returned no response")
                        for stream in ("stdout", "stderr"):
                            text = str(response.get(stream, ""))
                            if text.startswith(streamed[stream]):
                                text = text[len(streamed[stream]) :]
                            if text:
                                stdout.append(text)
                                yield {"type": "output", "stream": stream, "text": text}
                        return_code = 0 if response.get("ok") is True else 1
                    else:
                        response = None
                        return_code = 0
                        for event in self._stream_host_terminal(command):
                            if event["type"] == "output":
                                text = str(event["text"])
                                stdout.append(text)
                                yield event
                            else:
                                return_code = int(event["returnCode"])
                    if return_code != 0:
                        response = response or {}
                        error_type = response.get("errorType", "TerminalCommandError")
                        detail = response.get(
                            "error",
                            f"Command exited with status {return_code}: {command}",
                        )
                        yield {
                            "type": "result",
                            "ok": False,
                            "cellId": cell_id,
                            "attempt": self._attempt,
                            "stdout": "".join(stdout),
                            "error": f"{error_type}: {detail}",
                        }
                        return
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                yield {
                    "type": "result",
                    "ok": False,
                    "cellId": cell_id,
                    "attempt": self._attempt,
                    "stdout": "".join(stdout),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                return

            self._execution_count += 1
            yield {
                "type": "result",
                "ok": True,
                "cellId": cell_id,
                "attempt": self._attempt,
                "executionCount": self._execution_count,
                "stdout": "".join(stdout),
                "result": None,
            }

    def _stream_host_terminal(self, command: str) -> Iterator[dict[str, Any]]:
        if self.python is None:
            raise RuntimeError("Host terminal requires a selected Python interpreter")

        tokens = shlex.split(command)
        if not tokens:
            raise ValueError("Terminal command cannot be empty")
        executable = self.python.executable
        if tokens[0] in {"pip", "pip3"}:
            argv = [executable, "-m", "pip", *tokens[1:]]
        elif tokens[0] in {"python", "python3"}:
            argv = [executable, *tokens[1:]]
        else:
            argv = ["/bin/sh", "-lc", command]

        env = os.environ.copy()
        python_path = Path(executable)
        env["PATH"] = os.pathsep.join([str(python_path.parent), env.get("PATH", "")]).rstrip(
            os.pathsep
        )
        if (
            python_path.parent.name == "bin"
            and (python_path.parent.parent / "pyvenv.cfg").is_file()
        ):
            env["VIRTUAL_ENV"] = str(python_path.parent.parent)

        process = subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        output: Queue[tuple[str, str | None]] = Queue()

        def pump(stream_name: str, stream: Any) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while chunk := os.read(stream.fileno(), 4096):
                    if text := decoder.decode(chunk):
                        output.put((stream_name, text))
                if text := decoder.decode(b"", final=True):
                    output.put((stream_name, text))
            finally:
                stream.close()
                output.put((stream_name, None))

        if process.stdout is None or process.stderr is None:
            process.kill()
            raise RuntimeError("Terminal process output pipes are unavailable")
        readers = [
            threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + _TERMINAL_TIMEOUT
        open_streams = len(readers)
        try:
            while open_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise subprocess.TimeoutExpired(command, _TERMINAL_TIMEOUT)
                try:
                    stream_name, text = output.get(timeout=min(remaining, 0.1))
                except Empty:
                    continue
                if text is None:
                    open_streams -= 1
                    continue
                yield {"type": "output", "stream": stream_name, "text": text}
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            yield {"type": "command_result", "returnCode": return_code}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)

    def next_browser_request(self, *, timeout: float) -> dict[str, Any] | None:
        if self.mode != "browser" or not isinstance(self.bridge, BridgeRun):
            raise ValueError("Notebook session does not use browser Python")
        return self.bridge.next_request(timeout=timeout)

    def submit_browser_response(self, request_id: str, response: Mapping[str, Any]) -> None:
        if self.mode != "browser" or not isinstance(self.bridge, BridgeRun):
            raise ValueError("Notebook session does not use browser Python")
        self.bridge.submit_response(request_id, response)

    def submit_browser_output(self, request_id: str, *, stream: str, text: str) -> None:
        if self.mode != "browser" or not isinstance(self.bridge, BridgeRun):
            raise ValueError("Notebook session does not use browser Python")
        self.bridge.submit_output(request_id, stream=stream, text=text)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if isinstance(self.bridge, BridgeRun):
                self.bridge.cancel()
            else:
                self.bridge.close()
            self._session.close()


class NotebookSessionManager:
    def __init__(
        self,
        *,
        cwd: Path | None = None,
        explicit_pythons: Sequence[str | Path] = (),
    ) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.pythons = discover_host_pythons(explicit_pythons)
        self._sessions: dict[str, NotebookSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        mode: Literal["browser", "host"] = "browser",
        python_id: str | None = None,
    ) -> NotebookSession:
        python = None
        if mode == "host":
            python = self._resolve_python(python_id)
        session_id = uuid4().hex
        session = NotebookSession(
            session_id=session_id,
            mode=mode,
            cwd=self.cwd,
            python=python,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> NotebookSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError("Notebook session was not found")
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _resolve_python(self, python_id: str | None) -> HostPython:
        if not self.pythons:
            raise RuntimeError("No compatible host Python installation was found")
        if python_id is None:
            return self.pythons[0]
        for python in self.pythons:
            if python.id == python_id:
                return python
        raise ValueError("Selected host Python is not available")


def discover_host_pythons(explicit: Sequence[str | Path] = ()) -> list[HostPython]:
    candidates: list[tuple[Path, bool]] = [(Path(item).expanduser(), True) for item in explicit]
    candidates.append((Path(sys.executable), False))
    for name in ("python3", "python", *(f"python3.{minor}" for minor in range(10, 15))):
        found = shutil.which(name)
        if found:
            candidates.append((Path(found), False))
    for directory in _COMMON_PYTHON_DIRS:
        if not directory.is_dir():
            continue
        candidates.extend(
            (path, False)
            for path in sorted(directory.iterdir())
            if _PYTHON_BINARY_NAME.fullmatch(path.name)
        )

    resolved_candidates: dict[Path, tuple[Path, bool]] = {}
    current_executable = Path(os.path.abspath(sys.executable))
    current_target = current_executable.resolve()
    for candidate, is_explicit in candidates:
        executable = Path(os.path.abspath(candidate))
        try:
            target = executable.resolve(strict=True)
        except OSError:
            if is_explicit:
                raise ValueError(f"Python executable does not exist: {candidate}") from None
            continue
        if not os.access(executable, os.X_OK) or executable.is_dir():
            if is_explicit:
                raise ValueError(f"Python path is not an executable file: {executable}")
            continue
        previous = resolved_candidates.get(target)
        if previous is None or (is_explicit and not previous[1]):
            resolved_candidates[target] = (executable, is_explicit)

    executables = [item[0] for item in resolved_candidates.values()]

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(executables)))) as pool:
        metadata_by_path = dict(
            zip(
                executables,
                pool.map(_python_metadata, executables),
                strict=True,
            )
        )

    result: list[HostPython] = []
    for target, (executable, is_explicit) in resolved_candidates.items():
        metadata = metadata_by_path[executable]
        if metadata is None:
            if is_explicit:
                raise ValueError(f"Cannot inspect Python executable: {executable}")
            continue
        major, minor, patch = metadata["version"]
        if (major, minor) < (3, 10):
            if is_explicit:
                raise ValueError("Kedi notebook host execution requires Python 3.10 or newer")
            continue
        version = f"{major}.{minor}.{patch}"
        result.append(
            HostPython(
                id=f"python-{len(result) + 1}",
                executable=str(executable),
                version=version,
                label=f"Python {version} - {executable}",
                current=executable == current_executable or target == current_target,
                explicit=is_explicit,
            )
        )
    result.sort(key=lambda item: (not item.explicit, not item.current, item.executable))
    return [
        HostPython(
            id=f"python-{index}",
            executable=item.executable,
            version=item.version,
            label=item.label,
            current=item.current,
            explicit=item.explicit,
        )
        for index, item in enumerate(result, start=1)
    ]


def _python_metadata(executable: Path) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-c",
                "import json,sys; print(json.dumps({'version': list(sys.version_info[:3])}))",
            ],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            return None
        value = json.loads(completed.stdout.strip())
        return value if isinstance(value, dict) else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _json_result(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return {"kind": "repr", "type": type(value).__name__, "value": repr(value)}
    return {"kind": "json", "type": type(value).__name__, "value": value}


def _terminal_commands(source: str) -> list[str]:
    commands: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("!"):
            raise ValueError(f"Terminal cell line {line_number} must begin with '!'")
        command = stripped[1:].strip()
        if not command:
            raise ValueError(f"Terminal cell line {line_number} has no command")
        commands.append(command)
    if not commands:
        raise ValueError("Terminal cell has no commands")
    return commands


__all__ = [
    "HostPython",
    "HostPythonBridge",
    "NotebookSession",
    "NotebookSessionManager",
    "discover_host_pythons",
]
