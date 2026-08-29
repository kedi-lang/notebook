import { PyodideRuntime } from "/pyodide-runtime.js";
import {
  createKediEditor,
  setKediEditorTheme,
  setKediExecutionDiagnostic,
} from "/kedi-editor.js";

const STORAGE_KEY = "kedi.notebook.draft.v1";
const requestedTheme = new URLSearchParams(globalThis.location.search).get("theme");
const state = {
  title: "Untitled notebook",
  cells: [],
  activeCellId: null,
  sessionId: null,
  runtime: "browser",
  runningCellId: null,
  theme:
    requestedTheme === "light" || requestedTheme === "dark"
      ? requestedTheme
      : localStorage.getItem("kedi.notebook.theme") || "dark",
};

const ui = {
  cells: document.querySelector("#cells"),
  title: document.querySelector("#notebook-title"),
  saveState: document.querySelector("#save-state"),
  addCell: document.querySelector("#add-cell"),
  appendCell: document.querySelector("#append-cell"),
  cellKind: document.querySelector("#cell-kind"),
  runtime: document.querySelector("#runtime-select"),
  runtimeStatus: document.querySelector("#runtime-status"),
  resetSession: document.querySelector("#reset-session"),
  newNotebook: document.querySelector("#new-notebook"),
  openNotebook: document.querySelector("#open-notebook"),
  saveNotebook: document.querySelector("#save-notebook"),
  notebookFile: document.querySelector("#notebook-file"),
  themeToggle: document.querySelector("#theme-toggle"),
  toast: document.querySelector("#toast"),
};

let activeEditor = null;
let activeEditorCellId = null;
let activeEditorResizeDisposable = null;
let activeTextEditor = null;
let browserBridge = null;
let browserRuntime = null;
let browserWarmup = null;
let toastTimer = null;

restoreDraft();
if (!state.cells.length) {
  addCell(
    "kedi",
    "[values: list[int]] = `[2, 3, 5]`\n= `sum(value * value for value in values)`",
  );
}
applyTheme();
bindEvents();
void prewarmBrowserRuntime().catch(() => {});
await loadRuntimes();
await render();
globalThis.lucide?.createIcons();

function bindEvents() {
  ui.addCell.addEventListener("click", () => addCellFromControl());
  ui.appendCell.addEventListener("click", () => addCellFromControl());
  ui.title.addEventListener("input", () => {
    state.title = ui.title.value;
    markChanged();
  });
  ui.runtime.addEventListener("change", async () => {
    if (state.sessionId) {
      ui.runtime.value = state.runtime;
      showToast("Start a new session before changing runtime");
      return;
    }
    state.runtime = ui.runtime.value;
    markChanged();
  });
  ui.resetSession.addEventListener("click", () => void resetRuntimeSession());
  ui.newNotebook.addEventListener("click", () => void newNotebook());
  ui.saveNotebook.addEventListener("click", saveNotebook);
  ui.openNotebook.addEventListener("click", () => ui.notebookFile.click());
  ui.notebookFile.addEventListener("change", () => void openNotebookFile());
  ui.themeToggle.addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    localStorage.setItem("kedi.notebook.theme", state.theme);
    applyTheme();
  });
  window.addEventListener("beforeunload", () => {
    persistDraft();
    if (state.sessionId) {
      void fetch(`/api/notebook/sessions/${state.sessionId}`, {
        method: "DELETE",
        keepalive: true,
      });
    }
    browserBridge?.stop();
    browserRuntime?.dispose();
  });
}

function addCellFromControl() {
  const kind = ["kedi", "terminal", "markdown"].includes(ui.cellKind.value)
    ? ui.cellKind.value
    : "kedi";
  addCell(kind, kind === "terminal" ? "!" : "");
  void render().then(focusActiveEditor);
}

function addCell(kind, source) {
  const cell = {
    id: crypto.randomUUID(),
    kind,
    source,
    status: "draft",
    executionCount: null,
    stdout: "",
    result: null,
    error: null,
    diagnostic: null,
  };
  state.cells.push(cell);
  state.activeCellId = cell.id;
  markChanged();
  return cell;
}

