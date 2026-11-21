import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { Parser, Language, Query } from "../src/kedi_notebook/static/vendor/web-tree-sitter.js";
import { testing } from "../src/kedi_notebook/static/tree-sitter-highlighter.js";

const staticRoot = new URL("../src/kedi_notebook/static/", import.meta.url);

async function loadResources() {
  await Parser.init({
    locateFile: () =>
      fileURLToPath(new URL("vendor/web-tree-sitter.wasm", staticRoot)),
  });
  const kediLanguage = await Language.load(
    fileURLToPath(new URL("grammars/kedi/tree-sitter-kedi.wasm", staticRoot)),
  );
  const pythonLanguage = await Language.load(
    fileURLToPath(new URL("grammars/python/tree-sitter-python.wasm", staticRoot)),
  );
  const kediHighlights = await readFile(
    new URL("grammars/kedi/highlights.scm", staticRoot),
    "utf8",
  );
  const kediInjections = await readFile(
    new URL("grammars/kedi/injections.scm", staticRoot),
    "utf8",
  );
  const pythonHighlights = await readFile(
    new URL("grammars/python/highlights.scm", staticRoot),
    "utf8",
  );
  return {
    Parser,
    kediLanguage,
    kediQuery: new Query(kediLanguage, kediHighlights),
    pythonLanguage,
    pythonQuery: new Query(pythonLanguage, pythonHighlights),
    injectionQuery: new Query(kediLanguage, kediInjections),
  };
}

test("disposing a highlighter frees its parsers and tree", async () => {
  const resources = await loadResources();
  const highlighter = new testing.KediTreeSitterHighlighter(resources);
  const provided = highlighter.provide("> show: `1 + 1`\n", null);
  assert.ok(provided.data.length > 0);
  assert.ok(highlighter.tree !== null);

  const parser = highlighter.parser;
  const pythonParser = highlighter.pythonParser;
  highlighter.dispose();
  assert.equal(highlighter.tree, null);
  assert.equal(highlighter.snapshots.size, 0);
  // A deleted web-tree-sitter parser throws on parse: the WASM memory is
  // gone, so the highlighter cannot be used again after dispose.
  assert.throws(() => parser.parse("> show: `1`\n"));
  assert.throws(() => pythonParser.parse("x = 1\n"));
  // Disposing twice is a no-op.
  highlighter.dispose();
});

test("disposing one highlighter leaves the shared queries usable", async () => {
  const resources = await loadResources();
  const first = new testing.KediTreeSitterHighlighter(resources);
  first.provide("> show: `1`\n", null);
  first.dispose();

  const second = new testing.KediTreeSitterHighlighter(resources);
  const provided = second.provide("> show: `2`\n", null);
  assert.ok(provided.data.length > 0);
  second.dispose();
});
