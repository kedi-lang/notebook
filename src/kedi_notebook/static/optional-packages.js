// Optional packages the browser runtime installs on demand.
//
// Detection looks for a real import of the package in the cell code. A bare
// mention in a string literal, a comment or a variable name must not trigger
// an install (or, for unsupported packages, an error).

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function codeUsesPackage(code, name) {
  if (typeof code !== "string" || code.length === 0) {
    return false;
  }
  const id = escapeRegExp(name);
  const pattern = new RegExp(
    `(?:^|\\n|;|\`)[ \\t]*import[ \\t]+(?:[\\w, \\t]*\\b)?${id}\\b` +
      `|(?:^|\\n)[ \\t]*from[ \\t]+${id}(?:[ \\t.]|$)` +
      `|__import__\\([ \\t]*["']${id}["']` +
      `|import_module\\([ \\t]*["']${id}["']`,
    "m",
  );
  return pattern.test(code);
}

export const OPTIONAL_PACKAGES = ["logfire"];
export const UNSUPPORTED_PACKAGES = ["pydantic_monty"];
