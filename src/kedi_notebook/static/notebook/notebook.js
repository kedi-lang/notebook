import { PyodideRuntime } from "/pyodide-runtime.js";
import {
  createKediEditor,
  setKediExecutionDiagnostic,
} from "/kedi-editor.js";

const STORAGE_KEY = "kedi.notebook.draft.v1";
const MAX_NOTEBOOK_BYTES = 5_000_000;
const MAX_CELLS = 1_000;
const MAX_SOURCE_CHARS = 1_000_000;
const MAX_OUTPUT_CHARS = 200_000;
const OUTPUT_TRUNCATION_NOTICE = "\n[output truncated by Kedi Notebook]";
const fragment = new URLSearchParams(globalThis.location.hash.slice(1));
const fragmentToken = fragment.get("token");
if (fragmentToken) {
  sessionStorage.setItem("kedi.notebook.apiToken", fragmentToken);
  globalThis.history.replaceState(null, "", globalThis.location.pathname + globalThis.location.search);
}
const apiToken = fragmentToken || sessionStorage.getItem("kedi.notebook.apiToken");
globalThis.__KEDI_NOTEBOOK_API_TOKEN = apiToken;
const state = {
  title: "Untitled notebook",
  cells: [],
  activeCellId: null,
  sessionId: null,
  runtime: "browser",
  runningCellId: null,
  packageInstalling: false,
  pendingSessionSnapshot: null,
  dirty: false,
  interrupting: false,
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
  managePackages: document.querySelector("#manage-packages"),
  resetSession: document.querySelector("#reset-session"),
  interruptSession: document.querySelector("#interrupt-session"),
  newNotebook: document.querySelector("#new-notebook"),
  openNotebook: document.querySelector("#open-notebook"),
  saveNotebook: document.querySelector("#save-notebook"),
  saveDialog: document.querySelector("#save-dialog"),
  closeSaveDialog: document.querySelector("#close-save-dialog"),
  saveProgress: document.querySelector("#save-progress"),
  saveJustNotebook: document.querySelector("#save-just-notebook"),
  notebookFile: document.querySelector("#notebook-file"),
  manageSecrets: document.querySelector("#manage-secrets"),
  secretDialog: document.querySelector("#secret-dialog"),
  closeSecrets: document.querySelector("#close-secrets"),
  secretForm: document.querySelector("#secret-form"),
  secretName: document.querySelector("#secret-name"),
  secretValue: document.querySelector("#secret-value"),
  saveSecret: document.querySelector("#save-secret"),
  dotenvForm: document.querySelector("#dotenv-form"),
  dotenvPath: document.querySelector("#dotenv-path"),
  importDotenv: document.querySelector("#import-dotenv"),
  secretCount: document.querySelector("#secret-count"),
  secretList: document.querySelector("#secret-list"),
  packageDialog: document.querySelector("#package-dialog"),
  packageEnvironment: document.querySelector("#package-environment"),
  closePackages: document.querySelector("#close-packages"),
  packageForm: document.querySelector("#package-form"),
  packageRequirements: document.querySelector("#package-requirements"),
  installPackages: document.querySelector("#install-packages"),
  packageOutput: document.querySelector("#package-output"),
  refreshPackages: document.querySelector("#refresh-packages"),
  packageList: document.querySelector("#package-list"),
  toast: document.querySelector("#toast"),
};

const kediEditors = new Map();
const editorResizeDisposables = new Map();
const textEditors = new Map();
let browserBridge = null;
let browserRuntime = null;
let browserWarmup = null;
let toastTimer = null;
let activeRequestController = null;
let liveOutputFrame = null;

restoreDraft();
if (!state.cells.length) {
  addCell(
    "kedi",
    "[values: list[int]] = `[2, 3, 5]`\n= `sum(value * value for value in values)`",
  );
}
bindEvents();
void prewarmBrowserRuntime().catch(() => {});
await loadRuntimes();
await render();
globalThis.lucide?.createIcons();

