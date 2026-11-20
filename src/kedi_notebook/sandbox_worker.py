from __future__ import annotations

import builtins
import contextlib
import io
import itertools
import json
import os
import sys
import tokenize
import traceback
import types
import typing
from collections.abc import Mapping
from typing import Any

typing_extensions_module: types.ModuleType
try:
    import typing_extensions as _typing_extensions
except ImportError:  # Python 3.11+ includes the subset needed by the worker.
    typing_extensions_module = typing
else:
    typing_extensions_module = _typing_extensions

base_model_type: type[Any] | None
try:
    from pydantic import BaseModel as _BaseModel
except ImportError:
    base_model_type = None
else:
    base_model_type = _BaseModel

_REFERENCE_KEY = "__kedi_notebook_ref__"
_PROCEDURE_KEY = "__kedi_playground_procedure__"
_MEDIA_KEY = "__kedi_media__"  # kedi.media.BRIDGE_KEY
_RESPONSE_PREFIX = "__KEDI_NOTEBOOK_RESPONSE__"
def _protocol_fd() -> int:
    try:
        return int(os.environ.get("KEDI_NOTEBOOK_PROTOCOL_FD", "3"))
    except ValueError:
        return 3


_PROTOCOL_FD = _protocol_fd()
# Opaque references to values that cannot cross the bridge, oldest first.
# The registry is bounded: an evicted handle simply stops resolving.
_OBJECT_REGISTRY: dict[str, Any] = {}
_MAX_OBJECT_REGISTRY = 1024
_REGISTRY_COUNTER = itertools.count(1)
# The bridge protocol keeps the process streams while user output is redirected.
_PROTOCOL_IN = sys.stdin
_PROTOCOL_OUT = sys.stdout
# Output captured by each request being handled; nested requests stack on top.
_CAPTURES: list[tuple[io.StringIO, io.StringIO]] = []
# Requests whose Python may write Kedi bindings: only changed values are sent back.
_CODE_ACTIONS = frozenset({"evaluate_inline", "execute_block", "execute_side_effects"})


class KediProcedureError(RuntimeError):
    """A Kedi procedure called from Python failed in the notebook runtime."""


class _HostStdin(io.TextIOBase):
    """Standard input of host-runtime code. The notebook cannot answer prompts here."""

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1, /) -> str:
        raise _stdin_unavailable()

    def readline(self, size: int | None = -1, /) -> str:
        raise _stdin_unavailable()


def _stdin_unavailable() -> EOFError:
    return EOFError(
        "Standard input is not available in the host Python runtime; "
        "use the browser runtime to answer input() prompts"
    )


class _KediProcedure:
    """Calls a Kedi procedure in the notebook runtime and waits for its result."""

    def __init__(self, name: str) -> None:
        self.__name__ = self.__qualname__ = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _call_procedure(self.__name__, args, kwargs)

    def __repr__(self) -> str:
        return f"<Kedi procedure {self.__name__}>"


class _KediAttrDict(dict[str, Any]):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _KediOutput(io.StringIO):
    def write(self, value: str) -> int:
        return super().write(value)


