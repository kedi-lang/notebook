import assert from "node:assert/strict";
import { test } from "node:test";

import { testing } from "../src/kedi_notebook/static/kedi-editor.js";

test("a rejected Monaco load is retried instead of cached", async () => {
  const originalRequire = globalThis.require;
  const originalMonaco = globalThis.monaco;
  try {
    // First load: no AMD loader, so it rejects.
    globalThis.require = undefined;
    await assert.rejects(
      testing.loadMonaco(),
      /Monaco AMD loader is unavailable/,
    );

    // Second load: the AMD loader is present now, so it must retry.
    const fakeMonaco = { editor: {} };
    const amdRequire = (deps, onSuccess) => {
      onSuccess(fakeMonaco);
    };
    amdRequire.config = () => {};
    globalThis.require = amdRequire;
    globalThis.monaco = fakeMonaco;
    const monaco = await testing.loadMonaco();
    assert.equal(monaco, fakeMonaco);
  } finally {
    globalThis.require = originalRequire;
    globalThis.monaco = originalMonaco;
  }
});
