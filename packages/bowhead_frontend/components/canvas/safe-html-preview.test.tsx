import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildSafeHtmlDocument,
  isSafeHtmlMime,
  MAX_SAFE_HTML_BYTES,
  SafeHtmlFrame,
  useSafeHtmlDocument,
} from "./safe-html-preview";

const { authSessionMock } = vi.hoisted(() => ({ authSessionMock: vi.fn(() => null as unknown) }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: authSessionMock }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  authSessionMock.mockReturnValue(null);
});

describe("safe HTML preview", () => {
  it("accepts only explicit HTML MIME types", () => {
    expect(isSafeHtmlMime("text/html; charset=utf-8")).toBe(true);
    expect(isSafeHtmlMime("application/xhtml+xml")).toBe(true);
    expect(isSafeHtmlMime("text/plain")).toBe(false);
    expect(isSafeHtmlMime("image/svg+xml")).toBe(false);
  });

  it("removes executable, interactive, navigation, and network-bearing content", async () => {
    const document = await buildSafeHtmlDocument(`
      <!doctype html>
      <html>
        <head>
          <meta http-equiv="refresh" content="0;url=https://attacker.invalid">
          <base href="https://attacker.invalid/">
          <link rel="stylesheet" href="https://attacker.invalid/a.css">
          <style>
            @import "https://attacker.invalid/b.css";
            .leak { background: url(https://attacker.invalid/pixel); }
          </style>
          <script>parent.document.body.dataset.pwned = "1"</script>
        </head>
        <body onload="fetch('https://attacker.invalid/load')">
          <form action="https://attacker.invalid/post"><input autofocus></form>
          <a href="https://attacker.invalid/nav" target="_top">leave</a>
          <a href="#section">section</a>
          <img src="https://attacker.invalid/image"
               srcset="https://attacker.invalid/2x 2x" onerror="alert(1)">
          <img alt="inline" src="data:image/png;base64,AAAA">
          <div style="background:url(https://attacker.invalid/style)">safe text</div>
        </body>
      </html>
    `);

    const parsed = new DOMParser().parseFromString(document, "text/html");
    expect(parsed.querySelector("script")).toBeNull();
    expect(parsed.querySelector("form")).toBeNull();
    expect(parsed.querySelector("input")).toBeNull();
    expect(parsed.querySelector("meta[http-equiv='refresh']")).toBeNull();
    expect(parsed.querySelector("base")).toBeNull();
    expect(parsed.querySelector("link")).toBeNull();
    expect(parsed.querySelector("[onload], [onerror]")).toBeNull();
    expect(parsed.querySelector("a")?.hasAttribute("href")).toBe(false);
    expect(parsed.querySelectorAll("a")[1]?.getAttribute("href")).toBe("#section");
    expect(parsed.querySelector("img")?.hasAttribute("src")).toBe(false);
    expect(parsed.querySelector("img")?.hasAttribute("srcset")).toBe(false);
    expect(parsed.querySelectorAll("img")[1]?.getAttribute("src")).toBe(
      "data:image/png;base64,AAAA",
    );
    expect(document).not.toContain("attacker.invalid");
    expect(document).toContain("default-src 'none'");
    expect(document).toContain("script-src 'none'");
    expect(document).toContain("form-action 'none'");
  });

  it("renders with an empty sandbox token list", () => {
    render(
      <SafeHtmlFrame
        document="<!doctype html><p>safe</p>"
        title="result.html"
      />,
    );

    const frame = screen.getByTitle("result.html");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
  });

  it("rejects a JavaScript-only mount instead of rendering a blank success", async () => {
    await expect(buildSafeHtmlDocument(`
      <html><body><div id="root"></div><script>renderApp()</script></body></html>
    `)).rejects.toMatchObject({ name: "SafeHtmlStaticContentEmptyError" });
  });

  it("keeps an allowed raster data image as readable static content", async () => {
    const document = await buildSafeHtmlDocument(
      '<html><body><img alt="chart" src="data:image/png;base64,AAAA"></body></html>',
    );

    expect(document).toContain("data:image/png;base64,AAAA");
  });

  it("keeps sandbox content independent from the authenticated app theme", async () => {
    document.documentElement.style.setProperty("--c-surface", "#101820");
    document.documentElement.style.setProperty("--c-ink", "#f8fafc");
    const darkHostDocument = await buildSafeHtmlDocument(
      "<html><body><p>Stable content</p></body></html>",
    );

    document.documentElement.style.setProperty("--c-surface", "#fffdf8");
    document.documentElement.style.setProperty("--c-ink", "#201a16");
    const lightHostDocument = await buildSafeHtmlDocument(
      "<html><body><p>Stable content</p></body></html>",
    );

    expect(darkHostDocument).toBe(lightHostDocument);
    expect(darkHostDocument).not.toContain("--c-surface");
    expect(darkHostDocument).not.toContain("--c-ink");
  });

  it("rejects a declared oversize document before fetching it", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() =>
      useSafeHtmlDocument({
        enabled: true,
        href: "/api/v1/files/too_large",
        declaredSize: 50 * 1024 * 1024 + 1,
      }),
    );

    await waitFor(() => expect(result.current.error).toBe("too_large"));
    expect(result.current.maxBytes).toBe(50 * 1024 * 1024);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a response whose served MIME is not HTML", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<p>not declared html</p>", {
          status: 200,
          headers: { "content-type": "text/plain" },
        }),
      ),
    );
    const { result } = renderHook(() =>
      useSafeHtmlDocument({
        enabled: true,
        href: "/api/v1/files/wrong_type",
      }),
    );

    await waitFor(() => expect(result.current.error).toBe("unsupported"));
    expect(result.current.document).toBeNull();
  });

  it.each([undefined, "6291456"])("uses the served byte policy (%s) and accepts reports above the old limit", async (policy) => {
    const html = `<p>Readable report</p><!--${"x".repeat(6 * 1024 * 1024 - 128)}-->`;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(html, {
      headers: { "content-type": "text/html", ...(policy ? { "X-Bowhead-HTML-Max-Bytes": policy } : {}) },
    })));
    const { result } = renderHook(() => useSafeHtmlDocument({ enabled: true, href: "/api/v1/files/report" }));
    await waitFor(() => expect(result.current.document).toContain("Readable report"));
    expect(result.current.error).toBeNull();
  });

  it.each(["64", "invalid", "52428801"])("enforces the policy on streamed UTF-8 bytes (%s)", async (policy) => {
    const limit = policy === "64" ? 64 : MAX_SAFE_HTML_BYTES;
    const bytes = new TextEncoder().encode(`<p>${"界".repeat(Math.ceil(limit / 3))}</p>`);
    const cancel = vi.fn();
    const stream = new ReadableStream({ start(controller) { controller.enqueue(bytes); }, cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, {
      headers: { "content-type": "text/html", "X-Bowhead-HTML-Max-Bytes": policy },
    })));
    const { result } = renderHook(() => useSafeHtmlDocument({ enabled: true, href: "/api/v1/files/report" }));
    await waitFor(() => expect(result.current.error).toBe("too_large"));
    expect(result.current.maxBytes).toBe(limit);
    expect(result.current.document).toBeNull();
    expect(cancel).toHaveBeenCalled();
  });

  it("recovers to static reading from invalid interactive code without refetching bytes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('<p>Original reading content</p><script>const = invalid</script>', {
      headers: { "content-type": "text/html" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(({ presentation }: { presentation: "static" | "interactive" }) =>
      useSafeHtmlDocument({ enabled: true, href: "/api/v1/files/recovery", presentation }),
      { initialProps: { presentation: "interactive" as "static" | "interactive" } });
    await waitFor(() => expect(result.current.error).toBe("interaction_unavailable"));
    expect(result.current.document).toBeNull();
    rerender({ presentation: "static" });
    await waitFor(() => expect(result.current.document).toContain("Original reading content"));
    expect(result.current.error).toBeNull();
    expect(result.current.interactive).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("masks cached bytes immediately and reauthorizes after a principal change", async () => {
    authSessionMock.mockReturnValue({ status: "authenticated", me: { id: "viewer_a", team_id: "team_a" } });
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response("<p>Viewer A content</p>", {
      headers: { "content-type": "text/html" },
    })).mockResolvedValueOnce(new Response("Forbidden", { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(() => useSafeHtmlDocument({ enabled: true, href: "/api/v1/files/owned" }));
    await waitFor(() => expect(result.current.document).toContain("Viewer A content"));
    authSessionMock.mockReturnValue({ status: "authenticated", me: { id: "viewer_b", team_id: "team_b" } });
    rerender();
    expect(result.current.document).toBeNull();
    await waitFor(() => expect(result.current.error).toBe("unavailable"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("maps a sanitized JavaScript-only shell to unsupported", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response('<div id="root"></div><script>renderApp()</script>', {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
      ),
    );
    const { result } = renderHook(() =>
      useSafeHtmlDocument({
        enabled: true,
        href: "/api/v1/files/js_only",
      }),
    );

    await waitFor(() => expect(result.current.error).toBe("unsupported"));
    expect(result.current.document).toBeNull();
  });
});
