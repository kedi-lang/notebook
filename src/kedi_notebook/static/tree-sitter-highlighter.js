const TREE_SITTER_MODULE = new URL("./vendor/web-tree-sitter.js", import.meta.url);
const TREE_SITTER_RUNTIME = new URL("./vendor/web-tree-sitter.wasm", import.meta.url);
const KEDI_GRAMMAR = new URL(
  "./grammars/kedi/tree-sitter-kedi.wasm",
  import.meta.url,
);
const KEDI_HIGHLIGHTS = new URL(
  "./grammars/kedi/highlights.scm",
  import.meta.url,
);
const KEDI_INJECTIONS = new URL(
  "./grammars/kedi/injections.scm",
  import.meta.url,
);
const PYTHON_GRAMMAR = new URL(
  "./grammars/python/tree-sitter-python.wasm",
  import.meta.url,
);
const PYTHON_HIGHLIGHTS = new URL(
  "./grammars/python/highlights.scm",
  import.meta.url,
);
const PYTHON_PRIORITY_OFFSET = 10_000;

export const KEDI_SEMANTIC_TOKEN_TYPES = [
  "namespace",
  "type",
  "class",
  "enum",
  "interface",
  "struct",
  "typeParameter",
  "parameter",
  "variable",
  "property",
  "enumMember",
  "event",
  "function",
  "method",
  "macro",
  "keyword",
  "modifier",
  "comment",
  "string",
  "number",
  "regexp",
  "operator",
  "decorator",
];

export const KEDI_SEMANTIC_TOKEN_MODIFIERS = [];

const CAPTURE_TOKEN_TYPES = new Map([
  ["comment.block", "comment"],
  ["comment.line", "comment"],
  ["comment", "comment"],
  ["constant", "variable"],
  ["constant.builtin", "variable"],
  ["constructor", "class"],
  ["embedded", "variable"],
  ["escape", "string"],
  ["eval", "keyword"],
  ["function", "function"],
  ["function.builtin", "function"],
  ["function.call", "function"],
  ["function.method", "method"],
  ["keyword", "keyword"],
  ["label", "variable"],
  ["namespace", "namespace"],
  ["operator", "operator"],
  ["property", "property"],
  ["punctuation.delimiter", "operator"],
  ["punctuation.special", "operator"],
  ["string", "string"],
  ["string.special", "string"],
  ["test", "keyword"],
  ["type", "type"],
  ["type.definition", "type"],
  ["variable", "variable"],
  ["variable.builtin", "variable"],
  ["variable.parameter", "parameter"],
]);

let resourcesPromise;

export function preloadKediTreeSitterResources() {
  return loadResources();
}

export async function createKediTreeSitterHighlighter() {
  const resources = await loadResources();
  return new KediTreeSitterHighlighter(resources);
}

class KediTreeSitterHighlighter {
  constructor({
    Parser,
    kediLanguage,
    kediQuery,
    pythonLanguage,
    pythonQuery,
    injectionQuery,
  }) {
    this.parser = new Parser();
    this.parser.setLanguage(kediLanguage);
    this.query = kediQuery;
    this.pythonParser = new Parser();
    this.pythonParser.setLanguage(pythonLanguage);
    this.pythonQuery = pythonQuery;
    this.injectionQuery = injectionQuery;
    this.source = "";
    this.tree = null;
    this.tokens = new Uint32Array();
    this.resultId = null;
    this.revision = 0;
    this.snapshots = new Map();
  }

  provide(source, lastResultId) {
    if (this.tree === null || source !== this.source) {
      this.update(source);
    }
    if (lastResultId && this.snapshots.has(lastResultId)) {
      return tokenEdits(
        this.snapshots.get(lastResultId),
        this.tokens,
        this.resultId,
      );
    }
    return { data: this.tokens, resultId: this.resultId };
  }

  update(source) {
    const oldTree = this.tree;
    if (oldTree !== null) {
      oldTree.edit(minimalTreeEdit(this.source, source));
    }
    const newTree = this.parser.parse(source, oldTree);
    if (newTree === null) {
      throw new Error("Tree-sitter failed to parse the Kedi document");
    }
    this.tree = newTree;
    this.source = source;
    const spans = captureSpans(this.query.captures(newTree.rootNode), source);
    for (const capture of this.injectionQuery.captures(newTree.rootNode)) {
      spans.push(
        ...pythonInjectionSpans(
          capture.node,
          this.pythonParser,
          this.pythonQuery,
        ),
      );
    }
    this.tokens = encodeSemanticTokens(resolveOverlaps(spans));
    this.revision += 1;
    this.resultId = `tree-sitter-kedi:${this.revision}`;
    this.snapshots.set(this.resultId, this.tokens);
    while (this.snapshots.size > 4) {
      this.snapshots.delete(this.snapshots.keys().next().value);
    }
    oldTree?.delete();
  }
}

