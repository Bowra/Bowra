import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { DesignSection } from "./design";
import { CanvasContextProvider } from "@/lib/canvas-pm-adapter/canvas-context";
import type { ReportSection } from "@/lib/types";

type DesignData = Extract<ReportSection, { kind: "design" }>;

const baseDesign: DesignData = {
  id: "sec_design",
  kind: "design",
  title: "夏季新品促销设计稿",
  size_preset: "portrait_3_4",
  canvas_width: 1080,
  canvas_height: 1440,
  background: {
    type: "artifact",
    artifact_id: "art_design_bg123",
    fit: "cover",
    opacity: 1,
  },
  layers: [
    {
      id: "headline",
      type: "text",
      text: "夏日新品",
      x: 0.1,
      y: 0.12,
      w: 0.8,
      h: 0.12,
      font_family: "Noto Sans SC",
      font_size: 72,
      color: "#111111",
      align: "center",
      rotation: 0,
      z: 10,
      locked: false,
    },
    {
      id: "cta",
      type: "text",
      text: "第二杯半价",
      x: 0.18,
      y: 0.76,
      w: 0.64,
      h: 0.08,
      font_family: "Noto Sans SC",
      font_size: 48,
      color: "#ffffff",
      align: "center",
      rotation: 0,
      z: 20,
      locked: false,
    },
  ],
  flattened_artifact_id: null,
  flattened_source_rev: null,
  flattened_at: null,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mockOverflowForText(text: string): () => void {
  const originalScrollHeight = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollHeight",
  );
  const originalClientHeight = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "clientHeight",
  );
  const originalScrollWidth = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollWidth",
  );
  const originalClientWidth = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "clientWidth",
  );

  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get() {
      return this.textContent?.trim() === text ? 48 : 20;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return 20;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
    configurable: true,
    get() {
      return 80;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get() {
      return 80;
    },
  });

  return () => {
    if (originalScrollHeight) {
      Object.defineProperty(HTMLElement.prototype, "scrollHeight", originalScrollHeight);
    } else {
      Reflect.deleteProperty(HTMLElement.prototype, "scrollHeight");
    }
    if (originalClientHeight) {
      Object.defineProperty(HTMLElement.prototype, "clientHeight", originalClientHeight);
    } else {
      Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
    }
    if (originalScrollWidth) {
      Object.defineProperty(HTMLElement.prototype, "scrollWidth", originalScrollWidth);
    } else {
      Reflect.deleteProperty(HTMLElement.prototype, "scrollWidth");
    }
    if (originalClientWidth) {
      Object.defineProperty(HTMLElement.prototype, "clientWidth", originalClientWidth);
    } else {
      Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
    }
  };
}

describe("DesignSection", () => {
  it("renders background artifact and text layers in read mode", () => {
    const { container } = renderIntl(<DesignSection data={baseDesign} />);

    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe(
      "/api/v1/files/art_design_bg123?disposition=inline",
    );
    expect(screen.getByText("夏日新品")).toBeTruthy();
    expect(screen.getByText("第二杯半价")).toBeTruthy();
    expect(screen.queryByRole("toolbar", { name: "设计文字样式" })).toBeNull();
  });

  it("content-permissions: 报告语境(sessionId)下背景走权限中介端点", () => {
    // Persisted-report sections get sessionId through CanvasContextProvider → artifact fetching uses the permission intermediary,
    // allowing share recipients to see poster backgrounds too (otherwise owner-gated /api/v1/files returns 403).
    const { container } = renderIntl(
      <CanvasContextProvider sections={[]} sessionId="ses_demo">
        <DesignSection data={baseDesign} />
      </CanvasContextProvider>,
    );
    expect(container.querySelector("img")?.getAttribute("src")).toBe(
      "/api/v1/canvas/by-session/ses_demo/artifacts/art_design_bg123?disposition=inline",
    );
  });

  it("emits text and style changes in edit mode", () => {
    const onLayerTextChange = vi.fn();
    const onLayerStyleChange = vi.fn();
    renderIntl(
      <DesignSection
        data={baseDesign}
        isEditMode
        selectedLayerId="headline"
        onLayerTextChange={onLayerTextChange}
        onLayerStyleChange={onLayerStyleChange}
      />,
    );

    const headline = screen.getByText("夏日新品");
    headline.textContent = "春日新品";
    fireEvent.blur(headline);
    expect(onLayerTextChange).toHaveBeenCalledWith("headline", "春日新品");

    fireEvent.change(screen.getByLabelText("文字颜色"), {
      target: { value: "#ff0000" },
    });
    expect(onLayerStyleChange).toHaveBeenCalledWith("headline", { color: "#ff0000" });

    fireEvent.change(screen.getByLabelText("字号"), {
      target: { value: "88" },
    });
    expect(onLayerStyleChange).toHaveBeenCalledWith("headline", { font_size: 88 });

    fireEvent.click(screen.getByRole("button", { name: "文字左对齐" }));
    expect(onLayerStyleChange).toHaveBeenCalledWith("headline", { align: "left" });
  });

  it("does not expose text editing controls for locked layers", () => {
    const onLayerTextChange = vi.fn();
    const onLayerStyleChange = vi.fn();
    const lockedDesign: DesignData = {
      ...baseDesign,
      layers: baseDesign.layers.map((layer) => (
        layer.id === "headline" ? { ...layer, locked: true } : layer
      )),
    };
    renderIntl(
      <DesignSection
        data={lockedDesign}
        isEditMode
        selectedLayerId="headline"
        onLayerTextChange={onLayerTextChange}
        onLayerStyleChange={onLayerStyleChange}
      />,
    );

    expect(screen.queryByRole("toolbar", { name: "设计文字样式" })).toBeNull();
    const headline = screen.getByText("夏日新品");
    expect(headline.getAttribute("contenteditable")).toBe("false");
    headline.textContent = "锁定层不应修改";
    fireEvent.blur(headline);
    expect(onLayerTextChange).not.toHaveBeenCalled();
    expect(onLayerStyleChange).not.toHaveBeenCalled();
  });

  it("warns when the selected text layer overflows its box", () => {
    const restoreMetrics = mockOverflowForText("夏日新品");
    try {
      renderIntl(
        <DesignSection
          data={baseDesign}
          isEditMode
          selectedLayerId="headline"
          onLayerTextChange={vi.fn()}
          onLayerStyleChange={vi.fn()}
        />,
      );

      expect(screen.getByRole("status").textContent).toContain("文字超出文本框");
      expect(screen.getByLabelText("文字超出文本框")).toBeTruthy();
    } finally {
      restoreMetrics();
    }
  });
});
