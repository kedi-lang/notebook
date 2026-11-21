from __future__ import annotations

import base64
import binascii
import codecs
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Generator, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from secrets import token_hex
from typing import Any, Final, Literal
from uuid import uuid4

import kedi
from kedi.agent_adapter.cancellation import RunCancellation, RunCancelled, use_run_cancellation
from kedi.agent_adapter.model_resolution import pydantic_model_resolution
from kedi.executors import PlaygroundExecutor, PyodideExecutor

from .bridge import BridgeCancelled, BridgeRun
from .execution import execution_error_payload
from .host_environment import HostEnvironmentManager, HostEnvironmentProvider
from .model_dependencies import resolve_notebook_model

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
_PACKAGE_INSTALL_TIMEOUT = 600.0
_EXECUTION_TIMEOUT = 120.0
_OUTPUT_LIMIT = 200_000
# A media result is previewed when its bytes are at most this large and the browser can show them.
_MEDIA_PREVIEW_LIMIT = 5_000_000
_MEDIA_PREVIEW_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "audio/wav",
        "audio/mpeg",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
    }
)


def _notebook_source_name(session_id: str, attempt: int) -> str:
    return f"<notebook:{session_id[:8]}:{attempt}>"


@dataclass(frozen=True)
class HostPython:
    id: str
    executable: str
    version: str
    label: str
    current: bool = False
    explicit: bool = False
    managed: bool = False
    base_executable: str | None = None
    environment: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class HostPythonBridge:
    """Persistent Python executor bridge running under a selected interpreter."""

    def __init__(self, executable: str, *, cwd: Path) -> None:
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        # Every reply from the worker must carry this nonce. User code can
        # write to fd 1, but it never sees the nonce, so a forged protocol
        # line is ignored instead of being accepted as the next response.
        self._nonce = token_hex(16)
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
        self._stderr: deque[str] = deque(maxlen=200)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()

    def request(self, payload: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
        with self._request_lock:
            with self._state_lock:
                if self._closed:
                    raise BridgeCancelled("Host Python worker was interrupted")
                process = self._process
            if process.stdin is None or process.stdout is None:
                raise RuntimeError("Host Python worker pipes are unavailable")
            stdout = process.stdout
            if process.poll() is not None:
                raise RuntimeError(self._worker_error("Host Python worker exited"))
            request_payload = {"nonce": self._nonce, **dict(payload)}
            process.stdin.write(json.dumps(request_payload, separators=(",", ":")) + "\n")
            process.stdin.flush()
            response_queue: Queue[tuple[str, dict[str, Any] | BaseException | None]] = Queue(
                maxsize=1
            )

            def read_response() -> None:
                try:
                    for line in stdout:
                        if not line.startswith(_RESPONSE_PREFIX):
                            continue
                        try:
                            candidate = json.loads(line.removeprefix(_RESPONSE_PREFIX))
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(candidate, dict) or candidate.get("nonce") != self._nonce:
                            # A forged line written to fd 1 by user code, or a
                            # malformed line: never a protocol reply.
                            continue
                        candidate.pop("nonce", None)
                        response_queue.put(("response", candidate))
                        return
                    response_queue.put(("closed", None))
                except BaseException as exc:  # Worker pipe boundary.
                    response_queue.put(("error", exc))

            reader = threading.Thread(target=read_response, daemon=True)
            reader.start()
            try:
                kind, value = response_queue.get(timeout=timeout)
            except Empty:
                self._terminate_process(process)
                reader.join(timeout=1)
                raise TimeoutError(f"Host Python execution exceeded {timeout:g} seconds") from None
            if kind == "error":
                if isinstance(value, BaseException):
                    raise RuntimeError("Host Python worker response failed") from value
                raise RuntimeError("Host Python worker response failed")
            if kind == "closed":
                with self._state_lock:
                    interrupted = self._closed
                if interrupted:
                    raise BridgeCancelled("Host Python worker was interrupted")
                raise RuntimeError(self._worker_error("Host Python worker returned no response"))
            if not isinstance(value, dict):
                raise TypeError("Host Python worker response must be an object")
            return value

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
        self._terminate_process(process)

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        for line in stderr:
            self._stderr.append(line.rstrip())

    def _worker_error(self, fallback: str) -> str:
        detail = "\n".join(self._stderr).strip()
        return detail or fallback


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate a host process and the children that inherited its pipes.

    The process runs in its own process group (see ``_stream_host_process``),
    so signalling the group also reaches backgrounded children; otherwise
    they keep the output pipes open and outlive the session as zombies.
    The group is signalled even when the main process already exited,
    because a backgrounded child may still hold the pipes open.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, OSError):
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            process.kill()
        process.wait(timeout=1)


