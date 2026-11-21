import assert from "node:assert/strict";
import { test } from "node:test";

import { PyodideRuntime } from "../src/kedi_notebook/static/pyodide-runtime.js";

class StubWorker {
  static instances = [];
  static throwOnPost = false;

  constructor(url, options) {
    this.url = url;
    this.options = options;
    this.listeners = new Map();
    this.posted = [];
    StubWorker.instances.push(this);
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }

  postMessage(message) {
    if (StubWorker.throwOnPost) {
      throw new Error("postMessage failed");
    }
    this.posted.push(message);
  }

  terminate() {}

  emit(type, data) {
    this.listeners.get(type)?.({ data });
  }
}

test("execute after preload-then-dispose reports the terminated worker", async () => {
  const originalWorker = globalThis.Worker;
  globalThis.Worker = StubWorker;
  try {
    StubWorker.instances = [];
    StubWorker.throwOnPost = false;
    const runtime = new PyodideRuntime(() => {});
    const execution = runtime.execute({ action: "execute", code: "1" });
    // Let #execute run up to awaiting the (pending) ready promise.
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(StubWorker.instances.length, 1);
    const worker = StubWorker.instances[0];
    // The worker becomes ready, then is disposed before #execute resumes:
    // without the null check this is a bare TypeError on null.postMessage.
    worker.emit("message", { type: "ready" });
    runtime.dispose();
    await assert.rejects(execution, /Pyodide worker was terminated/);
  } finally {
    globalThis.Worker = originalWorker;
  }
});

test("a failing postMessage rejects the execution and drops the pending entry", async () => {
  const originalWorker = globalThis.Worker;
  globalThis.Worker = StubWorker;
  try {
    StubWorker.instances = [];
    StubWorker.throwOnPost = true;
    const runtime = new PyodideRuntime(() => {});
    const execution = runtime.execute({ action: "execute", code: "1" });
    await Promise.resolve();
    await Promise.resolve();
    const worker = StubWorker.instances[0];
    worker.emit("message", { type: "ready" });
    await assert.rejects(execution, /postMessage failed/);
    assert.equal(runtime.pending.size, 0);
  } finally {
    globalThis.Worker = originalWorker;
  }
});
