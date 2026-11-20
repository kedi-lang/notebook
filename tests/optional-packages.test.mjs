import assert from "node:assert/strict";
import { test } from "node:test";

import { codeUsesPackage } from "../src/kedi_notebook/static/optional-packages.js";

test("codeUsesPackage matches real imports of an optional package", () => {
  assert.equal(codeUsesPackage("import logfire", "logfire"), true);
  assert.equal(codeUsesPackage("import os, logfire", "logfire"), true);
  assert.equal(codeUsesPackage("from logfire import configure", "logfire"), true);
  assert.equal(codeUsesPackage("from logfire.sub import x", "logfire"), true);
  assert.equal(codeUsesPackage("import logfire as lf", "logfire"), true);
  assert.equal(codeUsesPackage("__import__('logfire')", "logfire"), true);
  assert.equal(
    codeUsesPackage('importlib.import_module("logfire")', "logfire"),
    true,
  );
  assert.equal(
    codeUsesPackage("```\nimport logfire\n```", "logfire"),
    true,
  );
  assert.equal(codeUsesPackage("`import logfire`", "logfire"), true);
  assert.equal(
    codeUsesPackage("import pydantic_monty", "pydantic_monty"),
    true,
  );
});

test("codeUsesPackage ignores bare mentions in strings, comments and names", () => {
  assert.equal(codeUsesPackage('x = "logfire"', "logfire"), false);
  assert.equal(codeUsesPackage("# logfire is great", "logfire"), false);
  assert.equal(codeUsesPackage("logfire = 1", "logfire"), false);
  assert.equal(codeUsesPackage("import logfire_x", "logfire"), false);
  assert.equal(codeUsesPackage("import os\nlogfire = 1", "logfire"), false);
  assert.equal(codeUsesPackage('x = "pydantic_monty"', "pydantic_monty"), false);
  assert.equal(codeUsesPackage("", "logfire"), false);
  assert.equal(codeUsesPackage(null, "logfire"), false);
});