function bindEvents() {
  ui.addCell.addEventListener("click", () => addCellFromControl(false));
  ui.appendCell.addEventListener("click", () => addCellFromControl(true));
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
    syncRuntimeControls();
    markChanged();
  });
  ui.managePackages.addEventListener("click", () => void openPackageManager());
  ui.manageSecrets.addEventListener("click", () => void openSecretManager());
  ui.closeSecrets.addEventListener("click", () => {
    ui.secretValue.value = "";
    ui.secretDialog.close();
  });
  ui.secretForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveSecret();
  });
  ui.dotenvForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void importDotenv();
  });
  ui.closePackages.addEventListener("click", () => ui.packageDialog.close());
  ui.refreshPackages.addEventListener("click", () => void loadPackages());
  ui.packageForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void installPackages();
  });
  ui.resetSession.addEventListener("click", () => void resetRuntimeSession());
  ui.interruptSession.addEventListener("click", () => void interruptExecution());
  ui.newNotebook.addEventListener("click", () => void newNotebook());
  ui.saveNotebook.addEventListener("click", openSaveDialog);
  ui.closeSaveDialog.addEventListener("click", () => ui.saveDialog.close());
  ui.saveProgress.addEventListener("click", () => void saveNotebook("progress"));
  ui.saveJustNotebook.addEventListener("click", () => void saveNotebook("notebook"));
  ui.openNotebook.addEventListener("click", () => ui.notebookFile.click());
  ui.notebookFile.addEventListener("change", () => void openNotebookFile());
  window.addEventListener("beforeunload", () => {
    persistDraft();
    if (state.sessionId) {
      void apiFetch(`/api/notebook/sessions/${state.sessionId}`, {
        method: "DELETE",
        keepalive: true,
      });
    }
    browserBridge?.stop();
    browserRuntime?.dispose();
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

function addCellFromControl(append) {
  const kind = ["kedi", "terminal", "markdown"].includes(ui.cellKind.value)
    ? ui.cellKind.value
    : "kedi";
  const activeIndex = state.cells.findIndex((cell) => cell.id === state.activeCellId);
  const index = append || activeIndex < 0 ? state.cells.length : activeIndex + 1;
  addCell(kind, kind === "terminal" ? "!" : "", index);
  void render().then(focusActiveEditor);
}

function addCell(kind, source, index = state.cells.length) {
  if (state.cells.length >= MAX_CELLS) {
    showToast(`A notebook can contain at most ${MAX_CELLS} cells`);
    return null;
  }
  const cell = {
    id: crypto.randomUUID(),
    kind,
    source,
    status: "draft",
    stdout: "",
    result: null,
    error: null,
    diagnostic: null,
    hidden: false,
  };
  state.cells.splice(index, 0, cell);
  state.activeCellId = cell.id;
  markChanged();
  return cell;
}

async function render() {
  disposeEditors();
  ui.title.value = state.title;
  ui.cells.replaceChildren();
  for (const [cellPosition, cell] of state.cells.entries()) {
    ui.cells.append(await renderCell(cell, cellPosition + 1));
  }
  ui.runtime.value = state.runtime;
  ui.runtime.disabled = Boolean(state.sessionId);
  syncRuntimeControls();
  globalThis.lucide?.createIcons();
}

async function renderCell(cell, cellNumber) {
  const active = cell.id === state.activeCellId;
  const article = document.createElement("article");
  article.className = `cell ${cell.status}${active ? " active" : ""}${cell.hidden ? " hidden-cell" : ""}`;
  article.dataset.cellId = cell.id;
  article.addEventListener("focusin", () => selectCell(cell.id));
  article.addEventListener("pointerdown", () => selectCell(cell.id));

  const gutter = document.createElement("div");
  gutter.className = "cell-gutter";
  const index = document.createElement("span");
  index.className = `cell-index${cell.status === "success" ? " success" : ""}`;
  index.textContent = `[${cellNumber}]`;
  if (!cell.hidden) {
    const run = iconButton("play", runCellLabel(cell));
    run.classList.add("cell-run-button", "run-button");
    run.dataset.cellRunId = cell.id;
    run.dataset.idleLabel = runCellLabel(cell);
    run.disabled = Boolean(state.runningCellId);
    run.addEventListener("click", () => void runCell(cell.id));
    gutter.append(run);
  }
  gutter.append(index);
  article.append(gutter);

  const heading = document.createElement("div");
  heading.className = "cell-heading";
  const identity = document.createElement("div");
  identity.className = "cell-identity";
  identity.append(cellKindSelect(cell));
  if (cell.hidden) {
    const hidden = document.createElement("span");
    hidden.className = "cell-hidden-label";
    hidden.textContent = "Hidden";
    identity.append(hidden);
  }
  heading.append(identity, cellActions(cell));
  article.append(heading);

  if (cell.hidden) {
    return article;
  }

  const kind = effectiveCellKind(cell);
  if (kind === "kedi") {
    const editorHost = document.createElement("div");
    editorHost.className = "editor-host";
    editorHost.style.height = editorHeight(cell.source);
    article.append(editorHost);
    const editor = await createKediEditor(
      editorHost,
      cell.source,
      (source) => {
        cell.source = source;
        markChanged();
      },
      {
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
    kediEditors.set(cell.id, editor);
    const resizeEditor = () => {
      const height = Math.max(52, Math.min(520, editor.getContentHeight() + 2));
      const nextHeight = `${height}px`;
      if (editorHost.style.height !== nextHeight) {
        editorHost.style.height = nextHeight;
        editor.layout();
      }
    };
    editorResizeDisposables.set(
      cell.id,
      editor.onDidContentSizeChange(resizeEditor),
    );
    resizeEditor();
    setKediExecutionDiagnostic(editor, cell.diagnostic);
  } else {
    const textarea = document.createElement("textarea");
    textarea.className = kind === "terminal" ? "terminal-editor" : "markdown-editor";
    textarea.value = cell.source;
    textarea.placeholder = kind === "terminal" ? "!pip install package" : "Write Markdown";
    textarea.spellcheck = false;
    textarea.readOnly = state.runningCellId === cell.id;
    textarea.addEventListener("input", () => {
      cell.source = textarea.value;
      resizeTextarea(textarea);
      markChanged();
    });
    article.append(textarea);
    textEditors.set(cell.id, textarea);
    resizeTextarea(textarea);
    if (kind === "markdown" && cell.status === "success") {
      appendMarkdownPreview(article, cell);
    }
  }
  appendOutput(article, cell);
  return article;
}

function cellActions(cell) {
  const actions = document.createElement("div");
  actions.className = "cell-actions";
  const visibility = iconButton(cell.hidden ? "eye" : "eye-off", cell.hidden ? "Show cell" : "Hide cell");
  visibility.disabled = Boolean(state.runningCellId);
  visibility.addEventListener("click", () => setCellHidden(cell.id, !cell.hidden));
  actions.append(visibility);
  if (cell.source) {
    const copy = iconButton("copy", "Copy source");
    copy.addEventListener("click", () => void copyText(cell.source));
    actions.append(copy);
  }
  const position = state.cells.findIndex((item) => item.id === cell.id);
  if (position > 0) {
    const moveUp = iconButton("arrow-up", "Move cell up");
    moveUp.disabled = Boolean(state.runningCellId);
    moveUp.addEventListener("click", () => moveCell(cell.id, -1));
    actions.append(moveUp);
  }
  if (position >= 0 && position < state.cells.length - 1) {
    const moveDown = iconButton("arrow-down", "Move cell down");
    moveDown.disabled = Boolean(state.runningCellId);
    moveDown.addEventListener("click", () => moveCell(cell.id, 1));
    actions.append(moveDown);
  }
  const remove = iconButton("trash-2", "Delete cell");
  remove.disabled = Boolean(state.runningCellId);
  remove.addEventListener("click", () => deleteCell(cell.id));
  actions.append(remove);
  return actions;
}

function cellKindSelect(cell) {
  const select = document.createElement("select");
  select.className = "cell-kind-select";
  select.setAttribute("aria-label", "Cell type");
  for (const [value, label] of [
    ["kedi", "Kedi"],
    ["markdown", "Markdown"],
    ["terminal", "Terminal"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
  select.value = effectiveCellKind(cell);
  select.disabled = Boolean(state.runningCellId);
  select.addEventListener("change", () => changeCellKind(cell.id, select.value));
  return select;
}

function runCellLabel(cell) {
  const kind = effectiveCellKind(cell);
  if (kind === "markdown") {
    return "Render cell";
  }
  return kind === "terminal" ? "Run command" : "Run cell";
}

function appendMarkdownPreview(article, cell) {
  const preview = document.createElement("section");
  preview.className = "output markdown-preview";
  const heading = document.createElement("div");
  heading.className = "output-heading";
  heading.textContent = "Preview";
  const content = document.createElement("div");
  content.className = "markdown-output";
  renderMarkdown(content, cell.source);
  preview.append(heading, content);
  article.append(preview);
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
  selectCell(cellId);
  if (!cell.source.trim()) {
    showToast("Cell is empty");
    return;
  }
  if (cell.source.length > MAX_SOURCE_CHARS) {
    showToast("Cell source is larger than 1 MB");
    return;
  }
  const kind = effectiveCellKind(cell);
  if (kind === "markdown") {
    cell.status = "success";
    await render();
    focusActiveEditor();
    return;
  }

  const kindChanged = cell.kind !== kind;
  if (kindChanged) {
    cell.kind = kind;
    markChanged();
  }
  state.runningCellId = cellId;
  state.interrupting = false;
  ui.interruptSession.hidden = false;
  ui.interruptSession.disabled = false;
  cell.status = "running";
  cell.stdout = "";
  cell.result = null;
  cell.error = null;
  cell.diagnostic = null;
  const activeEditor = kediEditors.get(cell.id);
  if (activeEditor) {
    setKediExecutionDiagnostic(activeEditor, null);
  }
  setRuntimeStatus("Running", "busy");
  syncRuntimeControls();
  updateCellPresentation(cell);
  try {
    await ensureSession();
    setRuntimeStatus("Running", "busy");
    const endpoint =
      kind === "terminal"
        ? `/api/notebook/sessions/${state.sessionId}/terminal/execute`
        : `/api/notebook/sessions/${state.sessionId}/cells/execute`;
    activeRequestController = new AbortController();
    const request = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cellId: cell.id, source: cell.source }),
      signal: activeRequestController.signal,
    };
    const payload =
      kind === "terminal"
        ? await fetchTerminalStream(endpoint, request, cell)
        : await fetchJson(endpoint, request);
    cell.status = "success";
    cell.stdout = payload.stdout || "";
    cell.result = payload.result;
    state.activeCellId = cell.id;
    setRuntimeStatus("Ready");
  } catch (error) {
    cell.status = "error";
    cell.error = state.interrupting
      ? "Execution interrupted; runtime state was reset"
      : error?.message || String(error);
    cell.stdout = error?.payload?.stdout || cell.stdout;
    cell.diagnostic = error?.payload?.diagnostic || null;
    if (state.interrupting || error?.payload?.runtimeReset) {
      releaseRuntimeSession();
    }
    state.activeCellId = cell.id;
    setRuntimeStatus("Failed", "error");
  } finally {
    activeRequestController = null;
    state.runningCellId = null;
    state.interrupting = false;
    ui.interruptSession.hidden = true;
    syncRuntimeControls();
    if (kindChanged) {
      await render();
    } else {
      updateCellPresentation(cell);
    }
    const editor = kediEditors.get(cell.id);
    if (editor) {
      setKediExecutionDiagnostic(editor, cell.diagnostic);
    }
  }
}

function updateCellPresentation(cell) {
  const article = document.querySelector(`[data-cell-id="${cell.id}"]`);
  if (!article) {
    return;
  }
  const active = cell.id === state.activeCellId;
  article.className = `cell ${cell.status}${active ? " active" : ""}${cell.hidden ? " hidden-cell" : ""}`;
  const index = article.querySelector(".cell-index");
  index?.classList.toggle("success", cell.status === "success");
  article.querySelector(".output")?.remove();
  if (effectiveCellKind(cell) === "markdown" && cell.status === "success") {
    appendMarkdownPreview(article, cell);
  }
  appendOutput(article, cell);
  kediEditors.get(cell.id)?.updateOptions?.({ readOnly: state.runningCellId === cell.id });
  const textEditor = textEditors.get(cell.id);
  if (textEditor) {
    textEditor.readOnly = state.runningCellId === cell.id;
  }
  for (const button of document.querySelectorAll(
    ".cell-actions button, .cell-run-button, .cell-kind-select",
  )) {
    button.disabled = Boolean(state.runningCellId);
  }
  syncCellRunButtons();
  globalThis.lucide?.createIcons();
}

function syncCellRunButtons() {
  for (const button of document.querySelectorAll(".cell-run-button")) {
    const running = button.dataset.cellRunId === state.runningCellId;
    const stateChanged = button.dataset.running !== String(running);
    button.dataset.running = String(running);
    button.classList.toggle("running", running);
    button.disabled = Boolean(state.runningCellId);
    const label = running ? "Running cell" : button.dataset.idleLabel || "Run cell";
    button.title = label;
    button.setAttribute("aria-label", label);
    if (stateChanged) {
      const glyph = document.createElement("i");
      glyph.setAttribute("data-lucide", running ? "loader-circle" : "play");
      button.replaceChildren(glyph);
    }
  }
}

async function interruptExecution() {
  if (!state.runningCellId || !state.sessionId || state.interrupting) {
    return;
  }
  state.interrupting = true;
  ui.interruptSession.disabled = true;
  setRuntimeStatus("Interrupting", "busy");
  const sessionId = state.sessionId;
  try {
    await fetchJson(`/api/notebook/sessions/${sessionId}/interrupt`, { method: "POST" });
  } catch (error) {
    if (error?.payload?.error && !String(error.payload.error).includes("not found")) {
      showToast(error.message || "Could not interrupt execution");
    }
  } finally {
    activeRequestController?.abort();
    releaseRuntimeSession();
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
  const restoring = state.pendingSessionSnapshot !== null;
  const payload = await fetchJson(
    restoring ? "/api/notebook/sessions/restore" : "/api/notebook/sessions",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        pythonId,
        ...(restoring ? { snapshot: state.pendingSessionSnapshot } : {}),
      }),
    },
  );
  state.sessionId = payload.sessionId;
  state.pendingSessionSnapshot = null;
  state.runtime = pythonId || "browser";
  ui.runtime.disabled = true;
  if (payload.python?.environment) {
    ui.packageEnvironment.textContent = payload.python.environment;
    ui.packageEnvironment.title = payload.python.environment;
  }
  syncRuntimeControls();
  if (mode === "browser") {
    const runtime = await prewarmBrowserRuntime();
    browserBridge = new BrowserSessionBridge(state.sessionId, runtime);
    await browserBridge.start();
  } else {
    setRuntimeStatus("Ready");
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

function releaseRuntimeSession() {
  browserBridge?.stop();
  browserBridge = null;
  if (state.runtime === "browser") {
    resetBrowserRuntime();
  }
  state.sessionId = null;
  state.pendingSessionSnapshot = null;
  ui.runtime.disabled = false;
  ui.packageDialog.close();
  ui.packageEnvironment.textContent = "";
  syncRuntimeControls();
}

async function resetRuntimeSession() {
  if (state.runningCellId || state.packageInstalling) {
    showToast("Wait for the active operation to finish");
    return;
  }
  if (state.sessionId) {
    await apiFetch(`/api/notebook/sessions/${state.sessionId}`, { method: "DELETE" });
  }
  releaseRuntimeSession();
  for (const cell of state.cells) {
    if (cell.kind !== "markdown") {
      cell.status = "draft";
      cell.stdout = "";
      cell.result = null;
      cell.error = null;
      cell.diagnostic = null;
    }
  }
  state.activeCellId = state.cells.find((cell) => cell.kind !== "markdown")?.id || null;
  setRuntimeStatus("New session");
  await render();
}

async function newNotebook() {
  if (!confirmDiscard("Start a new notebook and discard unsaved changes?")) {
    return;
  }
  await resetRuntimeSession();
  state.title = "Untitled notebook";
  state.cells = [];
  addCell("kedi", "");
  markChanged();
  await render();
}

function openSaveDialog() {
  if (state.runningCellId || state.packageInstalling) {
    showToast("Wait for the active operation to finish");
    return;
  }
  ui.saveDialog.showModal();
  globalThis.lucide?.createIcons();
}

async function saveNotebook(mode) {
  if (!["progress", "notebook"].includes(mode)) {
    throw new Error("Unsupported notebook save mode");
  }
  let sessionSnapshot = null;
  if (mode === "progress") {
    try {
      if (state.sessionId) {
        const payload = await fetchJson(
          `/api/notebook/sessions/${state.sessionId}/snapshot`,
          { method: "POST" },
        );
        sessionSnapshot = payload.snapshot;
      } else if (state.pendingSessionSnapshot) {
        sessionSnapshot = state.pendingSessionSnapshot;
      }
    } catch (error) {
      showToast(error?.message || "Current Kedi session cannot be saved");
      return;
    }
  }
  const notebookDocument = {
    format: "kedi-notebook",
    version: 2,
    saveMode: mode,
    title: state.title,
    cells: state.cells.map((cell) => {
      const saved = { kind: cell.kind, source: cell.source, hidden: cell.hidden };
      if (mode === "progress") {
        saved.progress = {
          status: cell.status === "running" ? "draft" : cell.status,
          stdout: cell.stdout,
          result: cell.result,
          error: cell.error,
        };
      }
      return saved;
    }),
  };
  if (sessionSnapshot) {
    notebookDocument.sessionSnapshot = sessionSnapshot;
  }
  const blob = new Blob([JSON.stringify(notebookDocument, null, 2)], {
    type: "application/json",
  });
  if (blob.size > MAX_NOTEBOOK_BYTES) {
    showToast("Notebook is larger than the 5 MB file limit");
    return;
  }
  ui.saveDialog.close();
  const link = documentElement("a", {
    href: URL.createObjectURL(blob),
    download: `${fileStem(state.title)}.kedinb`,
  });
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 0);
  state.dirty = false;
  ui.saveState.textContent = "Saved";
  showToast(mode === "progress" ? "Progress saved" : "Notebook saved");
}

async function openNotebookFile() {
  const file = ui.notebookFile.files?.[0];
  ui.notebookFile.value = "";
  if (!file) {
    return;
  }
  try {
    if (file.size > MAX_NOTEBOOK_BYTES) {
      throw new Error("Notebook file is larger than 5 MB");
    }
    const value = JSON.parse(await file.text());
    validateNotebookDocument(value);
    if (!confirmDiscard("Open this notebook and discard unsaved changes?")) {
      return;
    }
    await resetRuntimeSession();
    state.pendingSessionSnapshot = value.sessionSnapshot || null;
    state.title = typeof value.title === "string" ? value.title : file.name;
    state.cells = value.cells.map((cell) => {
      const progress = value.saveMode === "progress" ? cell.progress : null;
      return {
        id: crypto.randomUUID(),
        kind: normalizeCellKind(cell.kind),
        source: typeof cell.source === "string" ? cell.source : "",
        status: progress?.status || "draft",
        stdout: progress?.stdout || "",
        result: progress?.result ?? null,
        error: progress?.error || null,
        diagnostic: null,
        hidden: cell.hidden === true,
      };
    });
    if (!state.cells.length) {
      addCell("kedi", "");
    }
    state.activeCellId = state.cells[0].id;
    state.dirty = false;
    ui.saveState.textContent = "Opened";
    persistDraft();
    await render();
    showToast(state.pendingSessionSnapshot ? "Notebook progress loaded" : "Notebook opened");
  } catch (error) {
    showToast(error?.message || "Cannot open notebook");
  }
}

function validateNotebookDocument(value) {
  if (
    !value ||
    value.format !== "kedi-notebook" ||
    ![1, 2].includes(value.version) ||
    !Array.isArray(value.cells)
  ) {
    throw new Error("Not a supported Kedi notebook file");
  }
  if (value.cells.length > MAX_CELLS) {
    throw new Error(`Notebook has more than ${MAX_CELLS} cells`);
  }
  for (const [index, cell] of value.cells.entries()) {
    if (!cell || typeof cell !== "object") {
      throw new Error(`Cell ${index + 1} is invalid`);
    }
    if (typeof cell.source !== "string") {
      throw new Error(`Cell ${index + 1} source must be text`);
    }
    if (cell.source.length > MAX_SOURCE_CHARS) {
      throw new Error(`Cell ${index + 1} source is larger than 1 MB`);
    }
    if (!["kedi", "terminal", "markdown"].includes(cell.kind)) {
      throw new Error(`Cell ${index + 1} has an unsupported type`);
    }
    if (cell.hidden !== undefined && typeof cell.hidden !== "boolean") {
      throw new Error(`Cell ${index + 1} hidden state must be a boolean`);
    }
    if (cell.progress !== undefined) {
      validateCellProgress(cell.progress, index);
    }
  }
  if (value.version === 2 && !["progress", "notebook"].includes(value.saveMode)) {
    throw new Error("Notebook has an unsupported save mode");
  }
  if (
    value.sessionSnapshot !== undefined &&
    (typeof value.sessionSnapshot !== "string" || !value.sessionSnapshot)
  ) {
    throw new Error("Notebook session snapshot is invalid");
  }
}

function validateCellProgress(progress, index) {
  if (!progress || typeof progress !== "object" || Array.isArray(progress)) {
    throw new Error(`Cell ${index + 1} progress is invalid`);
  }
  if (!["draft", "success", "error"].includes(progress.status)) {
    throw new Error(`Cell ${index + 1} progress status is invalid`);
  }
  for (const key of ["stdout", "error"]) {
    if (progress[key] !== null && typeof progress[key] !== "string") {
      throw new Error(`Cell ${index + 1} ${key} must be text`);
    }
    if (typeof progress[key] === "string" && progress[key].length > MAX_OUTPUT_CHARS) {
      throw new Error(`Cell ${index + 1} ${key} is too large`);
    }
  }
  if (
    progress.result !== null &&
    progress.result !== undefined &&
    JSON.stringify(progress.result).length > MAX_OUTPUT_CHARS
  ) {
    throw new Error(`Cell ${index + 1} result is too large`);
  }
}

function confirmDiscard(message) {
  return !state.dirty || globalThis.confirm(message);
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
    syncRuntimeControls();
  } catch (error) {
    setRuntimeStatus("Runtime discovery failed", "error");
  }
}

function syncRuntimeControls() {
  const host = ui.runtime.selectedOptions[0]?.dataset.mode === "host";
  ui.managePackages.hidden = !host;
  ui.managePackages.disabled = state.packageInstalling || Boolean(state.runningCellId);
}

async function openPackageManager() {
  if (ui.runtime.selectedOptions[0]?.dataset.mode !== "host") {
    return;
  }
  try {
    await ensureSession();
    ui.packageDialog.showModal();
    globalThis.lucide?.createIcons();
    await loadPackages();
  } catch (error) {
    showToast(error?.message || "Could not open package manager");
  }
}

async function openSecretManager() {
  ui.secretDialog.showModal();
  globalThis.lucide?.createIcons();
  await loadSecrets();
  ui.secretName.focus();
}

async function loadSecrets() {
  ui.secretList.replaceChildren(
    documentElement("span", { className: "secret-empty", textContent: "Loading..." }),
  );
  try {
    const payload = await fetchJson("/api/notebook/secrets");
    renderSecretList(payload.configured || []);
  } catch (error) {
    ui.secretList.replaceChildren(
      documentElement("span", {
        className: "secret-empty package-list-error",
        textContent: error?.message || "Could not load environment values",
      }),
    );
  }
}

function renderSecretList(names) {
  ui.secretList.replaceChildren();
  ui.secretCount.textContent = String(names.length);
  if (!names.length) {
    ui.secretList.append(
      documentElement("span", {
        className: "secret-empty",
        textContent: "No environment values configured",
      }),
    );
    return;
  }
  for (const name of names) {
    const row = document.createElement("div");
    row.className = "secret-row";
    const remove = document.createElement("button");
    remove.className = "icon-button compact danger-action";
    remove.type = "button";
    remove.title = `Delete ${name}`;
    remove.setAttribute("aria-label", `Delete ${name}`);
    remove.innerHTML = '<i data-lucide="trash-2"></i>';
    remove.addEventListener("click", () => void deleteSecret(name));
    row.append(
      documentElement("span", { className: "secret-name", textContent: name }),
      remove,
    );
    ui.secretList.append(row);
  }
  globalThis.lucide?.createIcons();
}

async function saveSecret() {
  const name = ui.secretName.value.trim();
  const value = ui.secretValue.value;
  if (!name || !value) {
    return;
  }
  ui.saveSecret.disabled = true;
  try {
    const payload = await fetchJson("/api/notebook/secrets", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, value }),
    });
    ui.secretValue.value = "";
    renderSecretList(payload.configured || []);
    await applySecretRuntimeReset(payload, `${name} saved`);
  } catch (error) {
    showToast(error?.message || "Could not save environment value");
  } finally {
    ui.saveSecret.disabled = false;
  }
}