async function render() {
  disposeEditor();
  ui.title.value = state.title;
  ui.cells.replaceChildren();
  for (const cell of state.cells) {
    ui.cells.append(await renderCell(cell));
  }
  ui.runtime.value = state.runtime;
  ui.runtime.disabled = Boolean(state.sessionId);
  globalThis.lucide?.createIcons();
}

async function renderCell(cell) {
  const active = cell.id === state.activeCellId;
  const article = document.createElement("article");
  article.className = `cell ${cell.status}${active ? " active" : ""}`;
  article.dataset.cellId = cell.id;

  const index = document.createElement("span");
  index.className = `cell-index${cell.status === "success" ? " success" : ""}`;
  index.textContent = cell.executionCount ? `[${cell.executionCount}]` : "[ ]";
  article.append(index);

  const heading = document.createElement("div");
  heading.className = "cell-heading";
  const kind = document.createElement("span");
  kind.className = "cell-kind";
  kind.textContent = effectiveCellKind(cell);
  heading.append(kind, cellActions(cell, active));
  article.append(heading);

  if (active) {
    const activeKind = effectiveCellKind(cell);
    if (activeKind === "kedi") {
      const editorHost = document.createElement("div");
      editorHost.className = "editor-host";
      editorHost.style.height = editorHeight(cell.source);
      article.append(editorHost);
      activeEditorCellId = cell.id;
      activeEditor = await createKediEditor(
        editorHost,
        cell.source,
        (source) => {
          cell.source = source;
          markChanged();
        },
        {
          theme: state.theme,
          editor: {
            readOnly: state.runningCellId === cell.id,
            lineNumbers: "on",
            folding: false,
            lineDecorationsWidth: 8,
            lineNumbersMinChars: 3,
            wordWrap: "on",
            wrappingIndent: "same",
          },
          lspSource: () => lspSourceForCell(cell),
        },
      );
      const resizeEditor = () => {
        const height = Math.max(52, Math.min(520, activeEditor.getContentHeight() + 2));
        const nextHeight = `${height}px`;
        if (editorHost.style.height !== nextHeight) {
          editorHost.style.height = nextHeight;
          activeEditor.layout();
        }
      };
      activeEditorResizeDisposable = activeEditor.onDidContentSizeChange(resizeEditor);
      resizeEditor();
      if (cell.diagnostic) {
        setKediExecutionDiagnostic(activeEditor, cell.diagnostic);
      }
    } else {
      const textarea = document.createElement("textarea");
      textarea.className =
        activeKind === "terminal" ? "terminal-editor" : "markdown-editor";
      textarea.value = cell.source;
      textarea.placeholder =
        activeKind === "terminal" ? "!pip install package" : "Write Markdown";
      textarea.spellcheck = false;
      textarea.readOnly = state.runningCellId === cell.id;
      textarea.addEventListener("input", () => {
        cell.source = textarea.value;
        resizeTextarea(textarea);
        markChanged();
      });
      article.append(textarea);
      activeTextEditor = textarea;
      resizeTextarea(textarea);
    }
  } else if (effectiveCellKind(cell) === "markdown" && cell.status === "success") {
    const output = document.createElement("div");
    output.className = "markdown-output";
    renderMarkdown(output, cell.source);
    article.append(output);
  } else {
    const source = document.createElement("pre");
    source.className = "source-view draft-source";
    source.textContent = cell.source || "Empty cell";
    source.addEventListener("click", () => activateCell(cell.id));
    article.append(source);
  }

  appendOutput(article, cell);
  if (!active) {
    article.addEventListener("dblclick", () => activateCell(cell.id));
  }
  return article;
}

function cellActions(cell, active) {
  const actions = document.createElement("div");
  actions.className = "cell-actions";
  const kind = effectiveCellKind(cell);
  if (kind !== "markdown" || active || cell.status !== "success") {
    const label =
      kind === "markdown"
        ? "Render cell"
        : kind === "terminal"
          ? "Run command"
          : "Run cell";
    const run = iconButton("play", label);
    run.classList.add("run-button");
    run.disabled = Boolean(state.runningCellId);
    run.addEventListener("click", () => void runCell(cell.id));
    actions.append(run);
  }
  if (!active) {
    const edit = iconButton("pencil", "Edit cell");
    edit.addEventListener("click", () => activateCell(cell.id));
    actions.append(edit);
  }
  if (cell.source) {
    const copy = iconButton("copy", "Copy source");
    copy.addEventListener("click", () => void copyText(cell.source));
    actions.append(copy);
  }
  if (cell.status !== "success") {
    const remove = iconButton("trash-2", "Delete draft cell");
    remove.addEventListener("click", () => deleteCell(cell.id));
    actions.append(remove);
  }
  return actions;
}

