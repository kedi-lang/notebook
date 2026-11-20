import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_BRIDGE_OUTPUT_CHARS,
  splitOutputText,
} from "../src/kedi_notebook/static/notebook/output-chunks.js";

test("splitOutputText leaves short output whole", () => {
  assert.deepEqual(splitOutputText("hello"), ["hello"]);
  assert.deepEqual(splitOutputText(""), [""]);
  assert.deepEqual(splitOutputText("x".repeat(64_000)), ["x".repeat(64_000)]);
});

test("splitOutputText splits long output into 64,000-char chunks", () => {
  const text = "a".repeat(200_000);
  const chunks = splitOutputText(text);
  assert.equal(chunks.length, 4);
  assert.ok(chunks.every((chunk) => chunk.length <= MAX_BRIDGE_OUTPUT_CHARS));
  assert.equal(chunks.join(""), text);
});

test("splitOutputText never splits a surrogate pair", () => {
  // A surrogate pair straddling the 64,000-char boundary: the high
  // surrogate is the last character of the first chunk's range.
  const text = "a".repeat(63_999) + "😀" + "b".repeat(64_000);
  const chunks = splitOutputText(text);
  assert.equal(chunks[0].length, 63_999);
  assert.ok(chunks[0].endsWith("a"));
  assert.ok(chunks[1].startsWith("😀"));
  assert.ok(chunks.every((chunk) => chunk.length <= MAX_BRIDGE_OUTPUT_CHARS));
  assert.equal(chunks.join(""), text);
});
