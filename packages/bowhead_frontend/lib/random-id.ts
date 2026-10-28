/**
 * Shared random-ID and idempotency-key generator.
 *
 * crypto.randomUUID is unavailable on insecure HTTP origins. Use getRandomValues
 * to produce a valid v4 UUID there, with Math.random as a last resort for runtimes
 * that provide neither API. Callers must use this helper so UI actions do not fail
 * before sending their request. random-id.source.test.ts enforces the shared entry.
 */

/** A v4 UUID. Use native generation in secure contexts, otherwise construct per RFC 4122. */
export function randomUuid(): string {
  const c = typeof globalThis.crypto !== "undefined" ? globalThis.crypto : undefined;

  if (typeof c?.randomUUID === "function") {
    return c.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof c?.getRandomValues === "function") {
    // getRandomValues is **not** secure-context-only; it remains available over plain HTTP.
    c.getRandomValues(bytes);
  } else {
    // Use this fallback only when even WebCrypto is absent (old jsdom / extremely old browsers).
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  // RFC 4122 §4.4: fix version (4) and variant (10xx) so format validators accept it.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}
