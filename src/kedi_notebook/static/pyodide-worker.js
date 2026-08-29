import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.2/full/pyodide.mjs";

const pyodidePromise = initializePyodide();
void pyodidePromise.then(
  () => self.postMessage({ type: "ready" }),
  (error) =>
    self.postMessage({
      type: "ready_error",
      error: error?.message || String(error),
    }),
);
const STDIN_HEADER_BYTES = 16;
const STDIN_CAPACITY = 1024 * 1024;
const loadedOptionalPackages = new Set();
let activeRequestId = null;

const TERMINAL_DRIVER = String.raw`
import importlib.metadata
import json
import shlex
import traceback

import micropip


def _browser_terminal_request(command):
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("Terminal command cannot be empty")

    prefix = tokens[:]
    if len(prefix) >= 3 and prefix[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
        prefix = ["pip", *prefix[3:]]
    if len(prefix) >= 2 and prefix[:2] == ["uv", "pip"]:
        prefix = ["pip", *prefix[2:]]

    if prefix[:2] in (["pip", "install"], ["uv", "add"]):
        packages = []
        for token in prefix[2:]:
            if token in {"-q", "--quiet", "-U", "--upgrade"}:
                continue
            if token.startswith("-"):
                raise ValueError(
                    f"Browser terminal does not support option {token!r}"
                )
            packages.append(token)
        if not packages:
            raise ValueError("Package installation requires at least one package")
        return {"kind": "install", "packages": packages, "uv": prefix[0] == "uv"}

    if prefix == ["pip", "list"]:
        return {"kind": "list"}
    if prefix[0] == "echo":
        return {"kind": "echo", "value": " ".join(prefix[1:])}
    if prefix == ["pwd"]:
        return {"kind": "pwd"}
    raise ValueError(
        "Browser terminal supports !pip install, !uv add, !pip list, !echo, and !pwd"
    )


try:
    __kedi_terminal = _browser_terminal_request(
        json.loads(__kedi_terminal_command_json)
    )
    if __kedi_terminal["kind"] == "install":
        kediStreamOutput(
            "stdout",
            "Installing " + ", ".join(__kedi_terminal["packages"]) + "...\n",
        )
        await micropip.install(__kedi_terminal["packages"])
        __kedi_note = (
            " Project files are unchanged."
            if __kedi_terminal["uv"]
            else ""
        )
        __kedi_output = (
            "Installed "
            + ", ".join(__kedi_terminal["packages"])
            + " in the browser runtime."
            + __kedi_note
            + "\n"
        )
        kediStreamOutput("stdout", __kedi_output)
        __kedi_terminal_response = {
            "ok": True,
            "stdout": __kedi_output,
            "stderr": "",
        }
    elif __kedi_terminal["kind"] == "list":
        __kedi_rows = sorted(
            (dist.metadata["Name"] or "", dist.version)
            for dist in importlib.metadata.distributions()
        )
        __kedi_output = "\n".join(f"{name} {version}" for name, version in __kedi_rows) + "\n"
        kediStreamOutput("stdout", __kedi_output)
        __kedi_terminal_response = {
            "ok": True,
            "stdout": __kedi_output,
            "stderr": "",
        }
    elif __kedi_terminal["kind"] == "echo":
        __kedi_output = __kedi_terminal["value"] + "\n"
        kediStreamOutput("stdout", __kedi_output)
        __kedi_terminal_response = {
            "ok": True,
            "stdout": __kedi_output,
            "stderr": "",
        }
    else:
        __kedi_output = "/home/pyodide\n"
        kediStreamOutput("stdout", __kedi_output)
        __kedi_terminal_response = {
            "ok": True,
            "stdout": __kedi_output,
            "stderr": "",
        }
except Exception as exc:
    __kedi_terminal_response = {
        "ok": False,
        "errorType": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback.format_exc(),
        "stdout": "",
        "stderr": "",
    }

json.dumps(__kedi_terminal_response)
`;

async function initializePyodide() {
  self.postMessage({ type: "status", message: "Loading Python runtime" });
  const pyodide = await loadPyodide();
  self.postMessage({ type: "status", message: "Loading Pydantic" });
  await pyodide.loadPackage(["micropip", "pydantic"]);
  pyodide.runPython(DRIVER_SETUP);
  return pyodide;
}