function appendOutput(article, cell) {
  const streaming =
    state.runningCellId === cell.id && effectiveCellKind(cell) === "terminal";
  if (!streaming && !cell.stdout && !cell.result && !cell.error) {
    return;
  }
  const output = document.createElement("section");
  output.className = `output${cell.error ? " error" : ""}`;
  const heading = document.createElement("div");
  heading.className = "output-heading";
  heading.textContent = cell.error ? "Error" : "Output";
  output.append(heading);
  if (streaming || cell.stdout) {
    const stdout = document.createElement("pre");
    stdout.className = "output-content";
    stdout.textContent = cell.stdout;
    stdout.dataset.liveOutput = "";
    stdout.setAttribute("aria-live", "polite");
    output.append(stdout);
  }
  if (cell.result) {
    const result = document.createElement("pre");
    result.className = "output-content result-value";
    result.textContent = formatResult(cell.result);
    output.append(result);
  }
  if (cell.error) {
    const error = document.createElement("pre");
    error.className = "error-content";
    error.textContent = cell.error;
    output.append(error);
  }
  article.append(output);
}

async function runCell(cellId) {
  const cell = state.cells.find((item) => item.id === cellId);
  if (!cell || state.runningCellId) {
    return;
  }
  state.activeCellId = cellId;
  if (activeEditorCellId === cellId && activeEditor) {
    cell.source = activeEditor.getValue();
  }
  if (!cell.source.trim()) {
    showToast("Cell is empty");
    return;
  }
  const kind = effectiveCellKind(cell);
  if (kind === "markdown") {
    cell.status = "success";
    state.activeCellId = null;
    markChanged();
    await render();
    return;
  }

  state.runningCellId = cellId;
  cell.status = "running";
  cell.stdout = "";
  cell.result = null;
  cell.error = null;
  cell.diagnostic = null;
  setRuntimeStatus("Running", "busy");
  await render();
  try {
    await ensureSession();
    setRuntimeStatus("Running", "busy");
    cell.kind = kind;
    const endpoint =
      kind === "terminal"
        ? `/api/notebook/sessions/${state.sessionId}/terminal/execute`
        : `/api/notebook/sessions/${state.sessionId}/cells/execute`;
    const request = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cellId: cell.id, source: cell.source }),
    };
    const payload =
      kind === "terminal"
        ? await fetchTerminalStream(endpoint, request, cell)
        : await fetchJson(endpoint, request);
    cell.status = "success";
    cell.executionCount = payload.executionCount;
    cell.stdout = payload.stdout || "";
    cell.result = payload.result;
    state.activeCellId = cell.id;
    setRuntimeStatus("Ready");
  } catch (error) {
    cell.status = "error";
    cell.error = error?.message || String(error);
    cell.stdout = error?.payload?.stdout || cell.stdout;
    cell.diagnostic = error?.payload?.diagnostic || null;
    state.activeCellId = cell.id;
    setRuntimeStatus("Failed", "error");
  } finally {
    state.runningCellId = null;
    markChanged();
    await render();
    if (cell.diagnostic && activeEditorCellId === cell.id && activeEditor) {
      setKediExecutionDiagnostic(activeEditor, cell.diagnostic);
    }
  }
}

async function ensureSession() {
  if (state.sessionId) {
    return;
  }
  const selected = ui.runtime.selectedOptions[0];
  const mode = selected?.dataset.mode === "host" ? "host" : "browser";
  const pythonId = mode === "host" ? selected.value : null;
  setRuntimeStatus(mode === "browser" ? "Loading Pyodide" : "Starting Python", "busy");
  const payload = await fetchJson("/api/notebook/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, pythonId }),
  });
  state.sessionId = payload.sessionId;
  state.runtime = pythonId || "browser";
  ui.runtime.disabled = true;
  if (mode === "browser") {
    const runtime = await prewarmBrowserRuntime();
    browserBridge = new BrowserSessionBridge(state.sessionId, runtime);
    await browserBridge.start();
  }
}

