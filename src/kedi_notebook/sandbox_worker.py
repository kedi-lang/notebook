from __future__ import annotations

import builtins
import contextlib
import io
import json
import sys
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
_RESPONSE_PREFIX = "__KEDI_NOTEBOOK_RESPONSE__"
_OBJECT_REGISTRY: dict[str, Any] = {}


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


def _to_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if callable(value):
        raise TypeError(f"Python callable {value!r} requires an opaque bridge reference")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Sandbox mappings require string keys")
        return {key: _to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_json(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, (type, types.ModuleType)):
        return {key: _to_json(item) for key, item in vars(value).items() if not key.startswith("_")}
    raise TypeError(f"Python value {type(value).__name__} cannot cross the sandbox bridge")


def _type_descriptor(value: Any) -> dict[str, Any]:
    if value is type(None):
        return {"kind": "none"}
    if isinstance(value, type) and getattr(builtins, value.__name__, None) is value:
        return {"kind": "builtin", "name": value.__qualname__}
    if value is typing.Any:
        return {"kind": "typing", "name": "Any"}
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


def _run_block(namespace: dict[str, Any], code: str, sync_names: list[str]) -> Any:
    header = ["def __kedi_wrapper__():"]
    if sync_names:
        header.append(f"    global {', '.join(sync_names)}")
    body = [f"    {line}" for line in code.splitlines()]
    source = "\n".join(header + body + ["    pass"])
    exec(compile(source, "<kedi-sandbox:block>", "exec"), namespace, namespace)
    wrapper = namespace.pop("__kedi_wrapper__")
    return wrapper()


def _serializable_namespace(namespace: Mapping[str, Any], names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        if name.startswith("_") or name not in namespace:
            continue
        value = namespace[name]
        try:
            result[name] = _to_json(value)
        except TypeError:
            handle = str(id(value))
            _OBJECT_REGISTRY[handle] = value
            result[name] = {_REFERENCE_KEY: handle}
    return result


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
    _USER_NAMESPACE.update(
        {name: _from_json(value) for name, value in request.get("env", {}).items()}
    )
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
        "env": _serializable_namespace(_USER_NAMESPACE, sync_names),
    }


def handle_request(request: Mapping[str, Any]) -> dict[str, Any]:
    stdout = _KediOutput()
    stderr = _KediOutput()
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
    response["stdout"] = stdout.getvalue()
    response["stderr"] = stderr.getvalue()
    return response


def main() -> None:
    for line in sys.stdin:
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
        sys.stdout.write(_RESPONSE_PREFIX + json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
