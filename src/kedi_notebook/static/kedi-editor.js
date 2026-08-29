import {
  KEDI_SEMANTIC_TOKEN_MODIFIERS,
  KEDI_SEMANTIC_TOKEN_TYPES,
  createKediTreeSitterHighlighter,
  preloadKediTreeSitterResources,
} from "./tree-sitter-highlighter.js";

const MONACO_BASE =
  "https://cdn.jsdelivr.net/npm/monaco-editor@0.55.1/min";

let monacoPromise;
let languageRegistered = false;
let workerUrl;
const executionDecorations = new WeakMap();
const tipDecorations = new WeakMap();
const highlighters = new WeakMap();
const lspContexts = new WeakMap();
const treeSitterResources = preloadKediTreeSitterResources();

export async function createKediEditor(element, value, onChange, options = {}) {
  const [monaco] = await Promise.all([loadMonaco(), treeSitterResources]);
  await registerKediLanguage(monaco);
  defineKediThemes(monaco);
  const theme = options.theme === "light" ? "kedi-light" : "kedi-dark";
  const editor = monaco.editor.create(element, {
    value,
    language: "kedi",
    theme,
    automaticLayout: true,
    fontFamily: '"IBM Plex Mono", monospace',
    fontSize: 13.5,
    lineHeight: 22,
    letterSpacing: 0,
    padding: { top: 14, bottom: 14 },
    tabSize: 2,
    insertSpaces: true,
    minimap: { enabled: false },
    overviewRulerLanes: 0,
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    wordWrap: "on",
    renderWhitespace: "selection",
    renderLineHighlight: "line",
    fixedOverflowWidgets: true,
    glyphMargin: true,
    "semanticHighlighting.enabled": true,
    ...options.editor,
  });
  if (typeof options.lspSource === "function") {
    lspContexts.set(editor.getModel(), options.lspSource);
  }
  editor.onDidChangeModelContent(() => {
    clearKediExecutionDiagnostic(editor);
    onChange(editor.getValue());
  });
  return editor;
}

export function setKediEditorTheme(theme) {
  if (!globalThis.monaco) {
    return;
  }
  defineKediThemes(globalThis.monaco);
  globalThis.monaco.editor.setTheme(theme === "light" ? "kedi-light" : "kedi-dark");
}

function defineKediThemes(monaco) {
  monaco.editor.defineTheme("kedi-dark", {
    base: "vs-dark",
    inherit: true,
    semanticHighlighting: true,
    rules: [
      { token: "comment", foreground: "6B7280", fontStyle: "italic" },
      { token: "string", foreground: "A6E3A1" },
      { token: "number", foreground: "F5C2A7" },
      { token: "keyword", foreground: "CBA6F7" },
      { token: "operator", foreground: "89DCEB" },
      { token: "type", foreground: "F9E2AF" },
      { token: "entity.name.type", foreground: "F9E2AF" },
      { token: "class", foreground: "F9E2AF" },
      { token: "entity.name.type.class", foreground: "F9E2AF" },
      { token: "function", foreground: "89B4FA" },
      { token: "entity.name.function", foreground: "89B4FA" },
      { token: "method", foreground: "89B4FA" },
      { token: "entity.name.function.member", foreground: "89B4FA" },
      { token: "parameter", foreground: "F38BA8" },
      { token: "variable.parameter", foreground: "F38BA8" },
      { token: "variable", foreground: "FAB387" },
      { token: "variable.other.readwrite", foreground: "FAB387" },
      { token: "property", foreground: "94E2D5" },
      { token: "variable.other.property", foreground: "94E2D5" },
      { token: "namespace", foreground: "74C7EC" },
      { token: "entity.name.namespace", foreground: "74C7EC" },
      { token: "decorator", foreground: "CBA6F7" },
    ],
    colors: {
      "editor.background": "#151821",
      "editor.foreground": "#CDD6F4",
      "editorLineNumber.foreground": "#4F566B",
      "editorLineNumber.activeForeground": "#A6ADC8",
      "editorCursor.foreground": "#89DCEB",
      "editor.lineHighlightBackground": "#1B1F2A",
      "editor.selectionBackground": "#3A506F",
      "editor.inactiveSelectionBackground": "#29384E",
      "editorIndentGuide.background1": "#252A38",
      "editorIndentGuide.activeBackground1": "#454D63",
      "editorBracketMatch.background": "#31384A",
      "editorBracketMatch.border": "#89DCEB",
    },
  });
  monaco.editor.defineTheme("kedi-light", {
    base: "vs",
    inherit: true,
    semanticHighlighting: true,
    rules: [
      { token: "comment", foreground: "697386", fontStyle: "italic" },
      { token: "string", foreground: "267A3A" },
      { token: "number", foreground: "A34A28" },
      { token: "keyword", foreground: "7B3FB2" },
      { token: "operator", foreground: "136B78" },
      { token: "type", foreground: "8A5A00" },
      { token: "class", foreground: "8A5A00" },
      { token: "function", foreground: "195BAA" },
      { token: "method", foreground: "195BAA" },
      { token: "parameter", foreground: "A72B4A" },
      { token: "variable", foreground: "9A4E12" },
      { token: "property", foreground: "167064" },
      { token: "namespace", foreground: "176F9B" },
      { token: "decorator", foreground: "7B3FB2" },
    ],
    colors: {
      "editor.background": "#FFFFFF",
      "editor.foreground": "#20242B",
      "editorLineNumber.foreground": "#A0A7B1",
      "editorLineNumber.activeForeground": "#586170",
      "editorCursor.foreground": "#176F9B",
      "editor.lineHighlightBackground": "#F5F7F9",
      "editor.selectionBackground": "#CDE0F5",
      "editor.inactiveSelectionBackground": "#E1EAF3",
      "editorIndentGuide.background1": "#E8EBEF",
      "editorIndentGuide.activeBackground1": "#B7BEC8",
      "editorBracketMatch.background": "#E7F2F3",
      "editorBracketMatch.border": "#176F9B",
    },
  });
}

