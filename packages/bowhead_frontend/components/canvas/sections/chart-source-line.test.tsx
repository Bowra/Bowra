/**
 * Batch 0, 0-D (chart-table-ux tech-design §1.4) — two-state rendering for source-line [N].
 *
 * The projection layer once hardcoded ordinal=1 for chart source_meta (all 37 source lines in a real production report
 * showed [1]). After the fix, send ordinal only when it matches a real source-list ordinal:
 *   With ordinal → [N] + label; without ordinal → label only, never a fabricated number.
 */

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartSourceLine } from "./chart-source-line";
import type { SourceRef } from "@/lib/canvas-pm-adapter";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

afterEach(() => cleanup());

const BASE: SourceRef = {
  id: "source-ref-v1:abc",
  source_alias: "source-ref-v1:abc",
  access_state: "linked_accessible",
  safe_label: "BI 品牌维度查询",
} as SourceRef;

describe("ChartSourceLine ordinal 两态", () => {
  it("有 ordinal → 渲 [N]", () => {
    render(<ChartSourceLine source={{ ...BASE, ordinal: 3 }} />);
    expect(screen.getByText("[3]")).toBeInTheDocument();
  });

  it("★负对照：无 ordinal → 不渲任何方括号序号，只显来源名", () => {
    const { container } = render(<ChartSourceLine source={BASE} />);
    expect(container.querySelector(".src-n")).toBeNull();
    // label remains (safe projection shows either label or verification-placeholder copy)
    expect(container.querySelector(".src-link")).not.toBeNull();
  });

  it("the source card mounts on body above the content layer; Esc closes it and returns to the source line; clicks inside the card do not re-trigger the line", async () => {
    const { container } = render(<ChartSourceLine source={{ ...BASE, ordinal: 2 }} />);
    const line = container.querySelector<HTMLElement>(".rc-source")!;
    line.focus();
    fireEvent.keyDown(line, { key: "Enter" });
    const card = await waitFor(() => {
      const el = document.querySelector<HTMLElement>(".rc-prov");
      expect(el).not.toBeNull();
      return el!;
    });
    expect(line.contains(card)).toBe(false);
    expect(card.style.zIndex).toBe("var(--z-menu)");
    expect(card).toHaveAttribute("role", "dialog");
    expect(line).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(card);
    expect(document.querySelector(".rc-prov")).not.toBeNull();
    fireEvent.keyDown(card, { key: "Escape" });
    await waitFor(() => expect(document.querySelector(".rc-prov")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(line));
  });

  it("无 source 的空态保持「– no_source」不受影响", () => {
    const { container } = render(<ChartSourceLine source={null} />);
    expect(container.querySelector(".rc-source.none")).not.toBeNull();
  });
});

it.each([false, true])("keeps distinct source destinations and merges identical ones (distinct=%s)", async (distinct) => {
  const { SourceAccessRegistryProvider } = await import("@/lib/source-access-registry");
  const source = { ...BASE, id: "source-ref-v1:" + "a".repeat(43), source_alias: "source-ref-v1:" + "a".repeat(43), safe_preview_url: "/api/v1/source/preview", safe_url: "/api/v1/source/download" };
  const evidenceSource = distinct ? { ...source, id: "source-ref-v1:" + "b".repeat(43), source_alias: "source-ref-v1:" + "b".repeat(43), safe_url: "/api/v1/source/other" } : source;
  const excerpt = JSON.stringify({ role: "coverage", time_key: "month", measure_keys: ["revenue"], text: "Partial reporting period", lower: "2026-01", upper: "2026-04" });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
  try {
    const { container } = render(<SourceAccessRegistryProvider><ChartSourceLine source={source} measureLabels={{ revenue: "Revenue" }} notes={[{ id: "scope", measure_keys: ["revenue"], excerpt, source_ref: evidenceSource, availability: "available" }]} /></SourceAccessRegistryProvider>);
    fireEvent.click(container.querySelector(".rc-source")!);
    expect(await screen.findByText("Partial reporting period")).toBeInTheDocument();
    expect(screen.getByText("2026-01 – 2026-04")).toBeInTheDocument();
    expect(screen.queryByText(excerpt)).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "open_source" })).toHaveLength(distinct ? 2 : 1);
    expect(screen.getAllByRole("link", { name: "open_source" })[0]).toHaveAttribute("href", source.safe_url);
  } finally { vi.unstubAllGlobals(); }
});
