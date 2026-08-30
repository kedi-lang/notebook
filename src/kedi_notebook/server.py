from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import os
import subprocess
import threading
import webbrowser
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .host_environment import HostEnvironmentProvider
from .pyright import PyrightServer
from .runtime import NotebookSessionManager
from .secrets import NotebookSecretStore

PACKAGE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PACKAGE_ROOT / "static"
_MAX_SOURCE_CHARS = 1_000_000
_MAX_BRIDGE_OUTPUT_CHARS = 64_000
_MAX_DOTENV_BYTES = 1_000_000
_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESTORE_REQUEST_BYTES = 5_500_000
_DEFAULT_SESSION_TTL = 30 * 60.0


class CreateSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["browser", "host"] = "browser"
    python_id: str | None = Field(default=None, alias="pythonId")


class ExecuteCellPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cell_id: str = Field(min_length=1, max_length=120, alias="cellId")
    source: str = Field(max_length=_MAX_SOURCE_CHARS)


class BrowserResponsePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=120, alias="requestId")
    response: dict[str, Any]


class BrowserOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=120, alias="requestId")
    stream: Literal["stdout", "stderr"]
    text: str = Field(max_length=_MAX_BRIDGE_OUTPUT_CHARS)


class HoverPayload(BaseModel):
    source: str = Field(max_length=_MAX_SOURCE_CHARS)
    line: int = Field(ge=0)
    character: int = Field(ge=0)


class SourcePayload(BaseModel):
    source: str = Field(max_length=_MAX_SOURCE_CHARS)


class ReferencePayload(HoverPayload):
    include_declaration: bool = Field(default=True, alias="includeDeclaration")


class RenamePayload(HoverPayload):
    new_name: str = Field(min_length=1, max_length=200, alias="newName")


class PackageInstallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    packages: list[str] = Field(min_length=1, max_length=50)


class SecretValuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=128_000)


class DotenvImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=4096)


class RestoreSessionPayload(CreateSessionPayload):
    model_config = ConfigDict(extra="forbid", strict=True)

    snapshot: str = Field(min_length=1, max_length=5_000_000)


