/**
 * Composer notice slot (Chat Cards Spec §9, D9): one notice at a time, error > warning > neutral,
 * error is an alert and the rest are status, every hidden notice is reachable with its fix.
 */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ComposerNotices, orderComposerNotices, type ComposerNoticeItem } from "./composer-notices";

const neutral: ComposerNoticeItem = { id: "drive", tone: "neutral", text: "从 Google Drive 选择前，需要先连接 Google。", textId: "n-drive" };
const warning: ComposerNoticeItem = {
  id: "conflict",
  tone: "warning",
  text: "修改文档时不能带新附件。",
  textId: "n-conflict",
  actions: [{ label: "移除附件", onSelect: vi.fn() }],
};
const error: ComposerNoticeItem = { id: "uncertain", tone: "error", text: "没能确认这条消息是否已发出，内容已保留。", textId: "n-error" };

describe("ComposerNotices", () => {
  it("orders by tone, then by the given order", () => {
    expect(orderComposerNotices([neutral, warning, error]).map((n) => n.id)).toEqual(["uncertain", "conflict", "drive"]);
  });

  it("shows one notice; neutral and warning notices are status, not alerts", () => {
    const { container } = renderIntl(<ComposerNotices notices={[neutral, warning]} />);
    const shown = screen.getByRole("status");
    expect(shown.textContent).toBe("修改文档时不能带新附件。");
    expect(shown.id).toBe("n-conflict");
    expect(container.querySelector(".ui-card-notice")?.getAttribute("data-tone")).toBe("warning");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("从 Google Drive 选择前，需要先连接 Google。")).toBeNull();
  });

  it("runs the shown notice's fix", () => {
    const onSelect = vi.fn();
    renderIntl(<ComposerNotices notices={[{ ...warning, actions: [{ label: "移除附件", onSelect }] }]} />);
    fireEvent.click(screen.getByRole("button", { name: "移除附件" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("makes only an error an alert", () => {
    renderIntl(<ComposerNotices notices={[neutral, error]} />);
    expect(screen.getByRole("alert").textContent).toBe("没能确认这条消息是否已发出，内容已保留。");
  });

  it("lists the other notices behind 'N more notices' with their fixes", () => {
    const onSelect = vi.fn();
    renderIntl(<ComposerNotices notices={[error, { ...warning, actions: [{ label: "移除附件", onSelect }] }, neutral]} />);
    const more = screen.getByRole("button", { name: "还有 2 条提示" });
    expect(more.getAttribute("aria-haspopup")).toBe("menu");
    fireEvent.click(more);
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "修改文档时不能带新附件。移除附件",
      "从 Google Drive 选择前，需要先连接 Google。",
    ]);
    fireEvent.click(items[0]);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("renders nothing without notices", () => {
    const { container } = renderIntl(<ComposerNotices notices={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
