/**
 * WP4/S4 — EmbedCard rendering contract: badge cluster + embedded provenance footer + expand.
 * Assert only surrounding chrome (header/footer independent of iframe content). Script execution round-trips inside the iframe
 * (auto-height/theme/data injection) are covered by html-artifact.test.ts injection-string unit tests + Playwright evidence.
 */
import "@testing-library/jest-dom/vitest";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import { HtmlArtifactSection } from "./html-artifact";
import type { ReportSection } from "@/lib/types";

type E = Extract<ReportSection, { kind: "html_artifact" }>;

const messages = {
  html_artifact: {
    open_raw: "Open raw HTML in new tab",
    reload: "Reload HTML",
    load_failed: "HTML load failed: {err}",
    loading: "Loading HTML artifact…",
    computed_badge: "Static content unchecked",
    injected_badge: "Generation data attached",
    expand: "Expand",
    collapse: "Collapse",
    footer_injected_sources: "Generation data attached · Source citations {sources}",
    footer_injected_no_sources: "Generation data attached · No source citations provided",
    footer_computed_sources: "Static content was not checked against tool output · Source citations {sources}",
    footer_computed_no_sources: "Static content was not checked against tool output",
    safe_preview_badge: "Safe static preview",
    footer_safe_static: "Static sandbox",
    safe_too_large: "too large",
    safe_unavailable: "unavailable",
    safe_load_failed: "safe load failed",
    open_safe_preview: "Open safe preview",
    mobile_safe_hint: "safe mobile",
  },
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() =>
    Promise.resolve(new Response("<html><body>hi</body></html>", {
      status: 200,
      headers: { "content-type": "text/html" },
    })),
  ));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function renderCard(data: E) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <HtmlArtifactSection data={data} />
    </NextIntlClientProvider>,
  );
}

function mk(over: Partial<E> = {}): E {
  return { id: "e1", kind: "html_artifact", artifact_id: "art_1", title: "Sim", ...over } as E;
}

describe("HtmlArtifactSection · D4 EmbedCard chrome", () => {
  it("always shows the AI-generated badge", () => {
    const { container } = renderCard(mk({ data: { x: 1 } }));
    expect(container.textContent).toContain("Safe static preview");
  });

  it("injected data uses a neutral badge and cites normalized sources", () => {
    const { container } = renderCard(mk({ data: { x: 1 }, sources: [2, 6], unverified: false }));
    expect(container.textContent).toContain("Generation data attached");
    expect(container.textContent).not.toContain("Static content unchecked");
    expect(container.textContent).toContain("Generation data attached · Source citations [2] [6]");
  });

  it("injected data without sources says none were provided", () => {
    const { container } = renderCard(mk({ data: { x: 1 }, unverified: false }));
    expect(container.textContent).toContain("Generation data attached · No source citations provided");
  });

  it("missing data: amber badge + block-computation footer warning", () => {
    const { container } = renderCard(mk());
    expect(container.textContent).toContain("Static content unchecked");
    expect(container.textContent).toContain("Static content was not checked against tool output");
  });

  it("keeps source citations when data is computed unchecked", () => {
    const { container } = renderCard(mk({ sources: [1] }));
    expect(container.textContent).toContain("Static content unchecked");
    expect(container.textContent).toContain("Source citations [1]");
  });

  it("Expand opens a fullscreen overlay", () => {
    const { container, getByRole } = renderCard(mk({ data: { x: 1 } }));
    expect(container.querySelector(".rc-embed-overlay")).toBeNull();
    fireEvent.click(getByRole("button", { name: "Expand" }));
    expect(container.querySelector(".rc-embed-overlay")).not.toBeNull();
  });

  it("fullscreen is an in-place modal: same iframe node, root marker, Esc closes and refocuses the toggle", async () => {
    // Real-browser coverage in e2e/html-artifact-fullscreen.spec.ts verifies stacking, hit testing and no iframe reload.
    const { container, getByRole, queryByRole } = renderCard(mk({ data: { x: 1 } }));
    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    const frame = container.querySelector("iframe");
    const toggle = getByRole("button", { name: "Expand" });

    fireEvent.click(toggle);
    const dialog = getByRole("dialog", { name: "Sim" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.contains(frame)).toBe(true);
    expect(container.querySelector("iframe")).toBe(frame);
    expect(document.documentElement).toHaveAttribute("data-rc-embed-fullscreen");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(queryByRole("dialog")).toBeNull());
    expect(document.documentElement).not.toHaveAttribute("data-rc-embed-fullscreen");
    expect(container.querySelector("iframe")).toBe(frame);
    await waitFor(() => expect(toggle).toHaveFocus());
  });

  it("uses static HTML with no sandbox permissions", async () => {
    const { container, getByRole } = renderCard(mk({ data: { x: 1 } }));

    expect(container.textContent).toContain("Safe static preview");
    await waitFor(() => {
      expect(container.querySelector("iframe")).not.toBeNull();
    });
    const frame = container.querySelector("iframe");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(frame).not.toHaveAttribute("sandbox", "allow-scripts");
    expect(container.textContent).toContain("Static sandbox");

    fireEvent.click(getByRole("button", { name: "Reload HTML" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });
});