async function loadResources() {
  if (!resourcesPromise) {
    resourcesPromise = (async () => {
      const [
        { Parser, Language, Query },
        kediHighlights,
        kediInjections,
        pythonHighlights,
      ] = await Promise.all([
          import(TREE_SITTER_MODULE.href),
          fetchText(KEDI_HIGHLIGHTS),
          fetchText(KEDI_INJECTIONS),
          fetchText(PYTHON_HIGHLIGHTS),
        ]);
      await Parser.init({
        locateFile(filename) {
          return filename.endsWith(".wasm")
            ? TREE_SITTER_RUNTIME.href
            : new URL(`./vendor/${filename}`, import.meta.url).href;
        },
      });
      const [kediLanguage, pythonLanguage] = await Promise.all([
        Language.load(KEDI_GRAMMAR.href),
        Language.load(PYTHON_GRAMMAR.href),
      ]);
      return {
        Parser,
        kediLanguage,
        kediQuery: new Query(kediLanguage, kediHighlights),
        pythonLanguage,
        pythonQuery: new Query(pythonLanguage, pythonHighlights),
        injectionQuery: new Query(kediLanguage, kediInjections),
      };
    })();
  }
  return resourcesPromise;
}

async function fetchText(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} while loading ${url.pathname}`);
  }
  return response.text();
}

function captureSpans(
  captures,
  source,
  { mapPoint = (point) => point, priorityOffset = 0 } = {},
) {
  // web-tree-sitter and Monaco both use UTF-16 code-unit columns.
  const lines = source.split("\n");
  const tokenTypeIndexes = new Map(
    KEDI_SEMANTIC_TOKEN_TYPES.map((name, index) => [name, index]),
  );
  const spans = [];
  for (const capture of captures) {
    const tokenType = CAPTURE_TOKEN_TYPES.get(capture.name);
    const tokenTypeIndex = tokenTypeIndexes.get(tokenType);
    if (tokenTypeIndex === undefined) {
      continue;
    }
    const { startPosition, endPosition } = capture.node;
    for (let row = startPosition.row; row <= endPosition.row; row += 1) {
      const line = lines[row] ?? "";
      const localStart = {
        row,
        column: row === startPosition.row ? startPosition.column : 0,
      };
      const localEnd = {
        row,
        column: row === endPosition.row ? endPosition.column : line.length,
      };
      const absoluteStart = mapPoint(localStart);
      const absoluteEnd = mapPoint(localEnd);
      const start = absoluteStart.column;
      const end = absoluteEnd.column;
      if (end > start) {
        spans.push({
          line: absoluteStart.row,
          start,
          end,
          tokenTypeIndex,
          priority: priorityOffset + capture.patternIndex,
        });
      }
    }
  }
  return spans;
}

function pythonInjectionSpans(node, parser, query) {
  const region = normalizePythonRegion(node.text);
  const tree = parser.parse(region.source);
  if (tree === null) {
    return [];
  }
  const spans = captureSpans(query.captures(tree.rootNode), region.source, {
    mapPoint(point) {
      return {
        row: node.startPosition.row + point.row,
        column:
          (point.row === 0 ? node.startPosition.column : 0) +
          (region.removedColumns[point.row] ?? 0) +
          point.column,
      };
    },
    priorityOffset: PYTHON_PRIORITY_OFFSET,
  });
  tree.delete();
  return spans;
}

function normalizePythonRegion(source) {
  const lines = source.split("\n");
  const indents = lines
    .filter((line) => line.trim().length > 0)
    .map((line) => line.match(/^[\t ]*/u)?.[0].length ?? 0);
  const commonIndent = indents.length ? Math.min(...indents) : 0;
  const removedColumns = lines.map((line) =>
    Math.min(commonIndent, line.match(/^[\t ]*/u)?.[0].length ?? 0),
  );
  return {
    source: lines
      .map((line, index) => line.slice(removedColumns[index]))
      .join("\n"),
    removedColumns,
  };
}

function encodeSemanticTokens(resolved) {
  const data = [];
  let previousLine = 0;
  let previousStart = 0;
  for (const span of resolved) {
    const deltaLine = span.line - previousLine;
    const deltaStart = deltaLine === 0 ? span.start - previousStart : span.start;
    data.push(
      deltaLine,
      deltaStart,
      span.end - span.start,
      span.tokenTypeIndex,
      0,
    );
    previousLine = span.line;
    previousStart = span.start;
  }
  return Uint32Array.from(data);
}

function resolveOverlaps(spans) {
  const byLine = new Map();
  for (const span of spans) {
    const lineSpans = byLine.get(span.line) ?? [];
    lineSpans.push(span);
    byLine.set(span.line, lineSpans);
  }

  const resolved = [];
  for (const [line, lineSpans] of [...byLine.entries()].sort(
    ([left], [right]) => left - right,
  )) {
    const boundaries = [
      ...new Set(lineSpans.flatMap((span) => [span.start, span.end])),
    ].sort((left, right) => left - right);
    for (let index = 0; index < boundaries.length - 1; index += 1) {
      const start = boundaries[index];
      const end = boundaries[index + 1];
      const winner = lineSpans
        .filter((span) => span.start <= start && span.end >= end)
        .sort(
          (left, right) =>
            right.priority - left.priority ||
            left.end - left.start - (right.end - right.start),
        )[0];
      if (!winner) {
        continue;
      }
      const previous = resolved.at(-1);
      if (
        previous?.line === line &&
        previous.end === start &&
        previous.tokenTypeIndex === winner.tokenTypeIndex
      ) {
        previous.end = end;
      } else {
        resolved.push({
          line,
          start,
          end,
          tokenTypeIndex: winner.tokenTypeIndex,
        });
      }
    }
  }
  return resolved;
}

function tokenEdits(previous, current, resultId) {
  if (arraysEqual(previous, current)) {
    return { edits: [], resultId };
  }
  let prefix = 0;
  const maxPrefix = Math.min(previous.length, current.length);
  while (prefix < maxPrefix && previous[prefix] === current[prefix]) {
    prefix += 1;
  }
  prefix -= prefix % 5;
  return {
    edits: [
      {
        start: prefix,
        deleteCount: previous.length - prefix,
        data: current.slice(prefix),
      },
    ],
    resultId,
  };
}

function arraysEqual(left, right) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function minimalTreeEdit(previous, current) {
  let start = commonPrefixLength(previous, current);
  let oldEnd = previous.length;
  let newEnd = current.length;
  while (
    oldEnd > start &&
    newEnd > start &&
    previous.charCodeAt(oldEnd - 1) === current.charCodeAt(newEnd - 1)
  ) {
    oldEnd -= 1;
    newEnd -= 1;
  }
  start = safeUtf16Boundary(previous, start);
  oldEnd = safeUtf16Boundary(previous, oldEnd);
  newEnd = safeUtf16Boundary(current, newEnd);
  return {
    startIndex: start,
    oldEndIndex: oldEnd,
    newEndIndex: newEnd,
    startPosition: pointAt(previous, start),
    oldEndPosition: pointAt(previous, oldEnd),
    newEndPosition: pointAt(current, newEnd),
  };
}

function commonPrefixLength(left, right) {
  const limit = Math.min(left.length, right.length);
  let index = 0;
  while (index < limit && left.charCodeAt(index) === right.charCodeAt(index)) {
    index += 1;
  }
  return index;
}

function safeUtf16Boundary(text, index) {
  if (
    index > 0 &&
    index < text.length &&
    isHighSurrogate(text.charCodeAt(index - 1)) &&
    isLowSurrogate(text.charCodeAt(index))
  ) {
    return index - 1;
  }
  return index;
}

function isHighSurrogate(code) {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLowSurrogate(code) {
  return code >= 0xdc00 && code <= 0xdfff;
}

function pointAt(text, utf16Index) {
  const prefix = text.slice(0, utf16Index);
  const row = prefix.split("\n").length - 1;
  const lineStart = prefix.lastIndexOf("\n") + 1;
  return {
    row,
    column: prefix.length - lineStart,
  };
}

export const testing = {
  encodeSemanticTokens,
  minimalTreeEdit,
  tokenEdits,
};
