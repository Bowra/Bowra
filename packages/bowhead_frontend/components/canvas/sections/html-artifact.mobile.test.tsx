/**
 * P6/D7 — mobile embed: collapsed state shows "Open interactive" instead of an inline iframe; clicking opens the fullscreen overlay.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

// Force a mobile viewport
vi.mock("@/lib/use-media-query", () => ({ useMediaQuery: () => true }));

import { HtmlArtifactSection } from "./html-artifact";
import type { ReportSection } from "@/lib/types";

type E = Extract<ReportSection, { kind: "html_artifact" }>;

const messages = {
  html_artifact: {
    open_raw: "Open raw", load_failed: "fail {err}", loading: "Loading…",
    computed_badge: "Static content unchecked",
    injected_badge: "Generation data attached",
    reload: "Reload", unverified_badge: "Unverified data",
    expand: "Expand", collapse: "Collapse",
    footer_safe_static: "Static sandbox", footer_injected_sources: "attached {sources}",
    footer_injected_no_sources: "attached", footer_computed_sources: "unchecked {sources}",
    footer_computed_no_sources: "unchecked",
    safe_preview_badge: "Safe static preview", open_safe_preview: "Open safe preview",
    mobile_safe_hint: "safe static mobile", safe_too_large: "too large",
    safe_unsupported: "unsupported", safe_unavailable: "unavailable",
    safe_load_failed: "load failed",
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

const data: E = {
  id: "e1", kind: "html_artifact", artifact_id: "art_1",
  title: "Sim", data: { x: 1 },
} as E;

describe("HtmlArtifactSection · mobile (D7)", () => {
  it("collapsed on mobile shows Open safe preview, not an inline iframe", () => {
    const { container, getByRole } = renderCard(data);
    expect(getByRole("button", { name: /Open safe preview/ })).toBeTruthy();
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("tapping Open safe preview opens the fullscreen overlay", () => {
    const { container, getByRole } = renderCard(data);
    fireEvent.click(getByRole("button", { name: /Open safe preview/ }));
    expect(container.querySelector(".rc-embed-overlay")).not.toBeNull();
  });
});
