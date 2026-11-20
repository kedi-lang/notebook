import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { createKediTreeSitterHighlighter } from "../src/kedi_notebook/static/tree-sitter-highlighter.js";

// In Node, Language.load reads the grammar from the filesystem and rejects
// a file: URL, so a full load cannot complete here. The retry is verified
// one stage earlier: after a failed fetch, the next call must fetch again
// (and fail at the later Language.load stage) instead of returning the
// cached fetch rejection.
test("a rejected resource load is retried instead of cached", async () => {
  const originalFetch = globalThis.fetch;
  let failFetch = true;
  let fetchCalls = 0;
  globalThis.fetch = async (url) => {
    fetchCalls += 1;
    if (failFetch) {
      return { ok: false, status: 503 };
    }
    const text = await readFile(url, "utf8");
    return { ok: true, status: 200, text: async () => text };
  };
  try {
    await assert.rejects(createKediTreeSitterHighlighter(), /HTTP 503/);
    assert.equal(fetchCalls, 3);

    failFetch = false;
    await assert.rejects(createKediTreeSitterHighlighter(), /ENOENT/);
    assert.equal(fetchCalls, 6);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
