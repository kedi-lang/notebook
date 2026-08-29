from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import webbrowser
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .pyright import PyrightServer
from .runtime import NotebookSessionManager

PACKAGE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PACKAGE_ROOT / "static"


class CreateSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["browser", "host"] = "browser"
    python_id: str | None = Field(default=None, alias="pythonId")


class ExecuteCellPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cell_id: str = Field(min_length=1, max_length=120, alias="cellId")
    source: str


class BrowserResponsePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(alias="requestId")
    response: dict[str, Any]


class BrowserOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(alias="requestId")
    stream: Literal["stdout", "stderr"]
    text: str


class HoverPayload(BaseModel):
    source: str
    line: int = Field(ge=0)
    character: int = Field(ge=0)


def create_app(
    *,
    cwd: Path | None = None,
    explicit_pythons: Sequence[str | Path] = (),
) -> FastAPI:
    manager = NotebookSessionManager(cwd=cwd, explicit_pythons=explicit_pythons)
    pyright = PyrightServer()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            manager.close_all()
            pyright.close()

    app = FastAPI(title="Kedi Notebook", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.notebook_manager = manager

    @app.middleware("http")
    async def local_browser_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api/") else "no-cache"
        )
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
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)
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
            for event in session.stream_terminal(
                cell_id=payload.cell_id,
                source=payload.source,
            ):
                yield json.dumps(event, separators=(",", ":")) + "\n"

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
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser")
    args = parser.parse_args(list(argv) if argv is not None else None)

    app = create_app(cwd=args.cwd, explicit_pythons=args.pythons)
    url = f"http://{args.host}:{args.port}/notebook/"
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    import uvicorn

    print(f"Kedi notebook: {url}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main() -> None:
    raise SystemExit(serve_cli())


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


if __name__ == "__main__":
    main()
