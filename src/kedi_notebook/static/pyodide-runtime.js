export class PyodideRuntime {
  constructor(onStatus, io = {}) {
    this.onStatus = onStatus;
    this.onStdout = io.onStdout ?? (() => {});
    this.onStderr = io.onStderr ?? (() => {});
    this.onStdin = io.onStdin ?? (() => Promise.resolve(null));
    this.worker = null;
    this.ready = null;
    this.resolveReady = null;
    this.rejectReady = null;
    this.nextId = 1;
    this.pending = new Map();
    this.executionQueue = Promise.resolve();
  }

  preload() {
    this.#ensureWorker();
    return this.ready;
  }

  execute(request, io = {}) {
    const execution = this.executionQueue.then(() => this.#execute(request, io));
    this.executionQueue = execution.then(
      () => undefined,
      () => undefined,
    );
    return execution;
  }

  async #execute(request, io) {
    await this.preload();
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {
        resolve,
        reject,
        stdout: "",
        stderr: "",
        onStdout: io.onStdout,
        onStderr: io.onStderr,
      });
      this.worker.postMessage({ id, request });
    });
  }

  dispose() {
    const error = new Error("Pyodide worker was terminated");
    this.worker?.terminate();
    this.worker = null;
    this.rejectReady?.(error);
    this.ready = null;
    this.resolveReady = null;
    this.rejectReady = null;
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
  }

  #ensureWorker() {
    if (this.worker) {
      return;
    }
    this.onStatus("Loading Python sandbox");
    this.worker = new Worker("/pyodide-worker.js", {
      type: "module",
    });
    this.ready = new Promise((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    this.worker.addEventListener("message", (event) => {
      if (event.data?.type === "ready") {
        this.resolveReady?.();
        this.resolveReady = null;
        this.rejectReady = null;
        return;
      }
      if (event.data?.type === "ready_error") {
        const error = new Error(
          event.data.error || "Failed to load Python sandbox",
        );
        this.rejectReady?.(error);
        this.worker?.terminate();
        this.worker = null;
        this.ready = null;
        this.resolveReady = null;
        this.rejectReady = null;
        return;
      }
      if (event.data?.type === "status") {
        this.onStatus(event.data.message || "Loading Python dependency");
        return;
      }
      if (event.data?.type === "stream") {
        const pending = this.pending.get(event.data.id);
        if (!pending) {
          return;
        }
        const text = event.data.text ?? "";
        const stream = event.data.stream === "stderr" ? "stderr" : "stdout";
        pending[stream] = appendBoundedOutput(pending[stream], text);
        const callback = event.data.stream === "stderr" ? this.onStderr : this.onStdout;
        callback(text);
        const requestCallback =
          event.data.stream === "stderr" ? pending.onStderr : pending.onStdout;
        requestCallback?.(text);
        return;
      }
      if (event.data?.type === "stdin") {
        this.#handleStdin(event.data);
        return;
      }
      const pending = this.pending.get(event.data?.id);
      if (!pending) {
        return;
      }
      this.pending.delete(event.data.id);
      const response = event.data.response || {};
      pending.resolve({
        ...response,
        stdout: pending.stdout || response.stdout || "",
        stderr: pending.stderr || response.stderr || "",
      });
    });
    this.worker.addEventListener("error", (event) => {
      const error = new Error(event.message || "Pyodide worker failed");
      for (const pending of this.pending.values()) {
        pending.reject(error);
      }
      this.pending.clear();
      this.worker?.terminate();
      this.worker = null;
      this.rejectReady?.(error);
      this.ready = null;
      this.resolveReady = null;
      this.rejectReady = null;
    });
  }

  async #handleStdin(message) {
    const pending = this.pending.get(message.id);
    if (!pending) {
      cancelStdin(message.buffer);
      return;
    }
    try {
      const value = await this.onStdin();
      if (value !== null) {
        const echo = `${value}\n`;
        pending.stdout += echo;
        this.onStdout(echo);
      }
      respondToStdin(message.buffer, value);
    } catch {
      cancelStdin(message.buffer);
    }
  }
}

const MAX_OUTPUT_CHARS = 200_000;
const OUTPUT_TRUNCATION_NOTICE = "\n[output truncated by Kedi Notebook]";

function appendBoundedOutput(current, addition) {
  if (!addition || current.endsWith(OUTPUT_TRUNCATION_NOTICE)) {
    return current;
  }
  const remaining = MAX_OUTPUT_CHARS - current.length;
  if (remaining <= 0) {
    return current + OUTPUT_TRUNCATION_NOTICE;
  }
  if (addition.length <= remaining) {
    return current + addition;
  }
  return current + addition.slice(0, remaining) + OUTPUT_TRUNCATION_NOTICE;
}

const STDIN_HEADER_BYTES = 16;

function respondToStdin(buffer, value) {
  const control = new Int32Array(buffer, 0, STDIN_HEADER_BYTES / Int32Array.BYTES_PER_ELEMENT);
  if (value === null) {
    Atomics.store(control, 0, 2);
    Atomics.notify(control, 0);
    return;
  }

  const bytes = new TextEncoder().encode(`${value}\n`);
  const destination = new Uint8Array(buffer, STDIN_HEADER_BYTES);
  if (bytes.length > destination.length) {
    cancelStdin(buffer);
    return;
  }
  destination.set(bytes);
  Atomics.store(control, 1, bytes.length);
  Atomics.store(control, 0, 1);
  Atomics.notify(control, 0);
}

function cancelStdin(buffer) {
  const control = new Int32Array(buffer, 0, STDIN_HEADER_BYTES / Int32Array.BYTES_PER_ELEMENT);
  Atomics.store(control, 0, 3);
  Atomics.notify(control, 0);
}
