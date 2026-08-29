from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any, BinaryIO

from kedi.lsp.python_virtual import compute_python_virtual_document

JsonObject = dict[str, Any]
Position = dict[str, int]
Range = dict[str, Position]


class PyrightServer:
    """Persistent Pyright language-server bridge for embedded Kedi Python."""

    def __init__(self, *, timeout: float = 5) -> None:
        self._timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[int, queue.Queue[JsonObject | BaseException]] = {}
        self._next_request_id = 0
        self._document_version = 0
        self._document_open = False
        self._lifecycle_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._document_lock = threading.Lock()
        self._uri = "file:///tmp/kedi-notebook-embedded.py"

    def hover(self, source: str, line: int, character: int) -> JsonObject | None:
        virtual = compute_python_virtual_document(
            source,
            source_path=None,
            focus_line=line,
        )
        virtual_position = _source_position_to_virtual(
            virtual,
            {"line": line, "character": character},
        )
        if virtual_position is None:
            return None

        with self._document_lock:
            self._ensure_started()
            self._sync_document(str(virtual["text"]))
            result = self._request(
                "textDocument/hover",
                {
                    "textDocument": {"uri": self._uri},
                    "position": virtual_position,
                },
            )

        if not isinstance(result, Mapping):
            return None
        contents = result.get("contents")
        if contents is None:
            return None
        virtual_range = result.get("range")
        source_range = (
            _virtual_range_to_source(virtual, virtual_range)
            if isinstance(virtual_range, Mapping)
            else None
        )
        return {
            "contents": _normalize_hover_contents(contents),
            "range": source_range,
        }

    def close(self) -> None:
        with self._lifecycle_lock:
            process = self._process
            if process is None:
                return
            if process.poll() is None:
                try:
                    self._request("shutdown", None)
                    self._notify("exit", None)
                    process.wait(timeout=1)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    process.terminate()
            self._process = None
            self._reader = None
            self._document_open = False

    def _ensure_started(self) -> None:
        with self._lifecycle_lock:
            if self._process is not None and self._process.poll() is None:
                return
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from basedpyright.langserver import main; main()",
                    "--stdio",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdin is None or process.stdout is None:
                process.terminate()
                raise RuntimeError("Pyright language server did not open stdio")
            self._process = process
            self._reader = threading.Thread(
                target=self._read_loop,
                args=(process.stdout,),
                name="kedi-notebook-pyright",
                daemon=True,
            )
            self._reader.start()
            self._request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": None,
                    "capabilities": {
                        "workspace": {"configuration": True},
                    },
                },
            )
            self._notify("initialized", {})

    def _sync_document(self, text: str) -> None:
        self._document_version += 1
        if not self._document_open:
            self._notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": self._uri,
                        "languageId": "python",
                        "version": self._document_version,
                        "text": text,
                    }
                },
            )
            self._document_open = True
            return
        self._notify(
            "textDocument/didChange",
            {
                "textDocument": {
                    "uri": self._uri,
                    "version": self._document_version,
                },
                "contentChanges": [{"text": text}],
            },
        )

    def _request(self, method: str, params: Any) -> Any:
        self._next_request_id += 1
        request_id = self._next_request_id
        response_queue: queue.Queue[JsonObject | BaseException] = queue.Queue(maxsize=1)
        self._pending[request_id] = response_queue
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        try:
            response = response_queue.get(timeout=self._timeout)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            raise RuntimeError(f"Pyright request timed out: {method}") from exc
        if isinstance(response, BaseException):
            raise RuntimeError("Pyright language server stopped") from response
        if "error" in response:
            raise RuntimeError(f"Pyright request failed: {response['error']}")
        return response.get("result")

    def _notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("Pyright language server is not running")
        payload = json.dumps(message, separators=(",", ":")).encode()
        with self._write_lock:
            process.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
            process.stdin.write(payload)
            process.stdin.flush()

    def _read_loop(self, stream: BinaryIO) -> None:
        failure: BaseException | None = None
        try:
            while True:
                message = _read_message(stream)
                if "method" in message and "id" in message:
                    self._answer_server_request(message)
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int):
                    response_queue = self._pending.pop(request_id, None)
                    if response_queue is not None:
                        response_queue.put(message)
        except BaseException as exc:
            failure = exc
        finally:
            error = failure or RuntimeError("Pyright language server closed its output")
            for response_queue in tuple(self._pending.values()):
                response_queue.put(error)
            self._pending.clear()

    def _answer_server_request(self, message: JsonObject) -> None:
        method = message["method"]
        if method == "workspace/configuration":
            items = message.get("params", {}).get("items", [])
            result = [_configuration_for(item) for item in items]
        else:
            result = None
        self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})


