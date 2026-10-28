/**
 * Frame attributes for third-party pages embedded in chat or canvas.
 *
 * Only absolute http(s) pages are framed; `javascript:` / `data:` / relative
 * URLs return null so the caller renders a fallback instead of an iframe.
 *
 * The sandbox keeps what embedded BI/SaaS pages need (scripts, their own
 * storage, forms, popups, downloads) and withholds top-level navigation, so an
 * embedded page cannot redirect Bowhead itself. A page on Bowhead's own origin
 * would be able to lift its sandbox with `allow-scripts` + `allow-same-origin`,
 * so that pair is never granted to a same-origin URL.
 */
const EMBED_SANDBOX_BASE = [
  "allow-scripts",
  "allow-forms",
  "allow-popups",
  "allow-popups-to-escape-sandbox",
  "allow-downloads",
];

export interface EmbedFrame {
  src: string;
  sandbox: string;
}

export function embedFrame(raw: string | null | undefined, hostOrigin?: string): EmbedFrame | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const origin = hostOrigin ?? (typeof window === "undefined" ? undefined : window.location.origin);
  const tokens = url.origin === origin ? EMBED_SANDBOX_BASE : [...EMBED_SANDBOX_BASE, "allow-same-origin"];
  return { src: url.href, sandbox: tokens.join(" ") };
}