def _from_json(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {_PROCEDURE_KEY}:
            return _KediProcedure(value[_PROCEDURE_KEY])
        if set(value) == {_MEDIA_KEY}:
            try:
                from kedi.media import from_bridge
            except ImportError as exc:
                raise RuntimeError("Media values need Kedi installed in this Python") from exc
            return from_bridge(value[_MEDIA_KEY])
        if set(value) == {_REFERENCE_KEY}:
            handle = value[_REFERENCE_KEY]
            try:
                return _OBJECT_REGISTRY[handle]
            except KeyError as exc:
                raise RuntimeError("Sandbox object reference is no longer available") from exc
        return _KediAttrDict({key: _from_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_from_json(item) for item in value]
    return value


_MAX_JSON_DEPTH = 100


def _to_json(value: Any, _seen: frozenset[int] | None = None, _depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _depth > _MAX_JSON_DEPTH:
        raise TypeError(
            f"Python value {type(value).__name__} is nested too deeply "
            "to cross the sandbox bridge"
        )
    if isinstance(value, _KediProcedure):
        return {_PROCEDURE_KEY: value.__name__}
    # A media value exists only once kedi.media is imported; never import Kedi here otherwise.
    media = sys.modules.get("kedi.media")
    if media is not None and isinstance(value, media.Media):
        return media.to_bridge(value)
    if callable(value):
        raise TypeError(f"Python callable {value!r} requires an opaque bridge reference")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Sandbox mappings require string keys")
        seen = _seen or frozenset()
        if id(value) in seen:
            raise TypeError(
                f"Python value {type(value).__name__} is circular and "
                "cannot cross the sandbox bridge"
            )
        seen = seen | {id(value)}
        return {key: _to_json(item, seen, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        seen = _seen or frozenset()
        if id(value) in seen:
            raise TypeError(
                f"Python value {type(value).__name__} is circular and "
                "cannot cross the sandbox bridge"
            )
        seen = seen | {id(value)}
        return [_to_json(item, seen, _depth + 1) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, (type, types.ModuleType)):
        seen = _seen or frozenset()
        if id(value) in seen:
            raise TypeError(
                f"Python value {type(value).__name__} is circular and "
                "cannot cross the sandbox bridge"
            )
        seen = seen | {id(value)}
        return {
            key: _to_json(item, seen, _depth + 1)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise TypeError(f"Python value {type(value).__name__} cannot cross the sandbox bridge")


def _type_descriptor(value: Any) -> dict[str, Any]:
    if value is type(None):
        return {"kind": "none"}
    if isinstance(value, type) and getattr(builtins, value.__name__, None) is value:
        return {"kind": "builtin", "name": value.__qualname__}
    if value is typing.Any:
        return {"kind": "typing", "name": "Any"}
    media = sys.modules.get("kedi.media")
    if media is not None and isinstance(value, type) and issubclass(value, media.Media):
        return {"kind": "media", "name": value.__name__}
    if (
        base_model_type is not None
        and isinstance(value, type)
        and issubclass(value, base_model_type)
    ):
        fields: list[dict[str, Any]] = []
        for name, field in value.model_fields.items():
            item = {
                "name": name,
                "annotation": _type_descriptor(field.annotation),
                "required": field.is_required(),
            }
            if field.description:
                item["description"] = field.description
            if field.examples:
                item["examples"] = _to_json(field.examples)
            if not field.is_required():
                item["default"] = _to_json(field.get_default(call_default_factory=True))
            fields.append(item)
        return {"kind": "pydantic_model", "name": value.__name__, "fields": fields}

    origin = typing.get_origin(value)
    args = typing.get_args(value)
    if origin in (typing.Union, types.UnionType):
        return {"kind": "union", "args": [_type_descriptor(arg) for arg in args]}
    if origin is typing.Literal:
        return {"kind": "literal", "values": [_to_json(arg) for arg in args]}
    if origin is not None:
        origin_descriptor = _type_descriptor(origin)
        if not args:
            return origin_descriptor
        return {
            "kind": "generic",
            "origin": origin_descriptor,
            "args": [_type_descriptor(arg) for arg in args],
        }
    raise TypeError("Python-defined types cannot cross the sandbox bridge yet")


def _indent_block_lines(code: str) -> list[str]:
    """Indent ``code`` for the wrapper function without touching string contents.

    Lines strictly inside a multi-line string literal keep their text: adding
    spaces there would change the string's value.
    """
    lines = code.splitlines()
    string_rows: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        for token in tokens:
            if token.type == tokenize.STRING:
                start_row, end_row = token.start[0], token.end[0]
                if end_row > start_row:
                    # Every line after the opening line up to and including
                    # the closing line: leading spaces there are string
                    # content, and the line never needs indenting because it
                    # continues the statement the string opened.
                    string_rows.update(range(start_row + 1, end_row + 1))
    except (tokenize.TokenError, SyntaxError):
        # Unparseable code: indent everything and let compile() report it.
        string_rows = set()
    return [
        f"    {line}" if index + 1 not in string_rows else line
        for index, line in enumerate(lines)
    ]


def _run_block(namespace: dict[str, Any], code: str, sync_names: list[str]) -> Any:
    header = ["def __kedi_wrapper__():"]
    if sync_names:
        header.append(f"    global {', '.join(sync_names)}")
    body = _indent_block_lines(code)
    source = "\n".join(header + body + ["    pass"])
    exec(compile(source, "<kedi-sandbox:block>", "exec"), namespace, namespace)
    wrapper = namespace.pop("__kedi_wrapper__")
    return wrapper()


def _serializable_namespace(
    namespace: Mapping[str, Any],
    names: list[str],
    installed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Encode ``names`` for the host, leaving out values still equal to ``installed``."""
    result: dict[str, Any] = {}
    for name in names:
        if name.startswith("_") or name not in namespace:
            continue
        value = namespace[name]
        try:
            encoded = _to_json(value)
        except TypeError:
            handle = f"ref-{next(_REGISTRY_COUNTER)}"
            _OBJECT_REGISTRY[handle] = value
            while len(_OBJECT_REGISTRY) > _MAX_OBJECT_REGISTRY:
                _OBJECT_REGISTRY.pop(next(iter(_OBJECT_REGISTRY)))
            encoded = {_REFERENCE_KEY: handle}
        if installed is None or name not in installed or installed[name] != encoded:
            result[name] = encoded
    return result


def _send(message: Mapping[str, Any]) -> None:
    _PROTOCOL_OUT.write(_RESPONSE_PREFIX + json.dumps(message, separators=(",", ":")) + "\n")
    _PROTOCOL_OUT.flush()


def _exchange(message: Mapping[str, Any]) -> dict[str, Any]:
    _send(message)
    line = _PROTOCOL_IN.readline()
    if not line:
        raise KediProcedureError("The notebook runtime closed during a procedure call")
    request = json.loads(line)
    if not isinstance(request, dict):
        raise TypeError("Sandbox request must be an object")
    return request


def _take(buffer: io.StringIO) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate()
    return value


def _call_procedure(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    stdout, stderr = _CAPTURES[-1]
    message = {
        "ok": True,
        "call": {"procedure": name, "args": _to_json(list(args)), "kwargs": _to_json(kwargs)},
        "stdout": _take(stdout),
        "stderr": _take(stderr),
    }
    # Like a Kedi procedure called from in-process Python, the caller keeps its own view
    # of the namespace; nested requests install the runtime's values for the procedure.
    snapshot = dict(_USER_NAMESPACE)
    try:
        reply = _exchange(message)
        while reply.get("operation") != "procedure_result":
            reply = _exchange(handle_request(reply))
    finally:
        _USER_NAMESPACE.clear()
        _USER_NAMESPACE.update(snapshot)
    if reply.get("ok") is True:
        return _from_json(reply.get("result"))
    raise KediProcedureError(f"{reply.get('errorType') or 'Error'}: {reply.get('error') or ''}")


def _initial_namespace() -> dict[str, Any]:
    return {
        "__name__": "__kedi_nsjail__",
        "__builtins__": builtins.__dict__,
        "builtins": builtins,
        "typing": typing,
        "typing_extensions": typing_extensions_module,
        **{
            name: getattr(module, name)
            for module in (typing, typing_extensions_module)
            for name in module.__all__
            if not name.startswith("_") and hasattr(module, name)
        },
    }


_USER_NAMESPACE = _initial_namespace()


def execute_request(request: Mapping[str, Any]) -> dict[str, Any]:
    installed = request.get("env", {})
    _USER_NAMESPACE.update({name: _from_json(value) for name, value in installed.items()})
    action = request["action"]
    code = request["code"]
    sync_names = request.get("syncNames", [])

    if action == "evaluate_inline":
        try:
            compiled = compile(code, "<kedi-sandbox:inline>", "eval")
            result = eval(compiled, _USER_NAMESPACE, _USER_NAMESPACE)
        except SyntaxError:
            _run_block(_USER_NAMESPACE, code, sync_names)
            result = ""
    elif action == "execute_block":
        result = _run_block(_USER_NAMESPACE, code, sync_names)
    elif action == "execute_side_effects":
        compiled = compile(code, "<kedi-sandbox:side-effects>", "exec")
        exec(compiled, _USER_NAMESPACE, _USER_NAMESPACE)
        result = None
    elif action == "execute_prelude":
        exec(
            compile(code, "<kedi-sandbox:prelude>", "exec"),
            _USER_NAMESPACE,
            _USER_NAMESPACE,
        )
        result = None
    elif action == "evaluate_type_expression":
        compiled = compile(code, "<kedi-sandbox:type>", "eval")
        value = eval(compiled, _USER_NAMESPACE, _USER_NAMESPACE)
        try:
            return {"ok": True, "result": _to_json(value), "env": {}}
        except TypeError:
            return {"ok": True, "type": _type_descriptor(value), "env": {}}
    else:
        raise ValueError(f"Unknown sandbox action: {action!r}")

    return {
        "ok": True,
        "result": _to_json(result),
        "env": _serializable_namespace(
            _USER_NAMESPACE, sync_names, installed if action in _CODE_ACTIONS else None
        ),
    }


def handle_request(request: Mapping[str, Any]) -> dict[str, Any]:
    stdout = _KediOutput()
    stderr = _KediOutput()
    _CAPTURES.append((stdout, stderr))
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                response = execute_request(request)
            except Exception as exc:  # noqa: BLE001 - sandbox boundary serializes errors.
                response = {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
    finally:
        _CAPTURES.pop()
    response["stdout"] = stdout.getvalue()
    response["stderr"] = stderr.getvalue()
    return response


def _reserve_protocol_fds() -> None:
    """Keep the bridge protocol on its own descriptors.

    Descriptor 0 then reads nothing, so neither ``input()`` nor a child process can consume
    bridge messages and block the worker. Descriptor 1 is pointed at /dev/null, so a raw
    ``os.write(1, ...)`` from user code cannot forge a protocol reply: replies travel on
    a dedicated pipe (descriptor 3) that user code is never handed.
    """
    global _PROTOCOL_IN, _PROTOCOL_OUT
    _PROTOCOL_IN = os.fdopen(os.dup(0), "r", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    try:
        _PROTOCOL_OUT = os.fdopen(_PROTOCOL_FD, "w", encoding="utf-8")
    except OSError:
        # No dedicated pipe (started by hand): fall back to stdout.
        _PROTOCOL_OUT = sys.stdout
    else:
        devnull_w = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_w, 1)
        os.close(devnull_w)
    sys.stdin = _HostStdin()


def main() -> None:
    _reserve_protocol_fds()
    while line := _PROTOCOL_IN.readline():
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError("Sandbox request must be an object")
            response = handle_request(request)
        except Exception as exc:  # noqa: BLE001 - line protocol must stay alive.
            response = {
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "stdout": "",
                "stderr": "",
            }
        _send(response)


if __name__ == "__main__":
    main()
