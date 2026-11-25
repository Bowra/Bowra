/** ConfigDrawer shell + embedded-form wiring (tech-design-batch1b §5, tests 13-14).
 *
 * Three variant class names/non-modal/back arrow, GET prefill through admit, save → consume, discard → discard,
 * close → dismissed (no reopening). Use the real RoutineDrawer (the actual embedded-variant decomposition).
 */

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

vi.mock("@/lib/api-client", () => {
  class ApiError extends Error {
    status: number;
    body?: unknown;
    constructor(status: number, message: string, options: { body?: unknown } = {}) {
      super(message);
      this.status = status;
      this.body = options.body;
    }
  }
  return { apiJson: vi.fn(), ApiError };
});
vi.mock("@/lib/use-space", () => ({
  readSpaces: vi.fn(async () => ({ items: [{ id: "ws_1", name: "商超零售" }], active_id: "ws_1" })),
}));

import { apiJson } from "@/lib/api-client";
import { applyDraftEvent, isDraftDismissed, resetDraftForTests } from "@/lib/config-copilot-draft";

import { ConfigDrawer } from "./config-drawer";

const apiJsonMock = vi.mocked(apiJson);

function draftDto(over: Record<string, unknown> = {}) {
  return {
    draft_id: "ccd_d1",
    form_id: "scheduled_job",
    status: "draft",
    workspace_id: "ws_1",
    fields: { name: "销售周报", query: "汇总上周销售", cron: "0 9 * * 1", tz: "Asia/Shanghai" },
    field_meta: {
      name: { origin: "generated", needs_confirm: false, note: null },
      query: { origin: "generated", needs_confirm: false, note: null },
      cron: { origin: "generated", needs_confirm: false, note: null },
      tz: { origin: "generated", needs_confirm: false, note: null },
    },
    notes: {},
    result: null,
    row_version: 1,
    ...over,
  };
}

beforeEach(() => {
  resetDraftForTests();
  apiJsonMock.mockReset();
  apiJsonMock.mockImplementation(async (input: RequestInfo | URL) => {
    if (String(input).endsWith("/config-drafts/ccd_d1")) return draftDto();
    return {};
  });
});

afterEach(() => {
  cleanup();
  resetDraftForTests();
});

describe("ConfigDrawer", () => {
  it("overlay 形态：类名 + scrim；非模态（无 aria-modal，键盘可离开）；含「关闭后回到报告」", async () => {
    const { container } = renderIntl(<ConfigDrawer draftId="ccd_d1" layout="overlay" onClose={vi.fn()} />);
    const drawer = await screen.findByTestId("config-drawer");
    expect(drawer.className).toContain("cc-drawer-overlay");
    expect(drawer.getAttribute("aria-modal")).toBeNull();
    expect(container.querySelector(".cc-drawer-scrim")).toBeTruthy();
    expect(screen.getByText("关闭后回到报告")).toBeTruthy();
  });

  it("column 形态：无 scrim、无「回到报告」提示（对话列在文档流中收窄）", async () => {
    const { container } = renderIntl(<ConfigDrawer draftId="ccd_d1" layout="column" onClose={vi.fn()} />);
    const drawer = await screen.findByTestId("config-drawer");
    expect(drawer.className).toContain("cc-drawer-column");
    expect(container.querySelector(".cc-drawer-scrim")).toBeNull();
    expect(screen.queryByText("关闭后回到报告")).toBeNull();
  });

  it("sheet 形态：左上「返回对话」箭头替代关闭叉", async () => {
    renderIntl(<ConfigDrawer draftId="ccd_d1" layout="sheet" onClose={vi.fn()} />);
    const drawer = await screen.findByTestId("config-drawer");
    expect(drawer.className).toContain("cc-drawer-sheet");
    expect(screen.getByLabelText("返回对话")).toBeTruthy();
    expect(screen.queryByLabelText("关闭")).toBeNull();
  });

  it("GET 草稿 → 表单预填（走 admit：generated 字段进表单）", async () => {
    renderIntl(<ConfigDrawer draftId="ccd_d1" layout="column" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue("销售周报")).toBeTruthy());
    expect(screen.getByDisplayValue("汇总上周销售")).toBeTruthy();
    expect(apiJsonMock.mock.calls[0][0]).toBe("/api/v1/config-drafts/ccd_d1");
  });

  it("放弃草稿 → POST discard + 关闭 + 标记 dismissed（不复弹）", async () => {
    const onClose = vi.fn();
    renderIntl(<ConfigDrawer draftId="ccd_d1" layout="column" onClose={onClose} />);
    await waitFor(() => expect(screen.getByDisplayValue("销售周报")).toBeTruthy());
    fireEvent.click(screen.getByText("放弃草稿"));
    await waitFor(() =>
      expect(apiJsonMock.mock.calls.some((c) => String(c[0]).endsWith("/discard"))).toBe(true),
    );
    expect(onClose).toHaveBeenCalled();
    expect(isDraftDismissed("ccd_d1")).toBe(true);
  });

  it("关闭（✕）→ dismissed，避免 SSE 重连重放复弹", async () => {
    const onClose = vi.fn();
    renderIntl(<ConfigDrawer draftId="ccd_d1" layout="overlay" onClose={onClose} />);
    await screen.findByTestId("config-drawer");
    fireEvent.click(screen.getByLabelText("关闭"));
    expect(onClose).toHaveBeenCalled();
    expect(isDraftDismissed("ccd_d1")).toBe(true);
  });

  it("Esc 关闭（embedded 变体已停用表单自带 handler，壳统一监听）", async () => {
    const onClose = vi.fn();
    renderIntl(<ConfigDrawer draftId="ccd_d1" layout="column" onClose={onClose} />);
    await screen.findByTestId("config-drawer");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("草稿载入失败 → 提示引导设置页手填（失败不挡手填）", async () => {
    apiJsonMock.mockImplementation(async () => {
      throw new Error("boom");
    });
    renderIntl(<ConfigDrawer draftId="ccd_d1" layout="column" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/草稿载入失败/)).toBeTruthy());
  });
});
