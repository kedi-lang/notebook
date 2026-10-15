import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { Parser, Language, Query } from "../src/kedi_notebook/static/vendor/web-tree-sitter.js";

const staticRoot = new URL("../src/kedi_notebook/static/", import.meta.url);

test("shipped WASM and query support capabilities and deferred Python expressions", async () => {
  await Parser.init({
    locateFile: () => fileURLToPath(new URL("vendor/web-tree-sitter.wasm", staticRoot)),
  });
  const language = await Language.load(
    fileURLToPath(new URL("grammars/kedi/tree-sitter-kedi.wasm", staticRoot)),
  );
  const parser = new Parser();
  parser.setLanguage(language);
  const query = new Query(
    language,
    await readFile(new URL("grammars/kedi/highlights.scm", staticRoot), "utf8"),
  );
  const tree = parser.parse(
    "> capability: search:\n" +
    "  > instructions: Cite the evidence.\n" +
    "  > defer: `on_demand is not None`\n" +
    "> use: search\n",
  );
  try {
    assert.equal(tree.rootNode.hasError, false);
    const captures = query.captures(tree.rootNode);
    const keywords = captures.filter((item) => item.name === "keyword").map((item) => item.node.text);
    assert.ok(keywords.includes("capability"));
    assert.ok(keywords.includes("defer"));
    assert.ok(captures.some((item) => item.name === "label" && item.node.text === "search"));
  } finally {
    tree.delete();
    query.delete();
    parser.delete();
  }
});
