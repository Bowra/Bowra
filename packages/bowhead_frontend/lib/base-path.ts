/**
 * Build-time deployment path helpers.
 *
 * Next.js applies `basePath` to Link/router/static assets, but browser APIs such
 * as fetch and window.location do not. Keep those requests on the same public
 * path when Bowhead is reverse-proxied below a URI prefix.
 */

declare global {
  interface Window {
    __BOWHEAD_PUBLIC_CONTEXT__?: {
      entryId: string | null;
      teamId: string | null;
      baseUrl: string;
      basePath: string;
      matchSource: string;
    };
    __BOWHEAD_BASE_PATH__?: string;
    __BOWHEAD_BASE_URL__?: string;
  }
}

export function publicBaseUrl(): string | null {
  if (typeof window === "undefined") return null;
  return `${window.location.origin}${basePath()}`;
}

function normalizeBasePath(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "/") return "";
  if (!trimmed.startsWith("/") || trimmed.startsWith("//") || /[?#]/.test(trimmed)) {
    throw new Error("BOWHEAD_BASE_PATH must be an absolute path without query or fragment");
  }
  return trimmed.replace(/\/+$/, "");
}

// The route meta describes the initial HTML document and does not change during
// Next.js client navigation, so its derived entry must remain a document snapshot.
const initialBrowserBasePath = deriveBrowserBasePath();

export function basePath(): string {
  if (typeof window === "undefined") return "";
  const browserPrefix = browserBasePath();
  if (browserPrefix !== null) return browserPrefix;
  const configured = window.__BOWHEAD_PUBLIC_CONTEXT__?.basePath
    ?? window.__BOWHEAD_BASE_PATH__
    ?? document.querySelector('meta[name="bowhead-base-path"]')?.getAttribute("content")
    ?? "";
  return normalizeBasePath(configured);
}

function browserBasePath(): string | null {
  return initialBrowserBasePath;
}

function deriveBrowserBasePath(): string | null {
  if (typeof window === "undefined") return null;
  const internalPath = document
    .querySelector('meta[name="bowhead-internal-path"]')
    ?.getAttribute("content");
  if (!internalPath?.startsWith("/") || internalPath.startsWith("//")) {
    return null;
  }
  // The Gateway bootstrap resolves this value against the initial document.
  // Reuse it when HMR reloads this module after the SPA has already navigated.
  if (window.__BOWHEAD_BASE_PATH__ !== undefined) {
    return normalizeBasePath(window.__BOWHEAD_BASE_PATH__);
  }
  const browserPath = window.location.pathname;
  if (internalPath === "/") {
    return browserPath.endsWith("/")
      ? normalizeBasePath(browserPath.slice(0, -1))
      : null;
  }
  if (!browserPath.endsWith(internalPath)) return null;
  return normalizeBasePath(browserPath.slice(0, -internalPath.length));
}

/** Prefix a same-origin absolute path exactly once. */
export function appPath(path: string): string {
  const prefix = basePath();
  if (!path.startsWith("/") || path.startsWith("//") || !prefix) return path;
  if (path === prefix || path.startsWith(`${prefix}?`) || path.startsWith(`${prefix}#`)) {
    return `${prefix}/${path.slice(prefix.length)}`;
  }
  if (path.startsWith(`${prefix}/`)) return path;
  // StaticFiles treats the deployment root as a directory.  A bare prefix causes
  // a directory-slash redirect, whose absolute Location can expose a reverse
  // proxy's internal port.  Emit the canonical public root directly.
  return path === "/" ? `${prefix}/` : `${prefix}${path}`;
}

function pathForRequest(input: RequestInfo | URL): RequestInfo | URL {
  if (typeof input === "string") return appPath(input);
  if (input instanceof URL) {
    if (typeof window !== "undefined" && input.origin === window.location.origin) {
      return new URL(appPath(`${input.pathname}${input.search}${input.hash}`), input.origin);
    }
    return input;
  }
  if (typeof window !== "undefined") {
    const url = new URL(input.url);
    if (url.origin === window.location.origin) {
      return new Request(new URL(appPath(`${url.pathname}${url.search}${url.hash}`), url.origin), input);
    }
  }
  return input;
}

/** Same-origin fetch that honours Bowhead's optional deployment path. */
export function appFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = pathForRequest(input);
  if (typeof window === "undefined") return init === undefined ? globalThis.fetch(path) : globalThis.fetch(path, init);
  const url = new URL(path instanceof Request ? path.url : path.toString(), window.location.href);
  if (url.origin !== window.location.origin) return init === undefined ? globalThis.fetch(path) : globalThis.fetch(path, init);
  const headers = new Headers(init?.headers ?? (path instanceof Request ? path.headers : undefined));
  headers.set("X-Bowhead-Public-Base", publicBaseUrl()!);
  return globalThis.fetch(path, { ...init, headers });
}

/** Remove the configured prefix before passing an internal URL to Next router. */
export function unprefixAppPath(path: string): string {
  const prefix = basePath();
  if (!prefix || path !== prefix && !path.startsWith(`${prefix}/`) && !path.startsWith(`${prefix}?`) && !path.startsWith(`${prefix}#`)) {
    return path;
  }
  const result = path.slice(prefix.length);
  return !result || result.startsWith("?") || result.startsWith("#") ? `/${result}` : result;
}