class NotebookSession:
    def __init__(
        self,
        *,
        session_id: str,
        mode: Literal["browser", "host"],
        cwd: Path,
        python: HostPython | None,
        interactive_options: Mapping[str, Any] | None = None,
        session_snapshot: str | None = None,
    ) -> None:
        self.id = session_id
        self.mode = mode
        self.cwd = cwd
        self.python = python
        self.bridge: BridgeRun | HostPythonBridge
        if mode == "browser":
            self.bridge = BridgeRun(session_id)
            executor = PyodideExecutor(self.bridge, timeout=_EXECUTION_TIMEOUT)
        else:
            if python is None:
                raise ValueError("Host execution requires a Python interpreter")
            self.bridge = HostPythonBridge(python.executable, cwd=cwd)
            executor = PlaygroundExecutor(self.bridge, timeout=_EXECUTION_TIMEOUT)
        self._executor = executor
        self._cancellation = RunCancellation()
        try:
            if session_snapshot is None:
                self._session = kedi.interactive(
                    executor=executor,
                    cwd=cwd,
                    **dict(interactive_options or {}),
                )
            else:
                self._session = _load_session_snapshot(session_snapshot, executor=executor)
        except BaseException:
            if isinstance(self.bridge, BridgeRun):
                self.bridge.cancel()
            else:
                self.bridge.close()
            raise
        self._execution_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._active_terminal_process: subprocess.Popen[bytes] | None = None
        self._attempt = 0
        self._execution_count = 0
        self._last_activity = time.monotonic()

    def execute(self, *, cell_id: str, source: str) -> dict[str, Any]:
        with self._execution_lock:
            self._assert_open()
            self.touch()
            self._attempt += 1
            source_name = _notebook_source_name(self.id, self._attempt)
            displays: list[dict[str, object]] = []
            try:
                with (
                    use_run_cancellation(self._cancellation),
                    pydantic_model_resolution(resolve_notebook_model),
                    _media_displays(displays),
                ):
                    result = self._session.execute(source, source_name=source_name)
            except BaseException as exc:
                payload = execution_error_payload(exc, source_paths={source_name})
                payload.update(
                    {
                        "cellId": cell_id,
                        "attempt": self._attempt,
                        "stdout": _truncate_output(self._executor.drain_stdout()),
                        "runtimeReset": _requires_runtime_reset(exc),
                    }
                )
                return payload
            self._execution_count += 1
            return {
                "ok": True,
                "cellId": cell_id,
                "attempt": self._attempt,
                "executionCount": self._execution_count,
                "stdout": _truncate_output(self._executor.drain_stdout()),
                "result": _cell_result(result, displays),
            }

    def execute_terminal(self, *, cell_id: str, source: str) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        for event in self.stream_terminal(cell_id=cell_id, source=source):
            if event["type"] == "result":
                result = {key: value for key, value in event.items() if key != "type"}
        if result is None:
            raise RuntimeError("Terminal execution returned no result")
        return result

    def snapshot(self) -> str:
        with self._execution_lock:
            self._assert_open()
            self.touch()
            with tempfile.TemporaryDirectory(prefix="kedi-notebook-snapshot-") as directory:
                path = Path(directory) / "session.json"
                self._session.dump(path)
                return base64.b64encode(path.read_bytes()).decode("ascii")

    def stream_terminal(
        self,
        *,
        cell_id: str,
        source: str,
    ) -> Generator[dict[str, Any], None, None]:
        with self._execution_lock:
            self._assert_open()
            self.touch()
            self._attempt += 1
            stdout = _BoundedText(_OUTPUT_LIMIT)
            try:
                commands = _terminal_commands(source)
                for command in commands:
                    if self.mode == "browser":
                        if not isinstance(self.bridge, BridgeRun):
                            raise RuntimeError("Browser terminal bridge is unavailable")
                        response: Mapping[str, Any] | None = None
                        streamed = {
                            "stdout": _BoundedText(_OUTPUT_LIMIT),
                            "stderr": _BoundedText(_OUTPUT_LIMIT),
                        }
                        for event in self.bridge.request_events(
                            {"action": "execute_terminal", "command": command},
                            timeout=_TERMINAL_TIMEOUT,
                        ):
                            if event["type"] == "output":
                                stream = str(event["stream"])
                                text = str(event["text"])
                                streamed[stream].append(text)
                                if accepted := stdout.append(text):
                                    yield {**event, "text": accepted}
                            else:
                                response = event["response"]
                        if response is None:
                            raise RuntimeError("Browser terminal returned no response")
                        for stream in ("stdout", "stderr"):
                            text = str(response.get(stream, ""))
                            streamed_text = streamed[stream].raw_value
                            if text.startswith(streamed_text):
                                text = text[len(streamed_text) :]
                            if text:
                                if accepted := stdout.append(text):
                                    yield {"type": "output", "stream": stream, "text": accepted}
                        return_code = 0 if response.get("ok") is True else 1
                    else:
                        response = None
                        return_code = 0
                        for event in self._stream_host_terminal(command):
                            if event["type"] == "output":
                                text = str(event["text"])
                                if accepted := stdout.append(text):
                                    yield {**event, "text": accepted}
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
                            "stdout": stdout.value,
                            "error": f"{error_type}: {detail}",
                            "runtimeReset": False,
                        }
                        return
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                yield {
                    "type": "result",
                    "ok": False,
                    "cellId": cell_id,
                    "attempt": self._attempt,
                    "stdout": stdout.value,
                    "error": f"{type(exc).__name__}: {exc}",
                    "runtimeReset": isinstance(
                        exc,
                        (BridgeCancelled, subprocess.TimeoutExpired, TimeoutError),
                    ),
                }
                return

            self._execution_count += 1
            yield {
                "type": "result",
                "ok": True,
                "cellId": cell_id,
                "attempt": self._attempt,
                "executionCount": self._execution_count,
                "stdout": stdout.value,
                "result": None,
            }

    def _stream_host_terminal(self, command: str) -> Iterator[dict[str, Any]]:
        if self.python is None:
            raise RuntimeError("Host terminal requires a selected Python interpreter")

        tokens = shlex.split(command)
        if not tokens:
            raise ValueError("Terminal command cannot be empty")
        executable = self.python.executable
        # A pip/python command with shell syntax (a pipe, redirect or
        # separator) must go through the shell: the direct argv silently
        # dropped the extra tokens, so `!pip list | grep foo` ignored the
        # pipe entirely. A token ending in ";" is a separator attached to
        # the previous word (`pip list; echo done`); a ";" inside a quoted
        # -c string stays inside its token, so it is not misread.
        if any(token in _SHELL_OPERATORS or token.endswith(";") for token in tokens):
            argv = _shell_argv(command)
        elif tokens[0] in {"pip", "pip3"}:
            argv = [executable, "-m", "pip", *tokens[1:]]
        elif tokens[0] in {"python", "python3"}:
            argv = [executable, *tokens[1:]]
        else:
            argv = _shell_argv(command)

        yield from self._stream_host_process(argv, command=command)

    def list_packages(self) -> list[dict[str, str]]:
        if self.mode != "host" or self.python is None:
            raise ValueError("Package management requires a host Python runtime")
        with self._execution_lock:
            self._assert_open()
            self.touch()
            completed = subprocess.run(
                [
                    self.python.executable,
                    "-c",
                    (
                        "import importlib.metadata as m,json;"
                        "print(json.dumps([{'name':d.metadata.get('Name') or d.name,"
                        "'version':d.version} for d in m.distributions()]))"
                    ),
                ],
                cwd=self.cwd,
                env=self._host_process_env(),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(detail or "Could not list notebook packages")
            value = json.loads(completed.stdout)
            if not isinstance(value, list):
                raise TypeError("Python package list must be an array")
            packages = [
                {"name": str(item["name"]), "version": str(item["version"])}
                for item in value
                if isinstance(item, Mapping) and "name" in item and "version" in item
            ]
            return sorted(packages, key=lambda item: item["name"].lower())

    def stream_package_install(
        self, packages: Sequence[str]
    ) -> Generator[dict[str, Any], None, None]:
        if self.mode != "host" or self.python is None:
            raise ValueError("Package management requires a host Python runtime")
        with self._execution_lock:
            self._assert_open()
            self.touch()
            return_code = 1
            try:
                requirements = _package_requirements(packages)
                for event in self._stream_host_process(
                    [self.python.executable, "-m", "pip", "install", *requirements],
                    command="pip install " + " ".join(requirements),
                    timeout=_PACKAGE_INSTALL_TIMEOUT,
                ):
                    if event["type"] == "output":
                        yield event
                    else:
                        return_code = int(event["returnCode"])
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                yield {"type": "result", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                return
            yield {
                "type": "result",
                "ok": return_code == 0,
                "returnCode": return_code,
                "error": None if return_code == 0 else "Package installation failed",
            }

    def _stream_host_process(
        self,
        argv: Sequence[str],
        *,
        command: str,
        timeout: float = _TERMINAL_TIMEOUT,
    ) -> Iterator[dict[str, Any]]:
        if self.python is None:
            raise RuntimeError("Host process requires a selected Python interpreter")

        process = subprocess.Popen(
            list(argv),
            cwd=self.cwd,
            env=self._host_process_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            # A process group of its own, so a timeout can kill backgrounded
            # children that inherited the output pipes too; otherwise a
            # grandchild keeps the pipes open and the pump threads never see
            # EOF.
            start_new_session=True,
        )
        with self._state_lock:
            if self._closed:
                process.terminate()
                raise BridgeCancelled("Notebook session was interrupted")
            self._active_terminal_process = process
        output: Queue[tuple[str, str | None]] = Queue(maxsize=128)
        stop = threading.Event()

        def offer(item: tuple[str, str | None]) -> bool:
            """Queue an output item unless the consumer has gone away."""
            while True:
                try:
                    output.put(item, timeout=0.1)
                    return True
                except Full:
                    if stop.is_set():
                        return False

        def pump(stream_name: str, stream: Any) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while True:
                    try:
                        chunk = os.read(stream.fileno(), 4096)
                    except (OSError, ValueError):
                        break
                    if not chunk:
                        break
                    if text := decoder.decode(chunk):
                        if not offer((stream_name, text)):
                            return
                if text := decoder.decode(b"", final=True):
                    offer((stream_name, text))
            finally:
                with suppress(OSError, ValueError):
                    stream.close()
                offer((stream_name, None))

        if process.stdout is None or process.stderr is None:
            process.kill()
            raise RuntimeError("Terminal process output pipes are unavailable")
        readers = [
            threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout
        open_streams = len(readers)
        try:
            while open_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process_tree(process)
                    raise subprocess.TimeoutExpired(command, timeout)
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
            stop.set()
            with self._state_lock:
                if self._active_terminal_process is process:
                    self._active_terminal_process = None
            _terminate_process_tree(process)
            for reader in readers:
                reader.join(timeout=2)
            with suppress(OSError, ValueError):
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def _host_process_env(self) -> dict[str, str]:
        if self.python is None:
            raise RuntimeError("Host process requires a selected Python interpreter")
        env = os.environ.copy()
        python_path = Path(self.python.executable)
        env["PATH"] = os.pathsep.join([str(python_path.parent), env.get("PATH", "")]).rstrip(
            os.pathsep
        )
        if self.python.environment:
            env["VIRTUAL_ENV"] = self.python.environment
        return env

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
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            terminal_process = self._active_terminal_process
        self._cancellation.cancel()
        if isinstance(self.bridge, BridgeRun):
            self.bridge.cancel()
        else:
            self.bridge.close()
        if terminal_process is not None:
            _terminate_process_tree(terminal_process)
        with self._execution_lock:
            self._session.close()

    @property
    def last_activity(self) -> float:
        with self._state_lock:
            return self._last_activity

    def touch(self) -> None:
        with self._state_lock:
            self._last_activity = time.monotonic()

    def _assert_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Notebook session is closed")


class NotebookSessionManager:
    def __init__(
        self,
        *,
        cwd: Path | None = None,
        explicit_pythons: Sequence[str | Path] = (),
        host_environment: HostEnvironmentProvider | None = None,
        interactive_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.pythons = discover_host_pythons(explicit_pythons)
        self._host_environment = host_environment or HostEnvironmentManager()
        self._interactive_options = dict(interactive_options or {})
        self._sessions: dict[str, NotebookSession] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        mode: Literal["browser", "host"] = "browser",
        python_id: str | None = None,
        session_snapshot: str | None = None,
    ) -> NotebookSession:
        python = None
        if mode == "host":
            selected = self._resolve_python(python_id)
            prepared = self._host_environment.prepare(
                executable=selected.executable,
                version=selected.version,
                cwd=self.cwd,
            )
            python = HostPython(
                id=selected.id,
                executable=prepared.executable,
                version=selected.version,
                label=f"{selected.label} - Kedi Notebook environment",
                current=selected.current,
                explicit=selected.explicit,
                managed=True,
                base_executable=selected.executable,
                environment=prepared.directory,
            )
        session_id = uuid4().hex
        # Hold the lock while constructing and registering the session: the
        # constructor starts the bridge (and may be slow in kedi.interactive),
        # and a close_all() that ran between construction and registration
        # would miss the session and leak it. close_all() blocks on the same
        # lock, then closes the registered session.
        with self._lock:
            session = NotebookSession(
                session_id=session_id,
                mode=mode,
                cwd=self.cwd,
                python=python,
                interactive_options=self._interactive_options,
                session_snapshot=session_snapshot,
            )
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> NotebookSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError("Notebook session was not found")
        session.touch()
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

    def reconfigure(self, *, interactive_options: Mapping[str, Any]) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._interactive_options = dict(interactive_options)
        for session in sessions:
            session.close()

    def cleanup_stale(self, *, max_age: float) -> int:
        cutoff = time.monotonic() - max_age
        with self._lock:
            stale_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session.last_activity < cutoff
            ]
            sessions = [self._sessions.pop(session_id) for session_id in stale_ids]
        for session in sessions:
            session.close()
        return len(sessions)

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
        if metadata.get("releaselevel") != "final":
            if is_explicit:
                raise ValueError("Kedi notebook host execution requires a final Python release")
            continue
        if (major, minor) < (3, 11):
            if is_explicit:
                raise ValueError("Kedi notebook host execution requires Python 3.11 or newer")
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


def _load_session_snapshot(
    snapshot: str,
    *,
    executor: PlaygroundExecutor | PyodideExecutor,
) -> kedi.InteractiveSession:
    with tempfile.TemporaryDirectory(prefix="kedi-notebook-restore-") as directory:
        path = Path(directory) / "session.json"
        try:
            payload = base64.b64decode(snapshot, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Notebook session snapshot is not valid base64") from exc
        path.write_bytes(payload)
        return kedi.load_session(path, executor=executor)


def _python_metadata(executable: Path) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-c",
                (
                    "import json,ssl,sys,xml.parsers.expat; "
                    "print(json.dumps({"
                    "'version': list(sys.version_info[:3]),"
                    "'releaselevel': sys.version_info.releaselevel"
                    "}))"
                ),
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
    media = _media_result(value)
    if media is not None:
        return media
    try:
        serialized = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        # Non-finite floats (NaN, Infinity) are not JSON, so Starlette's
        # JSONResponse rejected the whole cell with a 400. Show them as the
        # text the user would read instead of failing the cell.
        sanitized = _sanitize_json_value(value)
        if sanitized is _UNSANITIZABLE:
            rendered = _truncate_output(repr(value))
            return {"kind": "repr", "type": type(value).__name__, "value": rendered}
        serialized = json.dumps(sanitized, allow_nan=False)
        if len(serialized) > _OUTPUT_LIMIT:
            return {
                "kind": "repr",
                "type": type(value).__name__,
                "value": _truncate_output(serialized),
                "truncated": True,
            }
        return {"kind": "json", "type": type(value).__name__, "value": sanitized}
    if len(serialized) > _OUTPUT_LIMIT:
        return {
            "kind": "repr",
            "type": type(value).__name__,
            "value": _truncate_output(serialized),
            "truncated": True,
        }
    return {"kind": "json", "type": type(value).__name__, "value": value}


_UNSANITIZABLE: Final = object()


def _sanitize_json_value(value: object, *, _seen: frozenset[int] = frozenset()) -> object:
    """Replace non-finite floats with their names for JSON output.

    Returns ``_UNSANITIZABLE`` for values that are not plain JSON data (custom
    objects, sets, cyclic structures); those keep the ``repr`` fallback.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        if id(value) in _seen:
            return _UNSANITIZABLE
        seen = _seen | {id(value)}
        sanitized: dict[object, object] = {}
        for key, item in value.items():
            clean_key = _sanitize_json_value(key, _seen=seen) if isinstance(key, float) else key
            if clean_key is _UNSANITIZABLE:
                return _UNSANITIZABLE
            clean_item = _sanitize_json_value(item, _seen=seen)
            if clean_item is _UNSANITIZABLE:
                return _UNSANITIZABLE
            sanitized[key] = clean_item
        return sanitized
    if isinstance(value, (list, tuple)):
        if id(value) in _seen:
            return _UNSANITIZABLE
        seen = _seen | {id(value)}
        items: list[object] = []
        for item in value:
            clean_item = _sanitize_json_value(item, _seen=seen)
            if clean_item is _UNSANITIZABLE:
                return _UNSANITIZABLE
            items.append(clean_item)
        return items
    return _UNSANITIZABLE


def _media_displays(displays: list[dict[str, object]]) -> AbstractContextManager[None]:
    """Collect media shown with ``> show: `value``` as previews instead of printed labels."""

    try:
        from kedi.display import display_hook
    except ImportError:  # a Kedi release without display hooks
        return nullcontext()

    def show(value: object) -> bool:
        media = _media_result(value)
        if media is None:
            return False
        displays.append(media)
        return True

    return display_hook(show)


def _cell_result(value: object, displays: list[dict[str, object]]) -> dict[str, object] | None:
    if not displays:
        return _json_result(value)
    result = _json_result(value)
    return {"kind": "displays", "items": [*displays, *([] if result is None else [result])]}


def _media_result(value: object) -> dict[str, object] | None:
    """A media value's label, with its bytes for an image or audio the browser can preview."""

    try:
        from kedi.media import as_media
    except ImportError:  # a Kedi release without media values
        return None
    media = as_media(value)
    if media is None:
        return None
    result: dict[str, object] = {
        "kind": "media",
        "type": type(media).__name__,
        "value": media.label,
    }
    if (
        media.data is not None
        and media.media_type in _MEDIA_PREVIEW_TYPES
        and len(media.data) <= _MEDIA_PREVIEW_LIMIT
    ):
        result["mediaType"] = media.media_type
        result["data"] = base64.b64encode(media.data).decode("ascii")
    return result


class _BoundedText:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: list[str] = []
        self._size = 0
        self._truncated = False

    def append(self, text: str) -> str:
        remaining = self._limit - self._size
        accepted = ""
        if remaining > 0:
            accepted = text[:remaining]
            self._parts.append(accepted)
            self._size += len(accepted)
        if len(text) > max(remaining, 0) and not self._truncated:
            self._truncated = True
            accepted += "\n[output truncated by Kedi Notebook]"
        return accepted

    @property
    def value(self) -> str:
        suffix = "\n[output truncated by Kedi Notebook]" if self._truncated else ""
        return "".join(self._parts) + suffix

    @property
    def raw_value(self) -> str:
        return "".join(self._parts)


def _truncate_output(value: str) -> str:
    output = _BoundedText(_OUTPUT_LIMIT)
    output.append(value)
    return output.value


def _requires_runtime_reset(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current, (BridgeCancelled, RunCancelled, TimeoutError, subprocess.TimeoutExpired)
        ):
            return True
        original = getattr(current, "original", None)
        current = (
            original
            if isinstance(original, BaseException)
            else current.__cause__ or current.__context__
        )
    return False


def _shell_argv(command: str) -> list[str]:
    # /bin/sh does not exist on Windows; cmd.exe supports the same pipes and
    # redirects the shell branch relies on.
    if os.name == "nt":
        return ["cmd.exe", "/c", command]
    return ["/bin/sh", "-lc", command]


_SHELL_OPERATORS: Final = frozenset({"|", "||", "&", "&&", ">", ">>", "<", "<<", ";"})


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


def _package_requirements(packages: Sequence[str]) -> list[str]:
    if not packages or len(packages) > 50:
        raise ValueError("Provide between 1 and 50 package requirements")
    requirements: list[str] = []
    for package in packages:
        requirement = package.strip()
        if not requirement or len(requirement) > 300:
            raise ValueError("Package requirements must contain 1 to 300 characters")
        if requirement.startswith("-") or any(ord(character) < 32 for character in requirement):
            raise ValueError(f"Invalid package requirement: {package!r}")
        requirements.append(requirement)
    return requirements


__all__ = [
    "HostPython",
    "HostPythonBridge",
    "NotebookSession",
    "NotebookSessionManager",
    "discover_host_pythons",
]