class BrowserSessionBridge {
  constructor(sessionId, runtime) {
    this.sessionId = sessionId;
    this.active = false;
    this.runtime = runtime;
  }

  async start() {
    this.active = true;
    await this.runtime.preload();
    void this.poll();
  }

  async poll() {
    while (this.active) {
      try {
        const payload = await fetchJson(
          `/api/notebook/sessions/${this.sessionId}/bridge/request`,
        );
        if (!this.active) {
          return;
        }
        if (!payload.request) {
          continue;
        }
        const { id, ...request } = payload.request;
        let outputPosts = Promise.resolve();
        const postOutput = (stream, text) => {
          outputPosts = outputPosts.then(() =>
            fetchJson(
              `/api/notebook/sessions/${this.sessionId}/bridge/output`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ requestId: id, stream, text }),
              },
            ),
          );
        };
        const response = await this.runtime.execute(request, {
          onStdout: (text) => postOutput("stdout", text),
          onStderr: (text) => postOutput("stderr", text),
        });
        await outputPosts;
        await fetchJson(
          `/api/notebook/sessions/${this.sessionId}/bridge/response`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ requestId: id, response }),
          },
        );
      } catch (error) {
        if (this.active) {
          setRuntimeStatus(error?.message || "Browser runtime failed", "error");
        }
        return;
      }
    }
  }

  stop() {
    this.active = false;
  }
}

function prewarmBrowserRuntime() {
  if (!browserRuntime) {
    browserRuntime = new PyodideRuntime(
      (message) => {
        if (state.runtime === "browser" || browserBridge?.active) {
          setRuntimeStatus(message, "busy");
        }
      },
      {
        onStdin: () => Promise.resolve(globalThis.prompt("Standard input") ?? null),
      },
    );
  }
  if (!browserWarmup) {
    if (state.runtime === "browser") {
      setRuntimeStatus("Loading Pyodide", "busy");
    }
    browserWarmup = browserRuntime.preload().then(
      () => {
        if (state.runtime === "browser" && !state.runningCellId) {
          setRuntimeStatus("Ready");
        }
        return browserRuntime;
      },
      (error) => {
        browserWarmup = null;
        if (state.runtime === "browser") {
          setRuntimeStatus(error?.message || "Pyodide failed", "error");
        }
        throw error;
      },
    );
  }
  return browserWarmup;
}

function resetBrowserRuntime() {
  browserBridge?.stop();
  browserBridge = null;
  browserRuntime?.dispose();
  browserRuntime = null;
  browserWarmup = null;
  void prewarmBrowserRuntime().catch(() => {});
}

async function resetRuntimeSession() {
  if (state.runningCellId) {
    showToast("Wait for the active cell to finish");
    return;
  }
  if (state.sessionId) {
    await fetch(`/api/notebook/sessions/${state.sessionId}`, { method: "DELETE" });
  }
  resetBrowserRuntime();
  state.sessionId = null;
  ui.runtime.disabled = false;
  for (const cell of state.cells) {
    if (cell.kind !== "markdown") {
      cell.status = "draft";
      cell.executionCount = null;
      cell.stdout = "";
      cell.result = null;
      cell.error = null;
      cell.diagnostic = null;
    }
  }
  state.activeCellId = state.cells.find((cell) => cell.kind !== "markdown")?.id || null;
  setRuntimeStatus("New session");
  markChanged();
  await render();
}

async function newNotebook() {
  if (state.cells.some((cell) => cell.source.trim()) && !globalThis.confirm("Start a new notebook?")) {
    return;
  }
  await resetRuntimeSession();
  state.title = "Untitled notebook";
  state.cells = [];
  addCell("kedi", "");
  markChanged();
  await render();
}

