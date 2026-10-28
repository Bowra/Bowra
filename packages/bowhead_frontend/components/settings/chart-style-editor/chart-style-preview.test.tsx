import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { useContext } from "react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { chartThemeBaseForMode } from "@/lib/chart-theme";
import messages from "@/messages/zh-CN.json";

import { ChartStylePreview } from "./chart-style-preview";

import { ChartMenuRootContext } from "@/components/canvas/sections/chart-menu-root";

function ChartBlockStub() {
  const menuRoot = useContext(ChartMenuRootContext);
  return <div data-testid="chart-block" data-menu-root={menuRoot?.className ?? String(menuRoot)} />;
}

vi.mock("@/components/canvas/sections/chart-block", () => ({
  ChartBlock: () => <ChartBlockStub />,
}));

function renderPreview(view?: "candidate" | "loading" | "lastValid" | "blocked") {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messages}>
      <ChartStylePreview themeForMode={chartThemeBaseForMode} view={view} />
    </NextIntlClientProvider>,
  );
}

afterEach(cleanup);

// docs/sprints/brand-appearance-repair-2026-09-09/spike.md §3 B1: the pane is
// titled "生产渲染器 · 真实图表预览", so every state that is NOT the current
// candidate has to say so on screen.
describe("ChartStylePreview 的四种状态", () => {
  it("正常候选：不打任何状态标", () => {
    renderPreview("candidate");

    expect(screen.queryByRole("status")).toBeNull();
  });

  it("在途：沿用「预览更新中…」", () => {
    renderPreview("loading");

    expect(screen.getByRole("status").textContent).toContain("预览更新中");
  });

  it("显示旧候选：明说当前候选没过检查", () => {
    renderPreview("lastValid");

    expect(screen.getByRole("status").textContent).toContain("上一版通过校验的候选");
  });

  it("阻断态：不渲染图表，只说明原因", () => {
    renderPreview("blocked");

    expect(screen.queryByTestId("chart-block")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("无法生成预览");
  });

  it("默认按正常候选处理，不会把成员侧的预览误标成异常", () => {
    renderPreview();

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByTestId("chart-block")).toBeTruthy();
  });
});

describe("ChartStylePreview 图表菜单", () => {
  it("图表里的菜单挂进预览区，读得到预览区内联的配色变量", async () => {
    renderPreview("candidate");

    await waitFor(() =>
      expect(screen.getByTestId("chart-block").getAttribute("data-menu-root")).toContain("cs-preview__viewport"),
    );
  });
});