async function ensureOptionalPackages(pyodide, request) {
  const serialized = JSON.stringify(request);
  if (serialized.includes("pydantic_monty")) {
    throw new Error(
      "pydantic_monty is not supported by the Pyodide runtime; use server sandbox execution",
    );
  }
  if (serialized.includes("logfire")) {
    await installLogfire(pyodide);
  }
}

async function installLogfire(pyodide) {
  if (loadedOptionalPackages.has("logfire")) {
    return;
  }
  self.postMessage({ type: "status", message: "Loading Logfire" });
  await pyodide.loadPackage("pygments");
  await pyodide.runPythonAsync(`
import micropip
await micropip.install([
    "protobuf==6.33.5",
    "opentelemetry-api==1.41.1",
    "opentelemetry-sdk==1.41.1",
    "opentelemetry-exporter-otlp-proto-http==1.41.1",
    "opentelemetry-instrumentation==0.62b1",
    "opentelemetry-semantic-conventions==0.62b1",
    "logfire==4.33.0",
])
import logfire
`);
  loadedOptionalPackages.add("logfire");
}

globalThis.kediStreamOutput = (stream, text) => {
  self.postMessage({
    type: "stream",
    id: activeRequestId,
    stream: String(stream),
    text: String(text),
  });
};

function readStdin() {
  if (typeof SharedArrayBuffer === "undefined" || !self.crossOriginIsolated) {
    throw new Error(
      "Interactive stdin requires a cross-origin isolated notebook response",
    );
  }

  const buffer = new SharedArrayBuffer(STDIN_HEADER_BYTES + STDIN_CAPACITY);
  const control = new Int32Array(
    buffer,
    0,
    STDIN_HEADER_BYTES / Int32Array.BYTES_PER_ELEMENT,
  );
  self.postMessage({ type: "stdin", id: activeRequestId, buffer });
  Atomics.wait(control, 0, 0);

  const state = Atomics.load(control, 0);
  if (state === 2) {
    return undefined;
  }
  if (state === 3) {
    throw new Error("Standard input was cancelled");
  }
  if (state !== 1) {
    throw new Error(`Invalid standard input state: ${state}`);
  }

  const length = Atomics.load(control, 1);
  if (length < 0 || length > STDIN_CAPACITY) {
    throw new Error("Invalid standard input length");
  }
  return new Uint8Array(buffer, STDIN_HEADER_BYTES, length);
}

