import { describe, expect, it } from "vitest";

import { HTML_PREVIEW_BUDGET } from "./html-preview-budget";
import { INTERACTIVE_HTML_PERMISSIONS, INTERACTIVE_HTML_SANDBOX } from "./html-preview-contract";

type Built = { ok: boolean; document?: string; channel?: string; externalResources?: boolean; error?: string };
type Preview = { version: string; sandbox: string; permissions: string; build(raw: string): Promise<Built> };

async function preview(): Promise<Preview> {
  await import("./html-preview-sandbox-entry");
  return (globalThis as unknown as { bowheadHtmlPreview: Preview }).bowheadHtmlPreview;
}

describe("preview transform for the sandbox capture", () => {
  it("exposes the product preview's transform and frame policy", async () => {
    const api = await preview();
    // documents/html_view.py checks this version before mounting.
    expect(api.version).toBe("html-preview-transform-v1");
    expect(api.sandbox).toBe(INTERACTIVE_HTML_SANDBOX);
    expect(api.permissions).toBe(INTERACTIVE_HTML_PERMISSIONS);
    const built = await api.build('<p>Total 163.42</p><img src="https://invalid.example/a.png">');
    expect(built.ok).toBe(true);
    expect(built.document).toContain("Total 163.42");
    expect(built.channel).toMatch(/^[0-9a-f-]{36}$/);
    expect(built.externalResources).toBe(true);
  });

  it("reports a budget overrun as resource_limited, never as content", async () => {
    const api = await preview();
    const deep = "<div>".repeat(HTML_PREVIEW_BUDGET.depth + 5);
    expect(await api.build(deep)).toEqual({ ok: false, error: "resource_limited" });
  });
});
