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

    def completion(
        self,
        source: str,
        line: int,
        character: int,
    ) -> list[JsonObject] | None:
        virtual, virtual_position = self._virtual_position(source, line, character)
        if virtual_position is None:
            return None
        with self._document_lock:
            self._ensure_started()
            self._sync_document(str(virtual["text"]))
            result = self._request(
                "textDocument/completion",
                {
                    "textDocument": {"uri": self._uri},
                    "position": virtual_position,
                    "context": {"triggerKind": 1},
                },
            )
        raw_items = result.get("items", []) if isinstance(result, Mapping) else result
        if not isinstance(raw_items, Sequence):
            return []
        return [
            mapped
            for item in raw_items
            if isinstance(item, Mapping)
            and (mapped := _completion_item_to_source(virtual, item)) is not None
        ]

    def references(
        self,
        source: str,
        line: int,
        character: int,
        *,
        include_declaration: bool,
    ) -> list[JsonObject] | None:
        virtual, virtual_position = self._virtual_position(source, line, character)
        if virtual_position is None:
            return None
        with self._document_lock:
            self._ensure_started()
            self._sync_document(str(virtual["text"]))
            result = self._request(
                "textDocument/references",
                {
                    "textDocument": {"uri": self._uri},
                    "position": virtual_position,
                    "context": {"includeDeclaration": include_declaration},
                },
            )
        if not isinstance(result, Sequence):
            return []
        references: list[JsonObject] = []
        for location in result:
            if not isinstance(location, Mapping) or location.get("uri") != self._uri:
                continue
            range_ = location.get("range")
            if (
                isinstance(range_, Mapping)
                and (source_range := _virtual_range_to_source(virtual, range_)) is not None
            ):
                references.append({"range": source_range})
        return references

    def prepare_rename(
        self,
        source: str,
        line: int,
        character: int,
    ) -> JsonObject | None:
        virtual, virtual_position = self._virtual_position(source, line, character)
        if virtual_position is None:
            return None
        with self._document_lock:
            self._ensure_started()
            self._sync_document(str(virtual["text"]))
            result = self._request(
                "textDocument/prepareRename",
                {
                    "textDocument": {"uri": self._uri},
                    "position": virtual_position,
                },
            )
        if not isinstance(result, Mapping):
            return None
        virtual_range = result.get("range", result)
        if not isinstance(virtual_range, Mapping):
            return None
        source_range = _virtual_range_to_source(virtual, virtual_range)
        if source_range is None:
            return None
        return {
            "range": source_range,
            "placeholder": result.get("placeholder"),
        }

    def rename(
        self,
        source: str,
        line: int,
        character: int,
        new_name: str,
    ) -> list[JsonObject] | None:
        virtual, virtual_position = self._virtual_position(source, line, character)
        if virtual_position is None:
            return None
        with self._document_lock:
            self._ensure_started()
            self._sync_document(str(virtual["text"]))
            result = self._request(
                "textDocument/rename",
                {
                    "textDocument": {"uri": self._uri},
                    "position": virtual_position,
                    "newName": new_name,
                },
            )
        if not isinstance(result, Mapping):
            return []
        changes = result.get("changes")
        if isinstance(changes, Mapping):
            edits = changes.get(self._uri, [])
        else:
            document_changes = result.get("documentChanges")
            edits = []
            if isinstance(document_changes, Sequence):
                for change in document_changes:
                    if not isinstance(change, Mapping):
                        continue
                    document = change.get("textDocument")
                    if isinstance(document, Mapping) and document.get("uri") == self._uri:
                        candidate_edits = change.get("edits", [])
                        if isinstance(candidate_edits, Sequence):
                            edits.extend(candidate_edits)
        if not isinstance(edits, Sequence):
            return []
        return [
            mapped
            for edit in edits
            if isinstance(edit, Mapping)
            and (mapped := _text_edit_to_source(virtual, edit)) is not None
        ]

    def diagnostics(self, source: str) -> list[JsonObject]:
        virtual = compute_python_virtual_document(source, source_path=None)
        if not virtual.get("ranges") and not virtual.get("mappings"):
            return []

        with self._document_lock:
            self._ensure_started()
            self._sync_document(str(virtual["text"]))
            report = self._request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": self._uri}},
            )
            diagnostics = report.get("items", []) if isinstance(report, Mapping) else []

        mapped: list[JsonObject] = []
        for diagnostic in diagnostics:
            range_ = diagnostic.get("range")
            if not isinstance(range_, Mapping):
                continue
            source_range = _virtual_range_to_source(virtual, range_)
            if source_range is None:
                continue
            mapped.append({**diagnostic, "range": source_range, "source": "pyright"})
        return mapped

    def _virtual_position(
        self,
        source: str,
        line: int,
        character: int,
    ) -> tuple[JsonObject, Position | None]:
        virtual = compute_python_virtual_document(
            source,
            source_path=None,
            focus_line=line,
        )
        return virtual, _source_position_to_virtual(
            virtual,
            {"line": line, "character": character},
        )

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


def _text_edit_to_source(
    virtual: Mapping[str, Any],
    edit: Mapping[str, Any],
) -> JsonObject | None:
    range_ = edit.get("range")
    new_text = edit.get("newText")
    if not isinstance(range_, Mapping) or not isinstance(new_text, str):
        return None
    source_range = _virtual_range_to_source(virtual, range_)
    return None if source_range is None else {"range": source_range, "newText": new_text}


def _completion_item_to_source(
    virtual: Mapping[str, Any],
    item: Mapping[str, Any],
) -> JsonObject | None:
    label = item.get("label")
    if not isinstance(label, str):
        return None
    mapped: JsonObject = {
        key: value
        for key, value in item.items()
        if key not in {"textEdit", "additionalTextEdits", "data"}
    }
    text_edit = item.get("textEdit")
    if isinstance(text_edit, Mapping):
        mapped_edit = _text_edit_to_source(virtual, text_edit)
        if mapped_edit is not None:
            mapped["textEdit"] = mapped_edit
    additional = item.get("additionalTextEdits")
    if isinstance(additional, Sequence):
        mapped["additionalTextEdits"] = [
            mapped_edit
            for edit in additional
            if isinstance(edit, Mapping)
            and (mapped_edit := _text_edit_to_source(virtual, edit)) is not None
        ]
    return mapped


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
