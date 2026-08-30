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
const diagnosticTimers = new WeakMap();
const treeSitterResources = preloadKediTreeSitterResources();

export async function createKediEditor(element, value, onChange, options = {}) {
  let monaco;
  try {
    [monaco] = await Promise.all([loadMonaco(), treeSitterResources]);
  } catch {
    return createFallbackEditor(element, value, onChange, options);
  }
  await registerKediLanguage(monaco);
  defineKediThemes(monaco);
  const editor = monaco.editor.create(element, {
    value,
    language: "kedi",
    theme: "kedi-dark",
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
    scheduleDiagnostics(monaco, editor.getModel());
  });
  scheduleDiagnostics(monaco, editor.getModel());
  return editor;
}

function createFallbackEditor(element, value, onChange, options) {
  const textarea = document.createElement("textarea");
  textarea.className = "kedi-fallback-editor";
  textarea.value = value;
  textarea.readOnly = Boolean(options.editor?.readOnly);
  textarea.spellcheck = false;
  element.replaceChildren(textarea);
  const listeners = new Set();
  const resize = () => {
    textarea.style.height = "0";
    textarea.style.height = `${Math.max(52, Math.min(520, textarea.scrollHeight))}px`;
    for (const listener of listeners) {
      listener();
    }
  };
  textarea.addEventListener("input", () => {
    onChange(textarea.value);
    resize();
  });
  resize();
  return {
    dispose() {
      listeners.clear();
      textarea.remove();
    },
    focus: () => textarea.focus(),
    getContentHeight: () => textarea.scrollHeight,
    getModel: () => null,
    getValue: () => textarea.value,
    layout: resize,
    updateOptions(nextOptions) {
      if ("readOnly" in nextOptions) {
        textarea.readOnly = Boolean(nextOptions.readOnly);
      }
    },
    onDidContentSizeChange(listener) {
      listeners.add(listener);
      return { dispose: () => listeners.delete(listener) };
    },
    revealLineInCenterIfOutsideViewport() {},
  };
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
      if (model.isDisposed()) {
        return null;
      }
      const version = model.getVersionId();
      let highlighterPromise = highlighters.get(model);
      if (!highlighterPromise) {
        highlighterPromise = createKediTreeSitterHighlighter();
        highlighters.set(model, highlighterPromise);
      }
      const highlighter = await highlighterPromise;
      if (
        cancellationToken.isCancellationRequested ||
        model.isDisposed() ||
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
      if (model.isDisposed()) {
        return null;
      }
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
        model.isDisposed() ||
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
  monaco.languages.registerCompletionItemProvider("kedi", {
    triggerCharacters: [">", "<", "[", ":", "."],
    async provideCompletionItems(model, position, completionContext, cancellationToken) {
      if (model.isDisposed()) {
        return { suggestions: [] };
      }
      const version = model.getVersionId();
      const context = lspContext(model);
      const result = await fetchJson("/api/lsp/completion", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(positionPayload(context, position)),
      });
      if (
        cancellationToken.isCancellationRequested ||
        model.isDisposed() ||
        version !== model.getVersionId()
      ) {
        return { suggestions: [] };
      }
      return {
        suggestions: result.items.flatMap((item) => {
          const range = item.textEdit?.range
            ? lspRangeToMonaco(monaco, item.textEdit.range, context.lineOffset)
            : undefined;
          if (range && !rangeInsideModel(model, range)) {
            return [];
          }
          const additionalTextEdits = (item.additionalTextEdits || []).flatMap((edit) => {
            const editRange = lspRangeToMonaco(monaco, edit.range, context.lineOffset);
            return editRange && rangeInsideModel(model, editRange)
              ? [{ range: editRange, text: edit.newText }]
              : [];
          });
          return [{
            label: item.label,
            kind: completionKind(monaco, item.kind),
            detail: item.detail,
            documentation: completionDocumentation(item.documentation),
            insertText: item.textEdit?.newText || item.insertText || item.label,
            insertTextRules:
              item.insertTextFormat === 2
                ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
                : undefined,
            range,
            sortText: item.sortText,
            filterText: item.filterText,
            additionalTextEdits,
          }];
        }),
      };
    },
  });
  monaco.languages.registerSignatureHelpProvider("kedi", {
    signatureHelpTriggerCharacters: ["(", ","],
    async provideSignatureHelp(model, position, cancellationToken) {
      if (model.isDisposed()) {
        return null;
      }
      const version = model.getVersionId();
      const context = lspContext(model);
      const result = await fetchJson("/api/lsp/signature", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(positionPayload(context, position)),
      });
      if (
        cancellationToken.isCancellationRequested ||
        model.isDisposed() ||
        version !== model.getVersionId() ||
        !result.signature
      ) {
        return null;
      }
      return { value: result.signature, dispose() {} };
    },
  });
  monaco.languages.registerDefinitionProvider("kedi", {
    async provideDefinition(model, position, cancellationToken) {
      if (model.isDisposed()) {
        return null;
      }
      const version = model.getVersionId();
      const context = lspContext(model);
      const result = await fetchJson("/api/lsp/definition", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(positionPayload(context, position)),
      });
      if (
        cancellationToken.isCancellationRequested ||
        model.isDisposed() ||
        version !== model.getVersionId() ||
        !result.definition
      ) {
        return null;
      }
      const range = lspRangeToMonaco(
        monaco,
        result.definition.range,
        context.lineOffset,
      );
      if (!range || range.startLineNumber < 1 || range.endLineNumber > model.getLineCount()) {
        return null;
      }
      return { uri: model.uri, range };
    },
  });
  monaco.languages.registerReferenceProvider("kedi", {
    async provideReferences(model, position, referenceContext, cancellationToken) {
      if (model.isDisposed()) {
        return [];
      }
      const version = model.getVersionId();
      const context = lspContext(model);
      const result = await fetchJson("/api/lsp/references", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...positionPayload(context, position),
          includeDeclaration: referenceContext.includeDeclaration,
        }),
      });
      if (
        cancellationToken.isCancellationRequested ||
        model.isDisposed() ||
        version !== model.getVersionId()
      ) {
        return [];
      }
      return result.references.flatMap((item) => {
        const range = lspRangeToMonaco(monaco, item.range, context.lineOffset);
        return range && rangeInsideModel(model, range) ? [{ uri: model.uri, range }] : [];
      });
    },
  });
  monaco.languages.registerRenameProvider("kedi", {
    async resolveRenameLocation(model, position, cancellationToken) {
      const context = lspContext(model);
      const result = await fetchJson("/api/lsp/prepare-rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(positionPayload(context, position)),
      });
      if (cancellationToken.isCancellationRequested || !result.rename) {
        return { rejectReason: "This symbol cannot be renamed" };
      }
      const range = lspRangeToMonaco(
        monaco,
        result.rename.range,
        context.lineOffset,
      );
      if (!range || !rangeInsideModel(model, range)) {
        return { rejectReason: "Rename target belongs to an earlier notebook cell" };
      }
      return { range, text: model.getValueInRange(range) };
    },
    async provideRenameEdits(model, position, newName, cancellationToken) {
      const context = lspContext(model);
      const result = await fetchJson("/api/lsp/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...positionPayload(context, position),
          newName,
        }),
      });
      if (cancellationToken.isCancellationRequested) {
        return { edits: [] };
      }
      const edits = [];
      for (const item of result.edits) {
        const range = lspRangeToMonaco(monaco, item.range, context.lineOffset);
        if (!range || !rangeInsideModel(model, range)) {
          return {
            edits: [],
            rejectReason: "Rename spans an earlier notebook cell; rename it from that cell",
          };
        }
        edits.push({
          resource: model.uri,
          textEdit: { range, text: item.newText },
          versionId: model.getVersionId(),
        });
      }
      return { edits };
    },
  });
  languageRegistered = true;
}

