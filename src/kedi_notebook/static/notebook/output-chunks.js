// Bridge output posts are capped at 64,000 characters per request
// (BridgeOutputPayload.text), so longer output must be split into ordered
// chunks. A chunk boundary must never split a UTF-16 surrogate pair.

export const MAX_BRIDGE_OUTPUT_CHARS = 64_000;

export function splitOutputText(text, maxChars = MAX_BRIDGE_OUTPUT_CHARS) {
  if (text.length <= maxChars) {
    return [text];
  }
  const chunks = [];
  let start = 0;
  while (start < text.length) {
    let end = Math.min(start + maxChars, text.length);
    if (end < text.length) {
      const last = text.charCodeAt(end - 1);
      const next = text.charCodeAt(end);
      if (
        last >= 0xd800 &&
        last <= 0xdbff &&
        next >= 0xdc00 &&
        next <= 0xdfff
      ) {
        end -= 1;
      }
    }
    chunks.push(text.slice(start, end));
    start = end;
  }
  return chunks;
}