function saveNotebook() {
  const notebookDocument = {
    format: "kedi-notebook",
    version: 1,
    title: state.title,
    cells: state.cells.map(({ kind, source }) => ({ kind, source })),
  };
  const blob = new Blob([JSON.stringify(notebookDocument, null, 2)], {
    type: "application/json",
  });
  const link = documentElement("a", {
    href: URL.createObjectURL(blob),
    download: `${fileStem(state.title)}.kedinb`,
  });
  link.click();
  URL.revokeObjectURL(link.href);
  ui.saveState.textContent = "Saved";
  showToast("Notebook saved");
}

async function openNotebookFile() {
  const file = ui.notebookFile.files?.[0];
  ui.notebookFile.value = "";
  if (!file) {
    return;
  }
  try {
    const value = JSON.parse(await file.text());
    if (value?.format !== "kedi-notebook" || !Array.isArray(value.cells)) {
      throw new Error("Not a Kedi notebook file");
    }
    await resetRuntimeSession();
    state.title = typeof value.title === "string" ? value.title : file.name;
    state.cells = value.cells.map((cell) => ({
      id: crypto.randomUUID(),
      kind: normalizeCellKind(cell.kind),
      source: typeof cell.source === "string" ? cell.source : "",
      status: "draft",
      executionCount: null,
      stdout: "",
      result: null,
      error: null,
      diagnostic: null,
    }));
    if (!state.cells.length) {
      addCell("kedi", "");
    }
    state.activeCellId = state.cells[0].id;
    markChanged();
    await render();
  } catch (error) {
    showToast(error?.message || "Cannot open notebook");
  }
}

async function loadRuntimes() {
  try {
    const payload = await fetchJson("/api/notebook/runtimes");
    ui.runtime.replaceChildren();
    const browser = document.createElement("option");
    browser.value = "browser";
    browser.dataset.mode = "browser";
    browser.textContent = payload.browser.label;
    ui.runtime.append(browser);
    for (const python of payload.host) {
      const option = document.createElement("option");
      option.value = python.id;
      option.dataset.mode = "host";
      option.textContent = python.label;
      ui.runtime.append(option);
    }
    ui.runtime.value = state.runtime;
    if (!ui.runtime.value) {
      state.runtime = "browser";
      ui.runtime.value = "browser";
    }
  } catch (error) {
    setRuntimeStatus("Runtime discovery failed", "error");
  }
}

function activateCell(cellId) {
  if (state.runningCellId) {
    return;
  }
  const cell = state.cells.find((item) => item.id === cellId);
  if (!cell) {
    return;
  }
  state.activeCellId = cellId;
  void render().then(focusActiveEditor);
}

function deleteCell(cellId) {
  if (state.runningCellId) {
    return;
  }
  const index = state.cells.findIndex((cell) => cell.id === cellId);
  if (index < 0) {
    return;
  }
  state.cells.splice(index, 1);
  if (!state.cells.length) {
    addCell("kedi", "");
  } else if (state.activeCellId === cellId) {
    const next = state.cells[Math.min(index, state.cells.length - 1)];
    state.activeCellId = next.id;
  }
  markChanged();
  void render();
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  setKediEditorTheme(state.theme);
  const icon = ui.themeToggle.querySelector("i, svg");
  if (icon) {
    icon.setAttribute("data-lucide", state.theme === "dark" ? "sun" : "moon");
  }
  globalThis.lucide?.createIcons();
}

function renderMarkdown(element, source) {
  const lines = source.split("\n");
  for (const line of lines) {
    const match = /^(#{1,3})\s+(.+)$/.exec(line);
    const node = document.createElement(match ? `h${match[1].length}` : "p");
    node.textContent = match ? match[2] : line || " ";
    element.append(node);
  }
}

function formatResult(result) {
  if (result.kind === "json") {
    return typeof result.value === "string" ? result.value : JSON.stringify(result.value, null, 2);
  }
  return result.value;
}

function editorHeight(source) {
  const lines = Math.max(1, Math.min(18, source.split("\n").length));
  return `${lines * 22 + 30}px`;
}

function lspSourceForCell(cell) {
  const previous = [];
  for (const item of state.cells) {
    if (item.id === cell.id) {
      break;
    }
    if (effectiveCellKind(item) === "kedi" && item.status === "success") {
      previous.push(item.source);
    }
  }
  if (!previous.length) {
    return { source: cell.source, lineOffset: 0 };
  }
  const prefix = `${previous.join("\n\n")}\n\n`;
  return {
    source: prefix + cell.source,
    lineOffset: prefix.split("\n").length - 1,
  };
}

function iconButton(icon, label) {
  const button = document.createElement("button");
  button.className = "icon-button compact";
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  const glyph = document.createElement("i");
  glyph.setAttribute("data-lucide", icon);
  button.append(glyph);
  return button;
}

function setRuntimeStatus(message, kind = "") {
  ui.runtimeStatus.className = `runtime-status${kind ? ` ${kind}` : ""}`;
  ui.runtimeStatus.lastElementChild.textContent = message;
}

function markChanged() {
  ui.saveState.textContent = "Unsaved";
  persistDraft();
}

function persistDraft() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      title: state.title,
      runtime: state.runtime,
      cells: state.cells.map(({ kind, source }) => ({ kind, source })),
    }),
  );
}