function lspContext(model) {
  return lspContexts.get(model)?.() ?? { source: model.getValue(), lineOffset: 0 };
}

function positionPayload(context, position) {
  return {
    source: context.source,
    line: position.lineNumber - 1 + context.lineOffset,
    character: position.column - 1,
  };
}

function scheduleDiagnostics(monaco, model) {
  if (!model) {
    return;
  }
  clearTimeout(diagnosticTimers.get(model));
  diagnosticTimers.set(
    model,
    setTimeout(async () => {
      if (model.isDisposed()) {
        return;
      }
      const version = model.getVersionId();
      const context = lspContext(model);
      try {
        const result = await fetchJson("/api/lsp/diagnostics", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source: context.source }),
        });
        if (model.isDisposed() || version !== model.getVersionId()) {
          return;
        }
        const severity = {
          1: monaco.MarkerSeverity.Error,
          2: monaco.MarkerSeverity.Warning,
          3: monaco.MarkerSeverity.Info,
          4: monaco.MarkerSeverity.Hint,
        };
        const markers = result.diagnostics.flatMap((diagnostic) => {
          const range = lspRangeToMonaco(monaco, diagnostic.range, context.lineOffset);
          if (
            !range ||
            range.startLineNumber < 1 ||
            range.endLineNumber > model.getLineCount()
          ) {
            return [];
          }
          return [{
            startLineNumber: range.startLineNumber,
            startColumn: range.startColumn,
            endLineNumber: range.endLineNumber,
            endColumn: range.endColumn,
            severity: severity[diagnostic.severity] || monaco.MarkerSeverity.Error,
            message: diagnostic.message,
            code: diagnostic.code,
            source: diagnostic.source || "kedi",
          }];
        });
        monaco.editor.setModelMarkers(model, "kedi-lsp", markers);
      } catch {
        if (!model.isDisposed() && version === model.getVersionId()) {
          monaco.editor.setModelMarkers(model, "kedi-lsp", []);
        }
      }
    }, 250),
  );
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

function rangeInsideModel(model, range) {
  return (
    range.startLineNumber >= 1 &&
    range.endLineNumber <= model.getLineCount() &&
    range.startColumn >= 1 &&
    range.endColumn <= model.getLineMaxColumn(range.endLineNumber)
  );
}

function completionKind(monaco, kind) {
  const mapping = {
    1: "Text",
    2: "Method",
    3: "Function",
    4: "Constructor",
    5: "Field",
    6: "Variable",
    7: "Class",
    8: "Interface",
    9: "Module",
    10: "Property",
    11: "Unit",
    12: "Value",
    13: "Enum",
    14: "Keyword",
    15: "Snippet",
    16: "Color",
    17: "File",
    18: "Reference",
    19: "Folder",
    20: "EnumMember",
    21: "Constant",
    22: "Struct",
    23: "Event",
    24: "Operator",
    25: "TypeParameter",
  };
  return monaco.languages.CompletionItemKind[mapping[kind] || "Text"];
}

function completionDocumentation(value) {
  if (!value) {
    return undefined;
  }
  return typeof value === "string" ? value : { value: value.value || "" };
}

async function fetchJson(url, options) {
  const headers = new Headers(options?.headers || {});
  const token = globalThis.__KEDI_NOTEBOOK_API_TOKEN;
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(url, { ...options, headers });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status} from ${url}`);
  }
  return payload;
}
