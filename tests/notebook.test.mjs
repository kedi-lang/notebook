import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { testing } from "../src/kedi_notebook/static/tree-sitter-highlighter.js";

// Audit #215 item 4: browser output over 64 KB killed the session because
// postOutput sent it in a single POST and the server rejects texts over
// 64_000 chars. notebook.js now splits output with chunkBridgeOutput and
// posts the chunks in order.
const notebookSource = await readFile(
  new URL("../src/kedi_notebook/static/notebook/notebook.js", import.meta.url),
  "utf8",
);

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} not found in notebook.js`);
  let depth = 0;
  let end = start;
  for (; end < source.length; end += 1) {
    if (source[end] === "{") depth += 1;
    if (source[end] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  assert.equal(depth, 0, `${name} has unbalanced braces`);
  return source.slice(start, end + 1);
}

const chunkBridgeOutput = new Function(
  "MAX_BRIDGE_OUTPUT_CHARS",
  `${extractFunction(notebookSource, "chunkBridgeOutput")}\nreturn chunkBridgeOutput;`,
)(64_000);

test("chunkBridgeOutput splits large output into <=64_000 char posts", () => {
  const text = "x".repeat(150_000);
  const chunks = chunkBridgeOutput(text);
  assert.equal(chunks.length, 3);
  assert.deepEqual(
    chunks.map((chunk) => chunk.length),
    [64_000, 64_000, 22_000],
  );
  assert.equal(chunks.join(""), text);
  assert.ok(chunks.every((chunk) => chunk.length <= 64_000));
});

test("chunkBridgeOutput keeps boundaries and empty text intact", () => {
  assert.deepEqual(chunkBridgeOutput("x".repeat(64_000)).map((c) => c.length), [64_000]);
  assert.deepEqual(
    chunkBridgeOutput("x".repeat(64_001)).map((c) => c.length),
    [64_000, 1],
  );
  assert.deepEqual(chunkBridgeOutput(""), []);
  assert.deepEqual(chunkBridgeOutput("hello"), ["hello"]);
});

test("postOutput posts chunks instead of one oversized body", () => {
  const postStart = notebookSource.indexOf("const postOutput =");
  assert.notEqual(postStart, -1, "postOutput not found in notebook.js");
  const postEnd = notebookSource.indexOf("};", postStart);
  const postOutput = notebookSource.slice(postStart, postEnd + 2);
  assert.match(postOutput, /chunkBridgeOutput\(text\)/);
  assert.match(postOutput, /text: chunk/);
  assert.doesNotMatch(postOutput, /JSON\.stringify\(\{ requestId, stream, text \}\)/);
});

test("teardown runs on pagehide, not beforeunload", () => {
  // The dirty prompt stays on beforeunload; the destructive teardown
  // (persistDraft + session DELETE + bridge/runtime disposal) must only
  // run when the page is actually going away.
  const beforeunloadCount = (notebookSource.match(/addEventListener\("beforeunload"/g) || []).length;
  assert.equal(beforeunloadCount, 1);
  const pagehideStart = notebookSource.indexOf('addEventListener("pagehide"');
  assert.notEqual(pagehideStart, -1, "no pagehide listener");
  const pagehideEnd = notebookSource.indexOf("\n  });", pagehideStart);
  const pagehide = notebookSource.slice(pagehideStart, pagehideEnd + 5);
  assert.match(pagehide, /persistDraft\(\)/);
  assert.match(pagehide, /method: "DELETE"/);
  assert.match(pagehide, /browserBridge\?\.stop\(\)/);
  assert.match(pagehide, /browserRuntime\?\.dispose\(\)/);
  const beforeunloadStart = notebookSource.indexOf('addEventListener("beforeunload"');
  const beforeunloadEnd = notebookSource.indexOf("\n  });", beforeunloadStart);
  const beforeunload = notebookSource.slice(beforeunloadStart, beforeunloadEnd + 5);
  assert.match(beforeunload, /state\.dirty/);
  assert.doesNotMatch(beforeunload, /DELETE/);
});

test("highlighter dispose frees parsers, queries, tree and snapshots", () => {
  const deleted = [];
  const stub = (name) => ({
    delete() {
      deleted.push(name);
    },
    setLanguage() {},
  });
  const tree = { delete: () => deleted.push("tree") };
  const highlighter = new testing.KediTreeSitterHighlighter({
    Parser: class {
      constructor() {
        this.deleted = false;
      }
      setLanguage() {}
      delete() {
        deleted.push("parser");
      }
    },
    kediLanguage: {},
    kediQuery: stub("kediQuery"),
    pythonLanguage: {},
    pythonQuery: stub("pythonQuery"),
    injectionQuery: stub("injectionQuery"),
  });
  highlighter.tree = tree;
  highlighter.snapshots.set("tree-sitter-kedi:1", new Uint32Array([1]));

  highlighter.dispose();
  assert.deepEqual(deleted.sort(), [
    "injectionQuery",
    "kediQuery",
    "parser",
    "parser",
    "pythonQuery",
    "tree",
  ]);
  assert.equal(highlighter.tree, null);
  assert.equal(highlighter.snapshots.size, 0);

  // Idempotent: a second dispose must not delete anything again.
  highlighter.dispose();
  assert.equal(deleted.length, 6);
});

test("editor disposes the per-model highlighter on model disposal", async () => {
  const editorSource = await readFile(
    new URL("../src/kedi_notebook/static/kedi-editor.js", import.meta.url),
    "utf8",
  );
  const providerStart = editorSource.indexOf("provideDocumentSemanticTokens");
  assert.notEqual(providerStart, -1, "semantic tokens provider not found");
  const provider = editorSource.slice(providerStart, providerStart + 1200);
  assert.match(provider, /model\.onDidDispose/);
  assert.match(provider, /highlighter\.dispose\(\)/);
  assert.match(provider, /highlighters\.delete\(model\)/);
});
