import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { NodeViewProps } from "@tiptap/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { flattenCanvasDesign } from "@/lib/api";
// The two telemetry channels were consolidated into `@/lib/metrics` (2026-08-08); interactions use trackInteraction.
import { trackInteraction as track } from "@/lib/metrics";
import type { ReportSection } from "@/lib/types";
import { CanvasContextProvider } from "../canvas-context";
import { DesignEditNodeView } from "./atomic";

vi.mock("@/lib/api", () => ({
  flattenCanvasDesign: vi.fn(),
}));

vi.mock("@/lib/metrics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/metrics")>()),
  trackInteraction: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

type DesignSectionData = Extract<ReportSection, { kind: "design" }>;

const baseDesign: DesignSectionData = {
  id: "sec_design",
  kind: "design",
  rev: 3,
  title: "夏季促销海报",
  size_preset: "portrait_3_4",
  canvas_width: 1080,
  canvas_height: 1440,
  background: {
    type: "artifact",
    artifact_id: "art_bg123456789",
    fit: "cover",
    opacity: 1,
  },
  layers: [
    {
      id: "headline",
      type: "text",
      text: "夏日冰爽",
      x: 0.12,
      y: 0.12,
      w: 0.76,
      h: 0.12,
      font_family: "Noto Sans SC",
      font_size: 72,
      color: "#111111",
      align: "center",
      z: 10,
    },
  ],
  flattened_artifact_id: null,
  flattened_source_rev: null,
  flattened_at: null,
};

function payloadFromSection(section: DesignSectionData): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(section).filter(([key]) => key !== "id" && key !== "kind"),
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.location.hash = "";
});

describe("DesignEditNodeView flatten bridge", () => {
  it("flushes pending edits, exports with latest rev, and replaces local section payload", async () => {
    const opOrder: string[] = [];
    const flushPendingEdits = vi.fn(async () => {
      opOrder.push("flush");
      return { ok: true as const, saved_revisions: {} };
    });
    const acceptServerSection = vi.fn(() => {
      opOrder.push("accept");
    });
    const updateAttributes = vi.fn(() => {
      opOrder.push("update");
    });
    const flattenedSection: DesignSectionData = {
      ...baseDesign,
      rev: 4,
      flattened_artifact_id: "art_flat123456",
      flattened_source_rev: 3,
      flattened_at: "2026-06-10T00:00:00Z",
    };
    vi.mocked(flattenCanvasDesign).mockImplementation(async () => {
      opOrder.push("flatten");
      return {
        artifact_id: "art_flat123456",
        url: "#download",
        section: flattenedSection,
        rev: 4,
      };
    });
    const nodeViewProps = {
      node: {
        attrs: {
          sectionId: baseDesign.id,
          payload: payloadFromSection(baseDesign),
        },
      },
      updateAttributes,
    } as unknown as NodeViewProps;

    render(
      <CanvasContextProvider
        sections={[baseDesign]}
        canvasId="cv_design"
        isEditMode
        flushPendingEdits={flushPendingEdits}
        getSectionRev={() => 3}
        acceptServerSection={acceptServerSection}
      >
        <DesignEditNodeView {...nodeViewProps} />
      </CanvasContextProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: /PNG/ }));

    await waitFor(() => {
      expect(flattenCanvasDesign).toHaveBeenCalledTimes(1);
    });
    expect(opOrder).toEqual(["flush", "flatten", "accept", "update"]);
    expect(flattenCanvasDesign).toHaveBeenCalledWith("cv_design", "sec_design", 3);
    expect(acceptServerSection).toHaveBeenCalledWith(flattenedSection);
    expect(updateAttributes).toHaveBeenCalledWith({
      payload: expect.objectContaining({
        flattened_artifact_id: "art_flat123456",
        flattened_source_rev: 3,
        flattened_at: "2026-06-10T00:00:00Z",
      }),
    });
    expect(track).toHaveBeenCalledWith("bowhead_design_flatten_ui_total", {
      action: "start",
    });
    expect(track).toHaveBeenCalledWith("bowhead_design_flatten_ui_total", {
      action: "done",
    });
  });

  it("invalidates cached flattened PNG when editing a design layer", () => {
    const updateAttributes = vi.fn();
    const cachedDesign: DesignSectionData = {
      ...baseDesign,
      flattened_artifact_id: "art_flat123456",
      flattened_source_rev: 2,
      flattened_at: "2026-06-10T00:00:00Z",
    };
    const nodeViewProps = {
      node: {
        attrs: {
          sectionId: cachedDesign.id,
          payload: payloadFromSection(cachedDesign),
        },
      },
      updateAttributes,
    } as unknown as NodeViewProps;

    render(
      <CanvasContextProvider
        sections={[cachedDesign]}
        canvasId="cv_design"
        isEditMode
      >
        <DesignEditNodeView {...nodeViewProps} />
      </CanvasContextProvider>,
    );

    const headline = screen.getByText("夏日冰爽");
    headline.textContent = "夏日新品";
    fireEvent.blur(headline);

    expect(updateAttributes).toHaveBeenCalledWith({
      payload: expect.objectContaining({
        layers: [
          expect.objectContaining({ id: "headline", text: "夏日新品" }),
        ],
        flattened_artifact_id: null,
        flattened_source_rev: null,
        flattened_at: null,
      }),
    });
  });
});
