import { describe, expect, it } from "vitest";
import { parse } from "parse5";
import { createHash } from "node:crypto";
import { buildInteractiveHtmlDocument } from "./interactive-html";

describe("offline interactive HTML preparation", () => {
  it("retains authored controls and hashes guarded code without changing the original", async () => {
    const raw = `<meta http-equiv="refresh" content="0;url=https://invalid.example"><base href="https://invalid.example"><script src="https://invalid.example/library.js"></script><button onclick="return this.textContent++">0</button><script>"use strict"; document.title='ready';</script>`;
    const prepared = await buildInteractiveHtmlDocument(raw);
    const outer = new DOMParser().parseFromString(prepared.document, "text/html");
    const content = outer.querySelector("iframe")!.getAttribute("srcdoc")!;
    const inner = new DOMParser().parseFromString(content, "text/html");
    const code = inner.querySelectorAll("script")[1].textContent!;
    expect(code.startsWith('"use strict";')).toBe(true);
    expect(code.indexOf("__bowheadHtmlNetworkGuardV1")).toBeLessThan(code.indexOf("document.title"));
    const csp = outer.querySelector("meta[http-equiv]")!.getAttribute("content")!;
    expect(csp).toContain(`'sha256-${createHash("sha256").update(code).digest("base64")}'`);
    expect(csp).toContain("frame-src 'none'");
    expect(csp).toContain("form-action 'none'");
    expect(csp).not.toMatch(/script-src 'unsafe-inline'/);
    expect(inner.querySelector("button")?.getAttribute("onclick")).toContain("return this.textContent++");
    expect(inner.querySelector("base, meta[http-equiv], script[src]")).toBeNull();
    expect(raw).toContain('onclick="return this.textContent++"');
    expect(prepared.externalResources).toBe(true);
  });

  it("fails closed on code the guard cannot prepare", async () => {
    await expect(buildInteractiveHtmlDocument("<script>const = broken</script>")).rejects.toThrow();
    await expect(buildInteractiveHtmlDocument('<button onclick="const = broken">Run</button>')).rejects.toThrow();
  });

  it("prepares modules, strict handlers, and dormant templates without a live DOM", async () => {
    const prepared = await buildInteractiveHtmlDocument(`<template><button onclick="'use strict';return 1">Run</button></template><script type="module">"use strict";export const value=1;</script>`);
    expect(parse(prepared.document)).toBeDefined();
    expect(prepared.document).toContain("export const value=1");
    expect(prepared.document).toContain("script-src-attr 'unsafe-hashes'");
  });
});
