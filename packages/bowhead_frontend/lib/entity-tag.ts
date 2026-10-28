/**
 * Normalize response-header entity-tags to the server-issued canonical form.
 *
 * Why: compression middleware (CDNs) **must** weaken forwarded entity-tags under RFC 9110 §8.8.1,
 * turning the origin's `"personal-v3:<sha256>"` into `W/"personal-v3:<sha256>"` at the browser.
 * If the client sends this weakened form unchanged in `If-Match`, the server's bytewise comparison can no longer match.
 * At a customer site behind EdgeOne, this made Personal Settings → Appearance inaccessible to every member.
 *
 * Normalize only at the **boundary**, without relaxing modules' own ETag-format validation—keep the weak-ETag concept
 * here, outside frontend state. After normalization, `If-Match` sends the strong form,
 * so this **works even without upgrading the server**, adding defense in depth against server middleware.
 *
 * Note: process response headers only. CDNs cannot alter ETags in JSON bodies (chart styles, subagent drafts, etc.),
 * which neither need nor should use this helper.
 */

const WEAK_PREFIX = "W/";

/** Strip the weak-validator prefix; return values outside `W/"…"` unchanged (normalization only, no syntax repair). */
export function canonicalEntityTag<T extends string | null | undefined>(value: T): T {
  if (typeof value !== "string") return value;
  if (!value.startsWith(WEAK_PREFIX)) return value;
  const rest = value.slice(WEAK_PREFIX.length);
  return (rest.startsWith('"') ? rest : value) as T;
}

/** Read ETag from response headers and normalize it. */
export function readCanonicalEtag(response: Response): string | null {
  return canonicalEntityTag(response.headers.get("etag"));
}