const DRIVER_SETUP = String.raw`
import builtins
import contextlib
import io
import json
import traceback
import types
import typing
import typing_extensions

from js import kediStreamOutput
from pydantic import BaseModel

_REFERENCE_KEY = "__kedi_notebook_ref__"
_OBJECT_REGISTRY = globals().setdefault("__kedi_object_registry", {})


class _KediAttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _from_json(value):
    if isinstance(value, dict):
        if set(value) == {_REFERENCE_KEY}:
            handle = value[_REFERENCE_KEY]
            try:
                return _OBJECT_REGISTRY[handle]
            except KeyError as exc:
                raise RuntimeError("Playground object reference is no longer available") from exc
        return _KediAttrDict({key: _from_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_from_json(item) for item in value]
    return value


class _KediOutput(io.StringIO):
    def __init__(self, stream):
        super().__init__()
        self._stream = stream

    def write(self, value):
        written = super().write(value)
        if value:
            kediStreamOutput(self._stream, value)
        return written


def _to_json(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if callable(value):
        raise TypeError(f"Python callable {value!r} requires an opaque bridge reference")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Playground mappings require string keys")
        return {key: _to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_json(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, (type, types.ModuleType)):
        return {
            key: _to_json(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise TypeError(f"Python value {type(value).__name__} cannot cross the notebook bridge")


def _type_descriptor(value):
    if value is type(None):
        return {"kind": "none"}
    if isinstance(value, type) and getattr(builtins, value.__name__, None) is value:
        return {"kind": "builtin", "name": value.__qualname__}
    if value is typing.Any:
        return {"kind": "typing", "name": "Any"}
    if isinstance(value, type) and issubclass(value, BaseModel):
        fields = []
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
                item["default"] = _to_json(
                    field.get_default(call_default_factory=True)
                )
            fields.append(item)
        return {
            "kind": "pydantic_model",
            "name": value.__name__,
            "fields": fields,
        }

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
    raise TypeError("Python-defined types cannot cross the notebook bridge yet")


def _run_block(namespace, code, sync_names):
    header = ["def __kedi_wrapper__():"]
    if sync_names:
        header.append(f"    global {', '.join(sync_names)}")
    body = [f"    {line}" for line in code.splitlines()]
    source = "\n".join(header + body + ["    pass"])
    exec(compile(source, "<kedi-pyodide:block>", "exec"), namespace, namespace)
    wrapper = namespace.pop("__kedi_wrapper__")
    return wrapper()


def _serializable_namespace(namespace, names):
    result = {}
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


def _execute_kedi_request(request):
    namespace = globals().setdefault(
        "__kedi_user_namespace",
        {
            "__name__": "__kedi_pyodide__",
            "__builtins__": builtins.__dict__,
            "builtins": builtins,
            "typing": typing,
            "typing_extensions": typing_extensions,
            **{
                name: getattr(module, name)
                for module in (typing, typing_extensions)
                for name in module.__all__
                if not name.startswith("_") and hasattr(module, name)
            },
        },
    )
    namespace.update(
        {name: _from_json(value) for name, value in request.get("env", {}).items()}
    )
    action = request["action"]
    code = request["code"]
    sync_names = request.get("syncNames", [])

    if action == "evaluate_inline":
        try:
            result = eval(compile(code, "<kedi-pyodide:inline>", "eval"), namespace, namespace)
        except SyntaxError:
            _run_block(namespace, code, sync_names)
            result = ""
    elif action == "execute_block":
        result = _run_block(namespace, code, sync_names)
    elif action == "execute_side_effects":
        exec(compile(code, "<kedi-pyodide:side-effects>", "exec"), namespace, namespace)
        result = None
    elif action == "execute_prelude":
        exec(compile(code, "<kedi-pyodide:prelude>", "exec"), namespace, namespace)
        result = None
    elif action == "evaluate_type_expression":
        value = eval(compile(code, "<kedi-pyodide:type>", "eval"), namespace, namespace)
        try:
            return {"ok": True, "result": _to_json(value), "env": {}}
        except TypeError:
            return {"ok": True, "type": _type_descriptor(value), "env": {}}
    else:
        raise ValueError(f"Unknown notebook action: {action!r}")

    return {
        "ok": True,
        "result": _to_json(result),
        "env": _serializable_namespace(namespace, sync_names),
    }


def _execute_kedi_json(request_json):
    stdout = _KediOutput("stdout")
    stderr = _KediOutput("stderr")
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            response = _execute_kedi_request(json.loads(request_json))
        except Exception as exc:
            response = {
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }

    response["stdout"] = stdout.getvalue()
    response["stderr"] = stderr.getvalue()
    return json.dumps(response)
`;

self.addEventListener("message", async (event) => {
  const { id, request } = event.data;
  activeRequestId = id;
  try {
    const pyodide = await pyodidePromise;
    if (request.action === "execute_terminal") {
      pyodide.globals.set(
        "__kedi_terminal_command_json",
        JSON.stringify(request.command),
      );
      const responseJson = await pyodide.runPythonAsync(TERMINAL_DRIVER);
      self.postMessage({ id, response: JSON.parse(responseJson) });
      return;
    }
    await ensureOptionalPackages(pyodide, request);
    pyodide.setStdin({ stdin: readStdin, isatty: true });
    pyodide.globals.set("__kedi_request_json", JSON.stringify(request));
    const responseJson = pyodide.runPython(
      "_execute_kedi_json(__kedi_request_json)",
    );
    self.postMessage({ id, response: JSON.parse(responseJson) });
  } catch (error) {
    self.postMessage({
      id,
      response: {
        ok: false,
        errorType: error?.name || "PyodideError",
        error: error?.message || String(error),
      },
    });
  } finally {
    activeRequestId = null;
  }
});
