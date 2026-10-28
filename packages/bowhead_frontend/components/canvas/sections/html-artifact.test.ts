import "@testing-library/jest-dom/vitest";

import { createElement } from "react";
import { cleanup, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";

import { HtmlArtifactSection } from "./html-artifact";

type HtmlArtifact = Extract<ReportSection, { kind: "html_artifact" }>;

function makeArtifact(overrides: Partial<HtmlArtifact> = {}): HtmlArtifact {
  return {
    id: "s1",
    kind: "html_artifact",
    artifact_id: "art_1",
    title: "T",
    ...overrides,
  } as HtmlArtifact;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HtmlArtifactSection", () => {
  it.each([
    [{ data: { value: 391 }, sources: [1, 1, 0, "2"] }, "已附带生成数据 · 附带来源引用 [1]"],
    [{ data: { value: 391 } }, "已附带生成数据 · 未提供来源引用"],
    [{ data: { value: 391 }, unverified: true, sources: [1] }, "静态内容未与工具输出核对 · 附带来源引用 [1]"],
    [{ data: {}, unverified: false }, "静态内容未与工具输出核对"],
  ])("renders honest static provenance for %j", (overrides, expected) => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
    const { container } = renderIntl(createElement(HtmlArtifactSection, {
      data: makeArtifact(overrides as Partial<HtmlArtifact>),
    }));
    expect(container.textContent).toContain(expected);
    expect(container.textContent).not.toMatch(/已注入数据|交互块内计算/);
  });

  it("always renders the sanitized frame without script permissions", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("<html><body>static report</body></html>", {
        status: 200,
        headers: { "content-type": "text/html" },
      }),
    ));
    const { container } = renderIntl(createElement(HtmlArtifactSection, {
      data: makeArtifact({ data: { value: 391 } }),
    }));

    await waitFor(() => expect(container.querySelector("iframe")).not.toBeNull());
    expect(container.querySelector("iframe")).toHaveAttribute("sandbox", "");
    expect(container.innerHTML).not.toContain("allow-scripts");
    expect(container.textContent).toContain("安全静态预览");
  });
});