def _read_message(stream: BinaryIO) -> JsonObject:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("Pyright language server closed stdout")
        if line in (b"\r\n", b"\n"):
            break
        name, separator, value = line.decode().partition(":")
        if not separator:
            raise RuntimeError("Invalid Pyright response header")
        headers[name.lower()] = value.strip()
    length = int(headers["content-length"])
    message = json.loads(stream.read(length))
    if not isinstance(message, dict):
        raise TypeError("Pyright response must be an object")
    return message


def _configuration_for(item: Any) -> JsonObject:
    section = item.get("section") if isinstance(item, Mapping) else None
    if isinstance(section, str) and section.endswith("analysis"):
        return {
            "diagnosticMode": "openFilesOnly",
            "typeCheckingMode": "basic",
        }
    return {}


def _source_position_to_virtual(
    virtual: Mapping[str, Any],
    position: Position,
) -> Position | None:
    entries = [
        *virtual.get("mappings", []),
        *(
            {
                "sourceRange": region["sourceRange"],
                "virtualRange": region["virtualRange"],
            }
            for region in virtual.get("ranges", [])
            if region.get("virtualRange") is not None
        ),
    ]
    for entry in entries:
        source_range = entry["sourceRange"]
        if _contains(source_range, position):
            return _translate_position(source_range, entry["virtualRange"], position)
    return None


def _virtual_position_to_source(
    virtual: Mapping[str, Any],
    position: Position,
) -> Position | None:
    entries = [
        *virtual.get("mappings", []),
        *(
            {
                "sourceRange": region["sourceRange"],
                "virtualRange": region["virtualRange"],
            }
            for region in virtual.get("ranges", [])
            if region.get("virtualRange") is not None
        ),
        *virtual.get("symbols", []),
    ]
    for entry in entries:
        virtual_range = entry["virtualRange"]
        if _contains(virtual_range, position):
            return _translate_position(virtual_range, entry["sourceRange"], position)
    return None


def _virtual_range_to_source(
    virtual: Mapping[str, Any],
    range_: Mapping[str, Any],
) -> Range | None:
    start_raw = range_.get("start")
    end_raw = range_.get("end")
    if not isinstance(start_raw, Mapping) or not isinstance(end_raw, Mapping):
        return None
    start = _position(start_raw)
    end = _position(end_raw)
    source_start = _virtual_position_to_source(virtual, start)
    source_end = _virtual_position_to_source(virtual, end)
    if source_end is None and end["character"] > 0:
        boundary = {"line": end["line"], "character": end["character"] - 1}
        source_end = _virtual_position_to_source(virtual, boundary)
        if source_end is not None:
            source_end = {
                "line": source_end["line"],
                "character": source_end["character"] + 1,
            }
    if source_start is None or source_end is None:
        return None
    return {"start": source_start, "end": source_end}


def _contains(range_: Mapping[str, Any], position: Position) -> bool:
    start = _position(range_["start"])
    end = _position(range_["end"])
    point = (position["line"], position["character"])
    return (start["line"], start["character"]) <= point <= (end["line"], end["character"])


def _translate_position(
    from_range: Mapping[str, Any],
    to_range: Mapping[str, Any],
    position: Position,
) -> Position:
    source_start = _position(from_range["start"])
    target_start = _position(to_range["start"])
    return {
        "line": target_start["line"] + position["line"] - source_start["line"],
        "character": (
            position["character"] + target_start["character"] - source_start["character"]
        ),
    }


def _position(value: Mapping[str, Any]) -> Position:
    return {"line": int(value["line"]), "character": int(value["character"])}


def _normalize_hover_contents(contents: Any) -> JsonObject:
    if isinstance(contents, Mapping):
        kind = contents.get("kind")
        value = contents.get("value")
        if isinstance(kind, str) and isinstance(value, str):
            return {"kind": kind, "value": value}
    if isinstance(contents, str):
        return {"kind": "plaintext", "value": contents}
    if isinstance(contents, Sequence):
        values = [
            item if isinstance(item, str) else item.get("value", "")
            for item in contents
            if isinstance(item, (str, Mapping))
        ]
        return {"kind": "markdown", "value": "\n\n".join(values)}
    return {"kind": "plaintext", "value": str(contents)}


__all__ = ["PyrightServer"]