def create_app(
    *,
    cwd: Path | None = None,
    explicit_pythons: Sequence[str | Path] = (),
    access_token: str | None = None,
    session_ttl: float = _DEFAULT_SESSION_TTL,
    host_environment: HostEnvironmentProvider | None = None,
    secret_store: NotebookSecretStore | None = None,
) -> FastAPI:
    if session_ttl <= 0:
        raise ValueError("session_ttl must be greater than zero")
    resolved_cwd = (cwd or Path.cwd()).resolve()
    load_dotenv(resolved_cwd / ".env", override=False)
    secrets = secret_store or NotebookSecretStore()
    secrets.apply_to_environment()
    manager = NotebookSessionManager(
        cwd=resolved_cwd,
        explicit_pythons=explicit_pythons,
        host_environment=host_environment,
        interactive_options=_interactive_options_from_environment(),
    )
    pyright = PyrightServer()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def cleanup_sessions() -> None:
            interval = max(1.0, min(60.0, session_ttl / 2))
            while True:
                await asyncio.sleep(interval)
                await asyncio.to_thread(manager.cleanup_stale, max_age=session_ttl)

        cleanup_task = asyncio.create_task(cleanup_sessions())
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            manager.close_all()
            pyright.close()

    app = FastAPI(title="Kedi Notebook", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.notebook_manager = manager
    app.state.notebook_secret_store = secrets

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _: Request,
        __: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": "Invalid request payload"},
            status_code=422,
        )

    @app.middleware("http")
    async def local_browser_headers(request: Request, call_next: Any) -> Response:
        response: Response
        if request.url.path.startswith("/api/"):
            if access_token is None and not _loopback_request_host(request):
                response = JSONResponse(
                    {"ok": False, "error": "Forbidden: notebook API host is not loopback"},
                    status_code=403,
                )
            elif not _same_origin(request):
                response = JSONResponse(
                    {"ok": False, "error": "Forbidden: cross-origin notebook API request"},
                    status_code=403,
                )
            elif access_token is not None and not _valid_access_token(request, access_token):
                response = JSONResponse(
                    {"ok": False, "error": "Unauthorized: notebook access token is required"},
                    status_code=401,
                )
            elif _request_too_large(
                request,
                limit=(
                    _MAX_RESTORE_REQUEST_BYTES
                    if request.url.path == "/api/notebook/sessions/restore"
                    else _MAX_REQUEST_BYTES
                ),
            ):
                response = JSONResponse(
                    {"ok": False, "error": "Request body is too large"},
                    status_code=413,
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        return _secure_browser_response(response, api=request.url.path.startswith("/api/"))

    def _secure_browser_response(response: Response, *, api: bool) -> Response:
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-eval' https://cdn.jsdelivr.net; "
            "worker-src 'self' blob:; "
            "connect-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://cdn.jsdelivr.net; "
            "img-src 'self' data:"
        )
        response.headers["Cache-Control"] = "no-store" if api else "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/notebook/")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/notebook/runtimes")
    def runtimes() -> dict[str, object]:
        return {
            "ok": True,
            "default": "browser",
            "browser": {"id": "browser", "label": "Browser - Pyodide 3.14"},
            "host": [python.to_dict() for python in manager.pythons],
        }

    @app.get("/api/notebook/secrets")
    def list_secrets() -> dict[str, object]:
        return {"ok": True, "configured": list(secrets.names)}

    @app.put("/api/notebook/secrets")
    def set_secret(payload: SecretValuePayload) -> JSONResponse:
        try:
            secrets.set(payload.name, payload.value)
            manager.reconfigure(
                interactive_options=_interactive_options_from_environment(),
            )
            return JSONResponse(
                {
                    "ok": True,
                    "configured": list(secrets.names),
                    "runtimeReset": True,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.delete("/api/notebook/secrets/{name}")
    def delete_secret(name: str) -> JSONResponse:
        try:
            deleted = secrets.delete(name)
            if deleted:
                manager.reconfigure(
                    interactive_options=_interactive_options_from_environment(),
                )
            return JSONResponse(
                {
                    "ok": True,
                    "configured": list(secrets.names),
                    "runtimeReset": deleted,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/secrets/import-dotenv")
    def import_dotenv(payload: DotenvImportPayload) -> JSONResponse:
        try:
            dotenv_path = _resolve_dotenv_path(payload.path, cwd=resolved_cwd)
            parsed = dotenv_values(dotenv_path)
            values = {
                name: value for name, value in parsed.items() if value is not None and value != ""
            }
            imported = secrets.set_many(values)
            manager.reconfigure(
                interactive_options=_interactive_options_from_environment(),
            )
            return JSONResponse(
                {
                    "ok": True,
                    "configured": list(secrets.names),
                    "imported": list(imported),
                    "runtimeReset": True,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/restore")
    def restore_session(payload: RestoreSessionPayload) -> JSONResponse:
        try:
            session = manager.create(
                mode=payload.mode,
                python_id=payload.python_id,
                session_snapshot=payload.snapshot,
            )
            return JSONResponse(
                {
                    "ok": True,
                    "sessionId": session.id,
                    "mode": session.mode,
                    "python": session.python.to_dict() if session.python else None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions")
    def create_session(payload: CreateSessionPayload) -> JSONResponse:
        try:
            session = manager.create(mode=payload.mode, python_id=payload.python_id)
            return JSONResponse(
                {
                    "ok": True,
                    "sessionId": session.id,
                    "mode": session.mode,
                    "python": session.python.to_dict() if session.python else None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/{session_id}/cells/execute")
    async def execute_cell(session_id: str, payload: ExecuteCellPayload) -> JSONResponse:
        try:
            session = manager.get(session_id)
            result = await asyncio.to_thread(
                session.execute,
                cell_id=payload.cell_id,
                source=payload.source,
            )
            if result.get("runtimeReset"):
                manager.close(session_id)
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/{session_id}/snapshot")
    async def snapshot_session(session_id: str) -> JSONResponse:
        try:
            snapshot = await asyncio.to_thread(manager.get(session_id).snapshot)
            return JSONResponse({"ok": True, "snapshot": snapshot})
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/{session_id}/terminal/execute")
    def execute_terminal(
        session_id: str,
        payload: ExecuteCellPayload,
    ) -> Response:
        try:
            session = manager.get(session_id)
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

        def events() -> Iterator[str]:
            runtime_reset = False
            for event in session.stream_terminal(
                cell_id=payload.cell_id,
                source=payload.source,
            ):
                runtime_reset = runtime_reset or event.get("runtimeReset") is True
                yield json.dumps(event, separators=(",", ":")) + "\n"
            if runtime_reset:
                manager.close(session_id)

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/notebook/sessions/{session_id}/bridge/request")
    async def browser_request(session_id: str) -> JSONResponse:
        try:
            request = await asyncio.to_thread(
                manager.get(session_id).next_browser_request,
                timeout=20,
            )
            return JSONResponse({"ok": True, "request": request})
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/{session_id}/bridge/response")
    def browser_response(session_id: str, payload: BrowserResponsePayload) -> JSONResponse:
        try:
            manager.get(session_id).submit_browser_response(
                payload.request_id,
                payload.response,
            )
            return JSONResponse({"ok": True})
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/{session_id}/bridge/output")
    def browser_output(session_id: str, payload: BrowserOutputPayload) -> JSONResponse:
        try:
            manager.get(session_id).submit_browser_output(
                payload.request_id,
                stream=payload.stream,
                text=payload.text,
            )
            return JSONResponse({"ok": True})
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.delete("/api/notebook/sessions/{session_id}")
    def close_session(session_id: str) -> dict[str, object]:
        manager.close(session_id)
        return {"ok": True}

    @app.post("/api/notebook/sessions/{session_id}/interrupt")
    async def interrupt_session(session_id: str) -> dict[str, object]:
        await asyncio.to_thread(manager.close, session_id)
        return {"ok": True, "runtimeReset": True}

    @app.get("/api/notebook/sessions/{session_id}/packages")
    async def list_packages(session_id: str) -> JSONResponse:
        try:
            session = manager.get(session_id)
            packages = await asyncio.to_thread(session.list_packages)
            return JSONResponse(
                {
                    "ok": True,
                    "environment": session.python.environment if session.python else None,
                    "packages": packages,
                }
            )
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

    @app.post("/api/notebook/sessions/{session_id}/packages/install")
    def install_packages(session_id: str, payload: PackageInstallPayload) -> Response:
        try:
            session = manager.get(session_id)
        except Exception as exc:  # noqa: BLE001 - API boundary.
            return _error_response(exc)

        def events() -> Iterator[str]:
            for event in session.stream_package_install(payload.packages):
                yield json.dumps(event, separators=(",", ":")) + "\n"

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/lsp/hover")
    async def hover(payload: HoverPayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_hover
        from lsprotocol import types as lsp

        python_hover = await asyncio.to_thread(
            pyright.hover,
            payload.source,
            payload.line,
            payload.character,
        )
        if python_hover is not None:
            return {"ok": True, "hover": python_hover, "provider": "pyright"}
        result = compute_hover(
            payload.source,
            source_path=None,
            pos=lsp.Position(line=payload.line, character=payload.character),
        )
        if result is None:
            return {"ok": True, "hover": None, "provider": "kedi"}
        contents = cast(lsp.MarkupContent, result.contents)
        range_ = result.range
        return {
            "ok": True,
            "provider": "kedi",
            "hover": {
                "contents": {"kind": contents.kind, "value": contents.value},
                "range": (
                    {
                        "start": {
                            "line": range_.start.line,
                            "character": range_.start.character,
                        },
                        "end": {
                            "line": range_.end.line,
                            "character": range_.end.character,
                        },
                    }
                    if range_ is not None
                    else None
                ),
            },
        }

    @app.post("/api/lsp/diagnostics")
    async def diagnostics(payload: SourcePayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_diagnostics

        values, python_values = await asyncio.gather(
            asyncio.to_thread(compute_diagnostics, payload.source, None),
            asyncio.to_thread(_pyright_diagnostics, pyright, payload.source),
        )
        return {
            "ok": True,
            "diagnostics": [
                {
                    "range": _range_payload(value.range),
                    "severity": int(value.severity or 1),
                    "code": value.code,
                    "source": value.source or "kedi",
                    "message": value.message,
                }
                for value in values
            ]
            + python_values,
        }

    @app.post("/api/lsp/completion")
    async def completion(payload: HoverPayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_completion
        from lsprotocol import types as lsp

        python_items = await asyncio.to_thread(
            pyright.completion,
            payload.source,
            payload.line,
            payload.character,
        )
        if python_items is not None:
            return {"ok": True, "items": python_items, "provider": "pyright"}
        items = await asyncio.to_thread(
            compute_completion,
            payload.source,
            None,
            lsp.Position(line=payload.line, character=payload.character),
        )
        return {
            "ok": True,
            "provider": "kedi",
            "items": [_completion_payload(item) for item in items],
        }

    @app.post("/api/lsp/signature")
    async def signature(payload: HoverPayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_signature_help
        from lsprotocol import types as lsp

        value = await asyncio.to_thread(
            compute_signature_help,
            payload.source,
            None,
            lsp.Position(line=payload.line, character=payload.character),
        )
        if value is None:
            return {"ok": True, "signature": None}
        return {
            "ok": True,
            "signature": {
                "activeSignature": value.active_signature or 0,
                "activeParameter": value.active_parameter or 0,
                "signatures": [
                    {
                        "label": item.label,
                        "documentation": _markup_payload(item.documentation),
                        "parameters": [
                            {
                                "label": parameter.label,
                                "documentation": _markup_payload(parameter.documentation),
                            }
                            for parameter in (item.parameters or [])
                        ],
                    }
                    for item in value.signatures
                ],
            },
        }

    @app.post("/api/lsp/definition")
    async def definition(payload: HoverPayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_definition
        from lsprotocol import types as lsp

        value = await asyncio.to_thread(
            compute_definition,
            payload.source,
            "file:///kedi-notebook.kedi",
            None,
            lsp.Position(line=payload.line, character=payload.character),
        )
        return {
            "ok": True,
            "definition": None if value is None else {"range": _range_payload(value.range)},
        }

    @app.post("/api/lsp/references")
    async def references(payload: ReferencePayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_references
        from lsprotocol import types as lsp

        python_locations = await asyncio.to_thread(
            pyright.references,
            payload.source,
            payload.line,
            payload.character,
            include_declaration=payload.include_declaration,
        )
        if python_locations is not None:
            return {"ok": True, "references": python_locations, "provider": "pyright"}
        locations = await asyncio.to_thread(
            compute_references,
            payload.source,
            "file:///kedi-notebook.kedi",
            None,
            lsp.Position(line=payload.line, character=payload.character),
            payload.include_declaration,
        )
        return {
            "ok": True,
            "provider": "kedi",
            "references": [{"range": _range_payload(item.range)} for item in locations],
        }

    @app.post("/api/lsp/prepare-rename")
    async def prepare_rename(payload: HoverPayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_prepare_rename
        from lsprotocol import types as lsp

        python_result = await asyncio.to_thread(
            pyright.prepare_rename,
            payload.source,
            payload.line,
            payload.character,
        )
        if python_result is not None:
            return {"ok": True, "rename": python_result, "provider": "pyright"}
        range_ = await asyncio.to_thread(
            compute_prepare_rename,
            payload.source,
            None,
            lsp.Position(line=payload.line, character=payload.character),
        )
        return {
            "ok": True,
            "provider": "kedi",
            "rename": None if range_ is None else {"range": _range_payload(range_)},
        }

    @app.post("/api/lsp/rename")
    async def rename(payload: RenamePayload) -> dict[str, Any]:
        from kedi.lsp.features import compute_rename
        from lsprotocol import types as lsp

        python_edits = await asyncio.to_thread(
            pyright.rename,
            payload.source,
            payload.line,
            payload.character,
            payload.new_name,
        )
        if python_edits is not None:
            return {"ok": True, "edits": python_edits, "provider": "pyright"}
        workspace_edit = await asyncio.to_thread(
            compute_rename,
            payload.source,
            "file:///kedi-notebook.kedi",
            None,
            lsp.Position(line=payload.line, character=payload.character),
            payload.new_name,
        )
        changes = workspace_edit.changes if workspace_edit is not None else None
        edits = changes.get("file:///kedi-notebook.kedi", []) if changes else []
        return {
            "ok": True,
            "provider": "kedi",
            "edits": [
                {"range": _range_payload(item.range), "newText": item.new_text} for item in edits
            ],
        }

    app.mount("/", StaticFiles(directory=STATIC_ROOT, html=True), name="static")
    return app


def serve_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Kedi notebook.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8788")))
    parser.add_argument(
        "--python",
        dest="pythons",
        action="append",
        default=[],
        metavar="PATH",
        help="Add a host Python interpreter (repeatable)",
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--token",
        default=os.environ.get("KEDI_NOTEBOOK_TOKEN"),
        help="Require this bearer token for notebook API access",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not _is_loopback_host(args.host) and not args.token:
        parser.error("non-loopback --host requires --token or KEDI_NOTEBOOK_TOKEN")

    app = create_app(
        cwd=args.cwd,
        explicit_pythons=args.pythons,
        access_token=args.token,
    )
    url = f"http://{args.host}:{args.port}/notebook/"
    browser_url = f"{url}#token={args.token}" if args.token else url
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(browser_url)).start()

    import uvicorn

    print(f"Kedi notebook: {browser_url}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main() -> None:
    raise SystemExit(serve_cli())


def _interactive_options_from_environment() -> dict[str, str]:
    adapter = os.environ.get("KEDI_ADAPTER")
    agent = os.environ.get("KEDI_AGENT")
    if adapter and agent:
        raise ValueError("KEDI_ADAPTER and KEDI_AGENT are mutually exclusive")

    options: dict[str, str] = {}
    model = os.environ.get("KEDI_ADAPTER_MODEL")
    if model:
        options["model"] = model
    if adapter:
        options["adapter"] = adapter
    if agent:
        options["agent"] = agent
    return options


def _resolve_dotenv_path(raw_path: str, *, cwd: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The selected .env file does not exist") from exc
    if not resolved.is_file():
        raise ValueError("The selected .env path is not a file")
    if resolved.stat().st_size > _MAX_DOTENV_BYTES:
        raise ValueError("The selected .env file is larger than 1 MB")
    return resolved


def _error_response(exc: BaseException) -> JSONResponse:
    if isinstance(exc, KeyError):
        status = 404
        message = str(exc.args[0]) if exc.args else "Resource not found"
    elif isinstance(exc, (TypeError, ValueError)):
        status = 400
        message = str(exc)
    else:
        status = 500
        message = str(exc) or type(exc).__name__
    return JSONResponse(
        {"ok": False, "error": f"{type(exc).__name__}: {message}"},
        status_code=status,
    )


def _valid_access_token(request: Request, expected: str) -> bool:
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied, expected)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme == request.url.scheme and parsed.netloc == request.headers.get("host")


def _loopback_request_host(request: Request) -> bool:
    parsed = urlsplit(f"//{request.headers.get('host', '')}")
    return parsed.hostname is not None and _is_loopback_host(parsed.hostname)


def _request_too_large(request: Request, *, limit: int) -> bool:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return False
    try:
        return int(content_length) > limit
    except ValueError:
        return True


def _range_payload(range_: Any) -> dict[str, dict[str, int]]:
    return {
        "start": {"line": range_.start.line, "character": range_.start.character},
        "end": {"line": range_.end.line, "character": range_.end.character},
    }


def _markup_payload(value: Any) -> str | dict[str, str] | None:
    if value is None or isinstance(value, str):
        return value
    if hasattr(value, "kind") and hasattr(value, "value"):
        return {"kind": str(value.kind), "value": str(value.value)}
    return str(value)


def _completion_payload(value: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": value.label,
        "kind": int(value.kind) if value.kind is not None else None,
        "detail": value.detail,
        "insertText": value.insert_text,
        "sortText": value.sort_text,
    }
    return {key: item for key, item in payload.items() if item is not None}


def _pyright_diagnostics(pyright: PyrightServer, source: str) -> list[dict[str, Any]]:
    try:
        return pyright.diagnostics(source)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    main()