function restoreDraft() {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!value || !Array.isArray(value.cells)) {
      return;
    }
    state.title = typeof value.title === "string" ? value.title : state.title;
    state.runtime = typeof value.runtime === "string" ? value.runtime : "browser";
    state.cells = value.cells.map((cell) => ({
      id: crypto.randomUUID(),
      kind: normalizeCellKind(cell.kind),
      source: typeof cell.source === "string" ? cell.source : "",
      status: "draft",
      executionCount: null,
      stdout: "",
      result: null,
      error: null,
      diagnostic: null,
    }));
    state.activeCellId = state.cells[0]?.id || null;
  } catch {
    localStorage.removeItem(STORAGE_KEY);
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function fetchTerminalStream(url, options, cell) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json();
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
  if (!response.body) {
    throw new Error("Terminal response stream is unavailable");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  const consume = (line) => {
    if (!line.trim()) {
      return;
    }
    const event = JSON.parse(line);
    if (event.type === "output") {
      cell.stdout += event.text || "";
      updateLiveOutput(cell);
    } else if (event.type === "result") {
      result = event;
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      consume(line);
    }
    if (done) {
      consume(buffer);
      break;
    }
  }

  if (!result) {
    throw new Error("Terminal response ended before an execution result arrived");
  }
  const payload = { ...result };
  delete payload.type;
  if (payload.ok === false) {
    const error = new Error(payload.error || "Terminal command failed");
    error.payload = payload;
    throw error;
  }
  return payload;
}

function updateLiveOutput(cell) {
  const output = document.querySelector(
    `[data-cell-id="${cell.id}"] [data-live-output]`,
  );
  if (!output) {
    return;
  }
  output.textContent = cell.stdout;
  output.scrollTop = output.scrollHeight;
}

function disposeEditor() {
  activeEditorResizeDisposable?.dispose();
  activeEditorResizeDisposable = null;
  activeEditor?.dispose();
  activeEditor = null;
  activeEditorCellId = null;
  activeTextEditor = null;
}

function focusActiveEditor() {
  (activeEditor || activeTextEditor)?.focus();
}

function effectiveCellKind(cell) {
  if (cell.kind === "markdown") {
    return "markdown";
  }
  if (cell.kind === "terminal" || cell.source.trimStart().startsWith("!")) {
    return "terminal";
  }
  return "kedi";
}

function normalizeCellKind(kind) {
  return ["kedi", "terminal", "markdown"].includes(kind) ? kind : "kedi";
}

function resizeTextarea(textarea) {
  textarea.style.height = "0";
  textarea.style.height = `${Math.max(52, Math.min(520, textarea.scrollHeight))}px`;
}

async function copyText(value) {
  await navigator.clipboard.writeText(value);
  showToast("Copied source");
}

function showToast(message) {
  clearTimeout(toastTimer);
  ui.toast.textContent = message;
  ui.toast.hidden = false;
  toastTimer = setTimeout(() => {
    ui.toast.hidden = true;
  }, 2400);
}

function fileStem(value) {
  const stem = value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return stem || "notebook";
}

function documentElement(tag, attributes) {
  const element = document.createElement(tag);
  Object.assign(element, attributes);
  return element;
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.shiftKey && state.activeCellId) {
    event.preventDefault();
    void runCell(state.activeCellId);
  }
});
