"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { HtmlPreviewIssue, InteractiveHtmlDocument } from "@/lib/html-preview-contract";
import { INTERACTIVE_HTML_PERMISSIONS, INTERACTIVE_HTML_SANDBOX } from "@/lib/html-preview-contract";
import { appFetch } from "@/lib/base-path";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

export const MAX_SAFE_HTML_BYTES = 25 * 1024 * 1024;
const MAX_CONFIGURABLE_HTML_BYTES = 50 * 1024 * 1024;

function responseHtmlLimit(response: Response): number {
  const value = Number(response.headers.get("X-Bowhead-HTML-Max-Bytes"));
  return Number.isSafeInteger(value) && value >= 1 && value <= MAX_CONFIGURABLE_HTML_BYTES
    ? value : MAX_SAFE_HTML_BYTES;
}

const SAFE_HTML_CSP = [
  "default-src 'none'",
  "script-src 'none'",
  "style-src 'unsafe-inline'",
  "img-src data:",
  "font-src data:",
  "media-src 'none'",
  "connect-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "worker-src 'none'",
  "object-src 'none'",
  "form-action 'none'",
  "base-uri 'none'",
  "manifest-src 'none'",
].join("; ");

const FORBIDDEN_TAGS = [
  "script",
  "iframe",
  "frame",
  "frameset",
  "object",
  "embed",
  "form",
  "input",
  "button",
  "textarea",
  "select",
  "option",
  "link",
  "meta",
  "base",
  "portal",
  "svg",
  "math",
  "audio",
  "video",
  "source",
  "track",
];

const FORBIDDEN_ATTRIBUTES = [
  "srcdoc",
  "action",
  "formaction",
  "target",
  "download",
  "ping",
  "autofocus",
  "integrity",
  "nonce",
  "srcset",
  "imagesrcset",
  "manifest",
];

const URL_ATTRIBUTES = [
  "href",
  "src",
  "xlink:href",
  "poster",
  "cite",
  "background",
  "longdesc",
  "usemap",
] as const;

export type SafeHtmlErrorCode =
  | "too_large"
  | "unsupported"
  | "unavailable"
  | "load_failed"
  | "interaction_unavailable";

type SafeHtmlState = {
  key: string;
  document: string | null;
  error: SafeHtmlErrorCode | null;
  maxBytes?: number;
  interactive?: InteractiveHtmlDocument;
};

export class SafeHtmlStaticContentEmptyError extends Error {
  constructor() {
    super("sanitized HTML has no readable static content");
    this.name = "SafeHtmlStaticContentEmptyError";
  }
}

export function isSafeHtmlMime(mime: string | null | undefined): boolean {
  const normalized = mime?.split(";", 1)[0]?.trim().toLowerCase();
  return normalized === "text/html" || normalized === "application/xhtml+xml";
}

function removeCssResourceLoads(css: string): string {
  return css
    .replace(/@import\b[^;]*(?:;|$)/gi, "")
    .replace(/url\s*\([^)]*\)/gi, "none");
}