async function importDotenv() {
  const path = ui.dotenvPath.value.trim();
  if (!path) {
    return;
  }
  ui.importDotenv.disabled = true;
  try {
    const payload = await fetchJson("/api/notebook/secrets/import-dotenv", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    renderSecretList(payload.configured || []);
    const count = payload.imported?.length || 0;
    await applySecretRuntimeReset(
      payload,
      `${count} environment value${count === 1 ? "" : "s"} imported`,
    );
  } catch (error) {
    showToast(error?.message || "Could not import .env file");
  } finally {
    ui.importDotenv.disabled = false;
  }
}

async function deleteSecret(name) {
  if (!globalThis.confirm(`Delete ${name} from Secret Manager?`)) {
    return;
  }
  try {
    const payload = await fetchJson(
      `/api/notebook/secrets/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    renderSecretList(payload.configured || []);
    await applySecretRuntimeReset(payload, `${name} deleted`);
  } catch (error) {
    showToast(error?.message || "Could not delete environment value");
  }
}

async function applySecretRuntimeReset(payload, message) {
  if (payload.runtimeReset) {
    releaseRuntimeSession();
    for (const cell of state.cells) {
      if (cell.kind !== "markdown") {
        cell.status = "draft";
        cell.stdout = "";
        cell.result = null;
        cell.error = null;
        cell.diagnostic = null;
      }
    }
    setRuntimeStatus("New session");
    await render();
  }
  showToast(message);
}

async function loadPackages() {
  if (!state.sessionId || ui.runtime.selectedOptions[0]?.dataset.mode !== "host") {
    return;
  }
  ui.refreshPackages.disabled = true;
  ui.packageList.replaceChildren(documentElement("span", { textContent: "Loading..." }));
  try {
    const payload = await fetchJson(`/api/notebook/sessions/${state.sessionId}/packages`);
    ui.packageEnvironment.textContent = payload.environment || "Managed host environment";
    ui.packageEnvironment.title = payload.environment || "";
    renderPackageList(payload.packages || []);
  } catch (error) {
    ui.packageList.replaceChildren(
      documentElement("span", {
        className: "package-list-error",
        textContent: error?.message || "Could not list packages",
      }),
    );
  } finally {
    ui.refreshPackages.disabled = state.packageInstalling;
  }
}

function renderPackageList(packages) {
  ui.packageList.replaceChildren();
  if (!packages.length) {
    ui.packageList.append(documentElement("span", { textContent: "No packages installed" }));
    return;
  }
  for (const item of packages) {
    const row = document.createElement("div");
    row.className = "package-row";
    row.append(
      documentElement("span", { textContent: item.name }),
      documentElement("span", { className: "package-version", textContent: item.version }),
    );
    ui.packageList.append(row);
  }
}

async function installPackages() {
  if (state.packageInstalling || !state.sessionId) {
    return;
  }
  const packages = ui.packageRequirements.value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
  if (!packages.length) {
    showToast("Enter at least one package requirement");
    return;
  }
  state.packageInstalling = true;
  ui.installPackages.disabled = true;
  ui.refreshPackages.disabled = true;
  ui.closePackages.disabled = true;
  ui.packageOutput.hidden = false;
  ui.packageOutput.textContent = "";
  syncRuntimeControls();
  try {
    const response = await apiFetch(
      `/api/notebook/sessions/${state.sessionId}/packages/install`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ packages }),
      },
    );
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    let result = null;
    await consumeNdjson(response, (event) => {
      if (event.type === "output") {
        ui.packageOutput.textContent = appendBoundedOutput(
          ui.packageOutput.textContent,
          event.text || "",
        );
        ui.packageOutput.scrollTop = ui.packageOutput.scrollHeight;
      } else if (event.type === "result") {
        result = event;
      }
    });
    if (!result) {
      throw new Error("Package installation ended without a result");
    }
    if (result.ok === false) {
      throw new Error(result.error || "Package installation failed");
    }
    ui.packageRequirements.value = "";
    showToast("Packages installed");
    await loadPackages();
  } catch (error) {
    const message = error?.message || "Package installation failed";
    ui.packageOutput.textContent = appendBoundedOutput(
      ui.packageOutput.textContent,
      `${ui.packageOutput.textContent ? "\n" : ""}${message}`,
    );
    showToast(message);
  } finally {
    state.packageInstalling = false;
    ui.installPackages.disabled = false;
    ui.refreshPackages.disabled = false;
    ui.closePackages.disabled = false;
    syncRuntimeControls();
  }
}

function selectCell(cellId) {
  if (!state.cells.some((cell) => cell.id === cellId)) {
    return;
  }
  state.activeCellId = cellId;
  for (const article of ui.cells.querySelectorAll(".cell")) {
    article.classList.toggle("active", article.dataset.cellId === cellId);
  }
}

function changeCellKind(cellId, nextKind) {
  if (state.runningCellId) {
    return;
  }
  const cell = state.cells.find((item) => item.id === cellId);
  const normalized = normalizeCellKind(nextKind);
  if (!cell || effectiveCellKind(cell) === normalized) {
    return;
  }
  if (effectiveCellKind(cell) === "terminal" && normalized !== "terminal") {
    cell.source = cell.source.replace(/^(\s*)!/, "$1");
  } else if (normalized === "terminal" && !cell.source.trimStart().startsWith("!")) {
    cell.source = `!${cell.source}`;
  }
  cell.kind = normalized;
  cell.status = "draft";
  cell.stdout = "";
  cell.result = null;
  cell.error = null;
  cell.diagnostic = null;
  state.activeCellId = cell.id;
  markChanged();
  void render().then(focusActiveEditor);
}

function setCellHidden(cellId, hidden) {
  if (state.runningCellId) {
    return;
  }
  const cell = state.cells.find((item) => item.id === cellId);
  if (!cell || cell.hidden === hidden) {
    return;
  }
  cell.hidden = hidden;
  state.activeCellId = cell.id;
  markChanged();
  void render().then(() => {
    if (!hidden) {
      focusActiveEditor();
    }
  });
}

function deleteCell(cellId) {
  if (state.runningCellId) {
    return;
  }
  const index = state.cells.findIndex((cell) => cell.id === cellId);
  if (index < 0) {
    return;
  }
  const cell = state.cells[index];
  if (
    (cell.status === "success" || cell.stdout || cell.result || cell.error) &&
    !globalThis.confirm("Delete this cell and its output?")
  ) {
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

function moveCell(cellId, direction) {
  if (state.runningCellId) {
    return;
  }
  const index = state.cells.findIndex((cell) => cell.id === cellId);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= state.cells.length) {
    return;
  }
  const [cell] = state.cells.splice(index, 1);
  state.cells.splice(target, 0, cell);
  state.activeCellId = cell.id;
  markChanged();
  void render().then(focusActiveEditor);
}

function renderMarkdown(element, source) {
  const lines = source.split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const language = line.slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      index += index < lines.length ? 1 : 0;
      const pre = document.createElement("pre");
      const codeElement = document.createElement("code");
      if (language) {
        codeElement.dataset.language = language;
      }
      codeElement.textContent = code.join("\n");
      pre.append(codeElement);
      element.append(pre);
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      const node = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(node, heading[2]);
      element.append(node);
      index += 1;
      continue;
    }
    const listMatch = /^\s*(?:([-*])|(\d+)\.)\s+(.+)$/.exec(line);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (index < lines.length) {
        const item = /^\s*(?:([-*])|(\d+)\.)\s+(.+)$/.exec(lines[index]);
        if (!item || Boolean(item[2]) !== ordered) {
          break;
        }
        const listItem = document.createElement("li");
        appendInlineMarkdown(listItem, item[3]);
        list.append(listItem);
        index += 1;
      }
      element.append(list);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = document.createElement("blockquote");
      const parts = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        parts.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      appendInlineMarkdown(quote, parts.join("\n"));
      element.append(quote);
      continue;
    }
    if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) {
      element.append(document.createElement("hr"));
      index += 1;
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()) {
      paragraph.push(lines[index]);
      index += 1;
    }
    const node = document.createElement("p");
    appendInlineMarkdown(node, paragraph.join("\n"));
    element.append(node);
  }
}

function appendInlineMarkdown(element, value) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^\s)]+\))/g;
  let offset = 0;
  for (const match of value.matchAll(pattern)) {
    element.append(document.createTextNode(value.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      element.append(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      element.append(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      element.append(emphasis);
    } else {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      const link = document.createElement("a");
      link.textContent = linkMatch[1];
      const href = safeMarkdownHref(linkMatch[2]);
      if (href) {
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
      element.append(link);
    }
    offset = match.index + token.length;
  }
  element.append(document.createTextNode(value.slice(offset)));
}

function safeMarkdownHref(value) {
  try {
    const url = new URL(value, globalThis.location.href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
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
  state.dirty = true;
  ui.saveState.textContent = "Unsaved";
  persistDraft();
}

function persistDraft() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        title: state.title,
        runtime: state.runtime,
        cells: state.cells.map(({ kind, source, hidden }) => ({ kind, source, hidden })),
      }),
    );
  } catch {
    ui.saveState.textContent = "Unsaved locally";
  }
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
      stdout: "",
      result: null,
      error: null,
      diagnostic: null,
      hidden: cell.hidden === true,
    }));
    state.activeCellId = state.cells[0]?.id || null;
    state.dirty = true;
  } catch {
    localStorage.removeItem(STORAGE_KEY);
  }
}

async function fetchJson(url, options = {}) {
  const response = await apiFetch(url, options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiToken) {
    headers.set("Authorization", `Bearer ${apiToken}`);
  }
  return fetch(url, { ...options, headers });
}

async function fetchTerminalStream(url, options, cell) {
  const response = await apiFetch(url, options);
  if (!response.ok) {
    const payload = await response.json();
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
  let result = null;
  await consumeNdjson(response, (event) => {
    if (event.type === "output") {
      cell.stdout = appendBoundedOutput(cell.stdout, event.text || "");
      updateLiveOutput(cell);
    } else if (event.type === "result") {
      result = event;
    }
  });

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

async function consumeNdjson(response, onEvent) {
  if (!response.body) {
    throw new Error("Response stream is unavailable");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (line) => {
    if (line.trim()) {
      onEvent(JSON.parse(line));
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
      return;
    }
  }
}

function updateLiveOutput(cell) {
  if (liveOutputFrame !== null) {
    return;
  }
  liveOutputFrame = requestAnimationFrame(() => {
    liveOutputFrame = null;
    const output = document.querySelector(
      `[data-cell-id="${cell.id}"] [data-live-output]`,
    );
    if (output) {
      output.textContent = cell.stdout;
      output.scrollTop = output.scrollHeight;
    }
  });
}

function appendBoundedOutput(current, addition) {
  if (!addition || current.endsWith(OUTPUT_TRUNCATION_NOTICE)) {
    return current;
  }
  const remaining = MAX_OUTPUT_CHARS - current.length;
  if (remaining <= 0) {
    return current + OUTPUT_TRUNCATION_NOTICE;
  }
  if (addition.length <= remaining) {
    return current + addition;
  }
  return current + addition.slice(0, remaining) + OUTPUT_TRUNCATION_NOTICE;
}

function disposeEditors() {
  for (const disposable of editorResizeDisposables.values()) {
    disposable?.dispose?.();
  }
  editorResizeDisposables.clear();
  for (const editor of kediEditors.values()) {
    const model = editor.getModel?.();
    editor.dispose?.();
    model?.dispose?.();
  }
  kediEditors.clear();
  textEditors.clear();
}

function focusActiveEditor() {
  const cellId = state.activeCellId;
  if (!cellId) {
    return;
  }
  (kediEditors.get(cellId) || textEditors.get(cellId))?.focus();
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
  const modifier = event.metaKey || event.ctrlKey;
  if (modifier && event.key.toLowerCase() === "s") {
    event.preventDefault();
    openSaveDialog();
    return;
  }
  if (!modifier && event.key === "Enter" && event.shiftKey && state.activeCellId) {
    event.preventDefault();
    void runCell(state.activeCellId);
    return;
  }
  if (!modifier || !state.activeCellId || state.runningCellId) {
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    addCellFromControl(event.shiftKey);
  } else if (event.key === "Backspace") {
    event.preventDefault();
    deleteCell(state.activeCellId);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    moveCell(state.activeCellId, -1);
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    moveCell(state.activeCellId, 1);
  }
});