export function setKediExecutionDiagnostic(editor, diagnostic) {
  clearKediExecutionDiagnostic(editor);
  if (!diagnostic) {
    return;
  }

  const monaco = globalThis.monaco;
  const model = editor.getModel();
  if (!monaco || !model) {
    return;
  }
  const line = Math.min(
    Math.max(Number(diagnostic.line) || 1, 1),
    model.getLineCount(),
  );
  const maxColumn = model.getLineMaxColumn(line);
  const column = Math.min(
    Math.max(Number(diagnostic.column) || 1, 1),
    maxColumn,
  );
  const message = diagnostic.message || "Kedi execution failed";

  monaco.editor.setModelMarkers(model, "kedi-runtime", [
    {
      severity: monaco.MarkerSeverity.Error,
      message,
      source: "Kedi runtime",
      startLineNumber: line,
      startColumn: column,
      endLineNumber: line,
      endColumn: maxColumn,
    },
  ]);
  const decorations =
    executionDecorations.get(editor) ?? editor.createDecorationsCollection();
  executionDecorations.set(editor, decorations);
  decorations.set([
    {
      range: new monaco.Range(line, 1, line, maxColumn),
      options: {
        isWholeLine: true,
        className: "kedi-runtime-error-line",
        glyphMarginClassName: "kedi-runtime-error-glyph",
        glyphMarginHoverMessage: { value: message },
        overviewRuler: {
          color: "#f38ba8",
          position: monaco.editor.OverviewRulerLane.Right,
        },
      },
    },
  ]);
  editor.revealLineInCenterIfOutsideViewport(line);
}

export function clearKediExecutionDiagnostic(editor) {
  const model = editor.getModel();
  if (model && globalThis.monaco) {
    globalThis.monaco.editor.setModelMarkers(model, "kedi-runtime", []);
  }
  executionDecorations.get(editor)?.clear();
}

