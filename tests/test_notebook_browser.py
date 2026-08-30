from __future__ import annotations

import base64
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

from kedi_notebook.host_environment import PreparedHostEnvironment  # noqa: E402
from kedi_notebook.secrets import NotebookSecretStore  # noqa: E402
from kedi_notebook.server import create_app  # noqa: E402


class _PassthroughHostEnvironment:
    def prepare(
        self,
        *,
        executable: str,
        version: str,
        cwd: Path,
    ) -> PreparedHostEnvironment:
        del version, cwd
        python = Path(executable)
        return PreparedHostEnvironment(executable, str(python.parent.parent))


@pytest.fixture
def notebook_url(tmp_path: Path) -> Iterator[str]:
    app = create_app(
        cwd=tmp_path,
        explicit_pythons=[sys.executable],
        host_environment=_PassthroughHostEnvironment(),
        secret_store=NotebookSecretStore(tmp_path / "secrets.json"),
    )
    config = uvicorn.Config(app, log_level="error")
    server = uvicorn.Server(config)
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{port}/notebook/"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


def test_notebook_cell_lifecycle_streaming_and_interrupt(notebook_url: str) -> None:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    executable = os.environ.get("KEDI_NOTEBOOK_BROWSER")
    if executable is None and chrome.is_file():
        executable = str(chrome)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(
            headless=True,
            **({"executable_path": executable} if executable else {}),
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page_errors: list[str] = []
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(notebook_url, wait_until="networkidle", timeout=120_000)

        playwright.expect(page.locator("#manage-secrets")).to_have_text("Secret Manager")
        page.locator("#manage-secrets").click()
        playwright.expect(page.locator("#secret-dialog")).to_be_visible()
        page.locator("#secret-name").fill("KEDI_TEST_VALUE")
        page.locator("#secret-value").fill("browser-secret-value")
        page.locator("#save-secret").click()
        playwright.expect(page.locator("#secret-list")).to_contain_text("KEDI_TEST_VALUE")
        assert page.locator("#secret-value").input_value() == ""
        assert "browser-secret-value" not in page.locator("body").inner_text()
        page.locator("#close-secrets").click()

        host = page.locator('#runtime-select option[data-mode="host"]').first
        page.select_option("#runtime-select", host.get_attribute("value"))
        playwright.expect(page.locator("#manage-packages")).to_be_visible()
        page.locator("#manage-packages").click()
        playwright.expect(page.locator("#package-dialog")).to_be_visible(timeout=30_000)
        playwright.expect(page.locator("#package-list")).to_contain_text("kedi", timeout=30_000)
        assert "Kedi Notebook environment" not in page.locator("#package-environment").inner_text()
        page.locator("#close-packages").click()
        first = page.locator(".cell").first
        assert first.locator(".cell-index").inner_text() == "[1]"

        page.locator("#add-cell").click()
        dependent = page.locator(".cell").nth(1)
        page.evaluate(
            "source => globalThis.monaco.editor.getModels().at(-1).setValue(source)",
            "= `len(values)`",
        )
        dependent.locator('button[aria-label="Run cell"]').click()
        playwright.expect(dependent.locator(".error-content")).to_be_visible(timeout=30_000)
        assert page.evaluate(
            """() => {
              const model = globalThis.monaco.editor.getModels().find(
                (candidate) => candidate.getValue() === "= `len(values)`",
              );
              return globalThis.monaco.editor.getModelMarkers({
                owner: "kedi-runtime",
                resource: model.uri,
              }).length;
            }"""
        ) == 1

        first.locator('button[aria-label="Run cell"]').click()
        playwright.expect(first.locator(".result-value")).to_have_text("38", timeout=30_000)
        dependent.locator('button[aria-label="Run cell"]').click()
        playwright.expect(dependent.locator(".result-value")).to_have_text("3", timeout=30_000)
        assert page.evaluate(
            """() => {
              const model = globalThis.monaco.editor.getModels().find(
                (candidate) => candidate.getValue() === "= `len(values)`",
              );
              return globalThis.monaco.editor.getModelMarkers({
                owner: "kedi-runtime",
                resource: model.uri,
              }).length;
            }"""
        ) == 0
        page.once("dialog", lambda dialog: dialog.accept())
        dependent.locator('button[aria-label="Delete cell"]').click()
        playwright.expect(page.locator(".cell")).to_have_count(1)

        assert first.locator(".editor-host").is_visible()

        page.locator("#save-notebook").click()
        playwright.expect(page.locator("#save-dialog")).to_be_visible()
        with page.expect_download() as progress_download:
            page.locator("#save-progress").click()
        progress_path = Path(progress_download.value.path())
        progress_document = json.loads(progress_path.read_text(encoding="utf-8"))
        assert progress_document["version"] == 2
        assert progress_document["saveMode"] == "progress"
        assert progress_document["cells"][0]["progress"]["result"] is not None
        session_document = json.loads(
            base64.b64decode(progress_document["sessionSnapshot"], validate=True)
        )
        assert session_document["format"] == "kedi.interactive-session"
        assert "browser-secret-value" not in progress_path.read_text(encoding="utf-8")

        page.locator("#save-notebook").click()
        with page.expect_download() as notebook_download:
            page.locator("#save-just-notebook").click()
        notebook_document = json.loads(
            Path(notebook_download.value.path()).read_text(encoding="utf-8")
        )
        assert notebook_document["saveMode"] == "notebook"
        assert "sessionSnapshot" not in notebook_document
        assert "progress" not in notebook_document["cells"][0]

        page.locator("#notebook-file").set_input_files(str(progress_path))
        playwright.expect(page.locator("#save-state")).to_have_text("Opened")
        playwright.expect(page.locator(".cell").first.locator(".result-value")).to_have_text(
            "38",
            timeout=30_000,
        )
        page.evaluate('globalThis.monaco.editor.getModels()[0].setValue("= `sum(values)`")')
        assert page.evaluate("globalThis.monaco.editor.getModels()[0].getValue()") == "= `sum(values)`"
        restored_run = first.locator('button[aria-label="Run cell"]')
        playwright.expect(restored_run).to_be_enabled()
        restored_run.click()
        playwright.expect(first.locator(".result-value")).to_have_text("10", timeout=30_000)
        page.evaluate(
            "source => globalThis.monaco.editor.getModels()[0].setValue(source)",
            "[values: list[int]] = `[2, 3, 5]`\n= `sum(value * value for value in values)`",
        )

        first.locator('button[aria-label="Run cell"]').click()
        playwright.expect(first.locator(".result-value")).to_have_text("38", timeout=30_000)
        assert first.locator(".cell-index").inner_text() == "[1]"
        assert page.locator(".cell").count() == 1

        page.select_option("#cell-kind", "terminal")
        page.locator("#add-cell").click()
        terminal = page.locator(".cell.active")
        terminal_id = terminal.get_attribute("data-cell-id")
        terminal = page.locator(f'[data-cell-id="{terminal_id}"]')
        assert first.locator(".editor-host").is_visible()
        assert terminal.locator(".cell-kind-select").input_value() == "terminal"
        terminal.locator(".cell-kind-select").select_option("markdown")
        playwright.expect(terminal.locator(".markdown-editor")).to_be_visible()
        terminal.locator(".cell-kind-select").select_option("terminal")
        page.evaluate('globalThis.monaco.editor.getModels()[0].setValue("= `41`")')
        first.locator('button[aria-label="Run cell"]').click()
        playwright.expect(first.locator(".result-value")).to_have_text("41", timeout=30_000)
        terminal.locator("textarea").fill("!python -u -c \"print('stream-ok')\"")
        terminal.locator('button[aria-label="Run command"]').click()
        playwright.expect(terminal.locator(".output-content")).to_contain_text(
            "stream-ok",
            timeout=30_000,
        )
        assert terminal.locator("textarea").is_visible()

        terminal.locator('button[aria-label="Move cell up"]').click()
        assert page.locator(".cell").first.locator(".cell-kind-select").input_value() == "terminal"
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(".cell").first.locator('button[aria-label="Delete cell"]').click()
        playwright.expect(page.locator(".cell")).to_have_count(1)

        first = page.locator(".cell").first
        first.locator('button[aria-label="Hide cell"]').click()
        playwright.expect(first.locator(".cell-hidden-label")).to_have_text("Hidden")
        playwright.expect(first.locator(".editor-host")).to_have_count(0)
        first.locator('button[aria-label="Show cell"]').click()
        playwright.expect(first.locator(".editor-host")).to_be_visible()

        page.evaluate(
            "globalThis.monaco.editor.getModels()[0].setValue("
            '"= `(__import__(\\"time\\").sleep(30), 1)[1]`"'
            ")"
        )
        first = page.locator(".cell").first
        first.locator('button[aria-label="Run cell"]').click()
        playwright.expect(page.locator("#interrupt-session")).to_be_visible(timeout=10_000)
        playwright.expect(first.locator(".cell-run-button.running")).to_have_attribute(
            "aria-label",
            "Running cell",
        )
        page.locator("#interrupt-session").click()
        playwright.expect(first.locator(".error-content")).to_contain_text(
            "interrupted",
            timeout=10_000,
        )
        playwright.expect(first.locator(".cell-run-button")).to_have_attribute(
            "aria-label",
            "Run cell",
        )

        page.evaluate('globalThis.monaco.editor.getModels()[0].setValue("= `7`")')
        first.locator('button[aria-label="Run cell"]').click()
        playwright.expect(first.locator(".result-value")).to_have_text("7", timeout=30_000)
        assert not page_errors
        assert not [
            error
            for error in errors
            if "Content Security Policy" in error or "Model is disposed" in error
        ]
        browser.close()


def test_browser_runtime_uses_vendored_pyodide(notebook_url: str) -> None:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    executable = os.environ.get("KEDI_NOTEBOOK_BROWSER")
    if executable is None and chrome.is_file():
        executable = str(chrome)

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(
            headless=True,
            **({"executable_path": executable} if executable else {}),
        )
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        external_pyodide_requests: list[str] = []
        page.on(
            "request",
            lambda request: (
                external_pyodide_requests.append(request.url)
                if "cdn.jsdelivr.net/pyodide" in request.url
                else None
            ),
        )
        page.goto(notebook_url, wait_until="networkidle", timeout=120_000)
        cell = page.locator(".cell").first
        run_button = cell.locator('button[aria-label="Run cell"]')
        run_button.click()
        playwright.expect(cell.locator(".result-value")).to_have_text("38", timeout=60_000)

        page.evaluate(
            "source => globalThis.monaco.editor.getModels()[0].setValue(source)",
            "```\nnumbers = [2, 3]\n```",
        )
        run_button.click()
        playwright.expect(run_button).to_be_enabled(timeout=60_000)
        page.evaluate(
            "source => globalThis.monaco.editor.getModels()[0].setValue(source)",
            "```\nnumbers.append(5)\n```\n= `sum(numbers)`",
        )
        run_button.click()
        playwright.expect(cell.locator(".result-value")).to_have_text("10", timeout=60_000)

        assert external_pyodide_requests == []
        browser.close()