async function readResponseTextWithinLimit(
  response: Response,
  maxBytes: number,
): Promise<string | null> {
  if (!response.body) {
    const blob = await response.blob();
    return blob.size <= maxBytes ? blob.text() : null;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let totalBytes = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > maxBytes) {
        await reader.cancel();
        return null;
      }
      text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

/**
 * Convert untrusted model HTML into a static, self-contained document.
 *
 * DOMPurify removes executable/interactive markup. The second DOM pass strips
 * every remaining URL-bearing attribute and CSS resource load before adding
 * our own fail-closed CSP. The iframe still receives no sandbox permissions,
 * so this remains safe if one sanitizer rule regresses.
 */
export async function buildSafeHtmlDocument(rawHtml: string): Promise<string> {
  const { default: DOMPurify } = await import("dompurify");
  const sanitized = DOMPurify.sanitize(rawHtml, {
    WHOLE_DOCUMENT: true,
    RETURN_TRUSTED_TYPE: false,
    FORBID_TAGS: FORBIDDEN_TAGS,
    FORBID_ATTR: FORBIDDEN_ATTRIBUTES,
  });

  const parsed = new DOMParser().parseFromString(String(sanitized), "text/html");
  for (const element of parsed.querySelectorAll("*")) {
    for (const attribute of URL_ATTRIBUTES) {
      if (!element.hasAttribute(attribute)) continue;
      const value = element.getAttribute(attribute)?.trim() ?? "";
      const isFragmentLink = attribute === "href" && value.startsWith("#");
      const isRasterDataImage =
        attribute === "src"
        && element.tagName === "IMG"
        && /^data:image\/(?:png|jpe?g|gif|webp);base64,/i.test(value);
      if (!isFragmentLink && !isRasterDataImage) {
        element.removeAttribute(attribute);
      }
    }
    if (element.hasAttribute("style")) {
      element.setAttribute(
        "style",
        removeCssResourceLoads(element.getAttribute("style") ?? ""),
      );
    }
  }
  for (const style of parsed.querySelectorAll("style")) {
    style.textContent = removeCssResourceLoads(style.textContent ?? "");
  }

  const contentRoot = parsed.body.cloneNode(true) as HTMLBodyElement;
  contentRoot.querySelectorAll("style, noscript, template").forEach((node) => node.remove());
  const hasReadableText = (contentRoot.textContent ?? "").replace(/\s+/g, "").length > 0;
  const hasRasterDataImage = Array.from(contentRoot.querySelectorAll("img[src]"))
    .some((image) => /^data:image\/(?:png|jpe?g|gif|webp);base64,/i.test(
      image.getAttribute("src") ?? "",
    ));
  if (!hasReadableText && !hasRasterDataImage) {
    throw new SafeHtmlStaticContentEmptyError();
  }

  const head = parsed.head;
  const charset = parsed.createElement("meta");
  charset.setAttribute("charset", "utf-8");
  const csp = parsed.createElement("meta");
  csp.setAttribute("http-equiv", "Content-Security-Policy");
  csp.setAttribute("content", SAFE_HTML_CSP);
  const referrer = parsed.createElement("meta");
  referrer.setAttribute("name", "referrer");
  referrer.setAttribute("content", "no-referrer");
  const baseStyle = parsed.createElement("style");
  // The sandboxed document is a content boundary. Keep its neutral reading
  // defaults stable instead of copying the authenticated app's theme into it.
  baseStyle.textContent =
    "html,body{margin:0;min-height:100%;background:#fff;"
    + "color:#161310;font-family:system-ui,sans-serif;}"
    + "body{box-sizing:border-box;padding:16px;overflow-wrap:anywhere;}"
    + "img{max-width:100%;height:auto;}table{max-width:100%;border-collapse:collapse;}";
  head.prepend(charset, csp, referrer, baseStyle);

  return `<!doctype html>${parsed.documentElement.outerHTML}`;
}

export function useSafeHtmlDocument({
  enabled,
  href,
  declaredSize,
  reloadToken = 0,
  presentation = "static",
}: {
  enabled: boolean;
  href: string;
  declaredSize?: number | null;
  reloadToken?: number;
  presentation?: "static" | "interactive";
}): {
  document: string | null;
  error: SafeHtmlErrorCode | null;
  loading: boolean;
  maxBytes: number | null;
  interactive?: InteractiveHtmlDocument;
} {
  const auth = useOptionalAuthSession();
  const scope = auth
    ? JSON.stringify([auth.status, auth.me?.id, auth.me?.team_id, auth.me?.auth_session_generation])
    : "standalone";
  const sourceKey = `${scope}:${href}:${declaredSize ?? ""}:${reloadToken}`;
  const sourceCache = useRef<{ key: string; raw: string; maxBytes: number } | null>(null);
  const requestKey = useMemo(
    () => `${enabled ? "1" : "0"}:${sourceKey}:${presentation}`,
    [sourceKey, enabled, presentation],
  );
  const [state, setState] = useState<SafeHtmlState | null>(null);

  useEffect(() => {
    if (!enabled) {
      sourceCache.current = null;
      return;
    }
    if (declaredSize != null && declaredSize > MAX_CONFIGURABLE_HTML_BYTES) {
      setState({ key: requestKey, document: null, error: "too_large", maxBytes: MAX_CONFIGURABLE_HTML_BYTES });
      return;
    }
    const controller = new AbortController();
    const commit = (next: SafeHtmlState) => {
      if (!controller.signal.aborted) setState(next);
    };
    void (async () => {
      try {
        const prepare = async (raw: string, maxBytes: number) => {
          if (presentation === "interactive") {
            try {
              const { buildInteractiveHtmlDocument } = await import("@/lib/interactive-html");
              const interactive = await buildInteractiveHtmlDocument(raw);
              commit({ key: requestKey, document: interactive.document, interactive, error: null, maxBytes });
            } catch {
              commit({ key: requestKey, document: null, error: "interaction_unavailable", maxBytes });
            }
          } else {
            commit({ key: requestKey, document: await buildSafeHtmlDocument(raw), error: null, maxBytes });
          }
        };
        if (sourceCache.current?.key === sourceKey) {
          await prepare(sourceCache.current.raw, sourceCache.current.maxBytes);
          return;
        }
        sourceCache.current = null;
        const response = await appFetch(href, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          commit({ key: requestKey, document: null, error: "unavailable" });
          return;
        }
        if (!isSafeHtmlMime(response.headers.get("content-type"))) {
          commit({ key: requestKey, document: null, error: "unsupported" });
          return;
        }
        const contentLength = Number(response.headers.get("content-length"));
        const maxBytes = responseHtmlLimit(response);
        if (
          (Number.isFinite(contentLength) && contentLength > maxBytes)
          || (declaredSize != null && declaredSize > maxBytes)
        ) {
          await response.body?.cancel();
          commit({ key: requestKey, document: null, error: "too_large", maxBytes });
          return;
        }
        const rawHtml = await readResponseTextWithinLimit(response, maxBytes);
        if (rawHtml === null) {
          commit({ key: requestKey, document: null, error: "too_large", maxBytes });
          return;
        }
        if (controller.signal.aborted) return;
        sourceCache.current = { key: sourceKey, raw: rawHtml, maxBytes };
        await prepare(rawHtml, maxBytes);
      } catch (error) {
        commit({
          key: requestKey,
          document: null,
          error: error instanceof SafeHtmlStaticContentEmptyError
            ? "unsupported"
            : "load_failed",
        });
      }
    })();
    return () => controller.abort();
  }, [declaredSize, enabled, href, reloadToken, requestKey, presentation, sourceKey]);

  const current = enabled && state?.key === requestKey ? state : null;
  return {
    document: current?.document ?? null,
    error: current?.error ?? null,
    loading: enabled && current === null,
    maxBytes: current?.maxBytes ?? null,
    interactive: current?.interactive,
  };
}

export function SafeHtmlFrame({
  document,
  title,
  className,
  height,
  interactive,
  onEscape,
  onIssue,
}: {
  document: string;
  title: string;
  className?: string;
  height?: number | string;
  interactive?: InteractiveHtmlDocument;
  onEscape?: () => void;
  onIssue?: (issue: HtmlPreviewIssue) => void;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    if (!interactive) return;
    const receive = (event: MessageEvent) => {
      if (event.source !== frameRef.current?.contentWindow || event.data?.channel !== interactive.channel) return;
      if (event.data.type === "bowhead-html-escape") onEscape?.();
      if (event.data.type === "bowhead-html-runtime") onIssue?.("runtime");
      if (event.data.type === "bowhead-html-blocked") onIssue?.("blocked");
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [interactive, onEscape, onIssue]);
  return (
    <iframe
      ref={frameRef}
      srcDoc={document}
      title={title}
      sandbox={interactive ? INTERACTIVE_HTML_SANDBOX : ""}
      allow={interactive ? INTERACTIVE_HTML_PERMISSIONS : undefined}
      referrerPolicy="no-referrer"
      className={className}
      style={height == null ? undefined : { height }}
    />
  );
}