export function setKediTips(editor, tips) {
  const monaco = globalThis.monaco;
  const model = editor.getModel();
  if (!monaco || !model) {
    return;
  }
  const markers = [];
  const decorations = [];
  for (const tip of tips) {
    const line = Math.min(Math.max(Number(tip.line) || 1, 1), model.getLineCount());
    const maxColumn = model.getLineMaxColumn(line);
    const column = Math.min(Math.max(Number(tip.column) || 1, 1), maxColumn);
    const message = tip.message || tip.title || "Kedi tip";
    markers.push({
      severity: monaco.MarkerSeverity.Hint,
      message,
      source: "Kedi tip",
      startLineNumber: line,
      startColumn: column,
      endLineNumber: line,
      endColumn: Math.min(column + 1, maxColumn),
    });
    decorations.push({
      range: new monaco.Range(line, 1, line, maxColumn),
      options: {
        isWholeLine: false,
        glyphMarginClassName: "kedi-tip-glyph",
        glyphMarginHoverMessage: { value: message },
        overviewRuler: {
          color: "#41b6e6",
          position: monaco.editor.OverviewRulerLane.Right,
        },
      },
    });
  }
  monaco.editor.setModelMarkers(model, "kedi-tips", markers);
  const collection = tipDecorations.get(editor) ?? editor.createDecorationsCollection();
  tipDecorations.set(editor, collection);
  collection.set(decorations);
}

export function clearKediTips(editor) {
  const model = editor.getModel();
  if (model && globalThis.monaco) {
    globalThis.monaco.editor.setModelMarkers(model, "kedi-tips", []);
  }
  tipDecorations.get(editor)?.clear();
}

async function loadMonaco() {
  if (monacoPromise) {
    return monacoPromise;
  }
  monacoPromise = new Promise((resolve, reject) => {
    const amdRequire = globalThis.require;
    if (!amdRequire?.config) {
      reject(new Error("Monaco AMD loader is unavailable"));
      return;
    }
    globalThis.MonacoEnvironment = {
      getWorkerUrl() {
        if (!workerUrl) {
          const source = [
            `self.MonacoEnvironment = { baseUrl: "${MONACO_BASE}/" };`,
            `importScripts("${MONACO_BASE}/vs/base/worker/workerMain.js");`,
          ].join("\n");
          workerUrl = URL.createObjectURL(
            new Blob([source], { type: "text/javascript" }),
          );
        }
        return workerUrl;
      },
    };
    amdRequire.config({ paths: { vs: `${MONACO_BASE}/vs` } });
    amdRequire(["vs/editor/editor.main"], () => resolve(globalThis.monaco), reject);
  });
  return monacoPromise;
}

async function registerKediLanguage(monaco) {
  if (languageRegistered) {
    return;
  }
  monaco.languages.register({ id: "kedi", extensions: [".kedi"] });
  monaco.languages.registerDocumentSemanticTokensProvider("kedi", {
    getLegend() {
      return {
        tokenTypes: KEDI_SEMANTIC_TOKEN_TYPES,
        tokenModifiers: KEDI_SEMANTIC_TOKEN_MODIFIERS,
      };
    },
    async provideDocumentSemanticTokens(model, lastResultId, cancellationToken) {
      const version = model.getVersionId();
      let highlighterPromise = highlighters.get(model);
      if (!highlighterPromise) {
        highlighterPromise = createKediTreeSitterHighlighter();
        highlighters.set(model, highlighterPromise);
      }
      const highlighter = await highlighterPromise;
      if (
        cancellationToken.isCancellationRequested ||
        version !== model.getVersionId()
      ) {
        return null;
      }
      return highlighter.provide(model.getValue(), lastResultId);
    },
    releaseDocumentSemanticTokens() {},
  });
  monaco.languages.registerHoverProvider("kedi", {
    async provideHover(model, position, cancellationToken) {
      const version = model.getVersionId();
      const context = lspContexts.get(model)?.() ?? {
        source: model.getValue(),
        lineOffset: 0,
      };
      const result = await fetchJson("/api/lsp/hover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: context.source,
          line: position.lineNumber - 1 + context.lineOffset,
          character: position.column - 1,
        }),
      });
      if (
        cancellationToken.isCancellationRequested ||
        version !== model.getVersionId() ||
        !result.hover
      ) {
        return null;
      }
      return {
        contents: [{ value: result.hover.contents.value }],
        range: lspRangeToMonaco(monaco, result.hover.range, context.lineOffset),
      };
    },
  });
  languageRegistered = true;
}

function lspRangeToMonaco(monaco, range, lineOffset = 0) {
  if (!range) {
    return undefined;
  }
  return new monaco.Range(
    range.start.line + 1 - lineOffset,
    range.start.character + 1,
    range.end.line + 1 - lineOffset,
    range.end.character + 1,
  );
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status} from ${url}`);
  }
  return payload;
}
