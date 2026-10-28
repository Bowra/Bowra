/**
 * Sprint pptx-from-template M1 (RD §7): reject non-http(s) URL schemes so a
 * third-party / LLM-supplied URL (web_search results, narrative sources) can
 * never become a live `javascript:` / `data:` XSS vector when rendered into an
 * <a href>. Returns `undefined` for unsafe URLs so the anchor renders without a
 * navigable href.
 *
 * Allowed: absolute http(s) and root-relative paths (our own download links).
 */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  const u = url.trim();
  if (/^https?:\/\//i.test(u)) return u;
  // Root-relative (e.g. /api/v1/files/...) ONLY — reject protocol-relative
  // "//evil.com" and the backslash variant "/\evil.com" (browsers treat "\"
  // as "/"), which would otherwise navigate off-origin.
  if (/^\/(?![/\\])/.test(u)) return u;
  return undefined;
}
