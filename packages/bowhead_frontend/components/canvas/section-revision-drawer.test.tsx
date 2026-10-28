import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { apiFetch } from "@/lib/api-client";
import { patchCanvasSection } from "@/lib/canvas-pm-adapter";
import { SectionRevisionDrawer, formatRevisionTime, revisionChanges, revisionTurnOrdinal } from "./section-revision-drawer";

vi.mock("@/lib/api-client", async (orig) => ({ ...(await orig<Record<string, unknown>>()), apiFetch: vi.fn() }));
vi.mock("@/lib/canvas-pm-adapter", () => ({ patchCanvasSection: vi.fn() }));

const ITEMS = [
  // Server returns newest first
  { id: "rev_3", sectionId: "sec_b", op: "patch", byTurnId: null, createdAt: "2026-09-26T06:25:00Z", prevPayload: { markdown: "约一半的增长" }, newPayload: { markdown: "约六成的增长" } },
  { id: "rev_2", sectionId: "sec_b", op: "patch", byTurnId: "t_second", turnOrdinal: 7, createdAt: "2026-09-26T06:18:00Z", prevPayload: { markdown: "x" }, newPayload: { markdown: "约一半的增长" } },
  { id: "rev_1", sectionId: "sec_b", op: "append", byTurnId: "t_first", createdAt: "2026-09-26T06:02:00Z" },
  { id: "rev_0", sectionId: "sec_b", op: "reorder", byTurnId: "machine:reorder", createdAt: "2026-09-26T06:01:00Z" },
];

function mockItems(items: unknown[] = ITEMS) {
  vi.mocked(apiFetch).mockResolvedValue({ ok: true, json: async () => ({ items }) } as Response);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SectionRevisionDrawer (C13 sheet)", () => {
  it("is a shared dialog drawer on body with a title, a Lucide close and no internal IDs", async () => {
    mockItems();
    const { container } = renderIntl(
      <div className="v2-ws-right-body">
        <SectionRevisionDrawer canvasId="canvas_1" open onClose={vi.fn()} sectionLabel={(id) => (id === "sec_b" ? "品类贡献拆解" : null)} />
      </div>,
    );
    const dialog = await screen.findByRole("dialog", { name: "修改历史" });
    expect(container.contains(dialog)).toBe(false);
    expect(within(dialog).getByRole("button", { name: "关闭" }).querySelector("svg")).not.toBeNull();
    await screen.findByText("你编辑 · 品类贡献拆解");
    expect(screen.getByText("AI 改写 · 第 7 轮 · 品类贡献拆解")).toBeInTheDocument();
    expect(screen.getByText("AI 撰写 · 品类贡献拆解")).toBeInTheDocument();
    expect(screen.getByText("自动整理 · 调整顺序 · 品类贡献拆解")).toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/📜|↺|✕|turn:|section:|sec_b|t_first|patch|append|markdown/);
  });

  it("shows the selected revision as a readable diff and restores it after confirming", async () => {
    mockItems();
    vi.mocked(patchCanvasSection).mockResolvedValue({ ok: true, rev: 12 } as never);
    const onRolledBack = vi.fn();
    const onClose = vi.fn();
    renderIntl(<SectionRevisionDrawer canvasId="canvas_1" open onClose={onClose} onRolledBack={onRolledBack} sectionLabel={() => "二、渠道贡献"} />);
    const restore = await screen.findByRole("button", { name: "恢复此版本…" });
    expect(restore).toBeDisabled();
    const rows = await screen.findAllByRole("option");
    fireEvent.click(rows[0]);
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
    expect(rows[0].querySelector("del")?.textContent).toBe("一半");
    expect(rows[0].querySelector("ins")?.textContent).toBe("六成");
    expect(within(rows[0]).getByText("正文")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "恢复此版本…" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "恢复此版本…" }));
    const confirm = await screen.findByRole("dialog", { name: "恢复到这个版本？" });
    expect(confirm.textContent).toContain("「二、渠道贡献」的当前内容会被");
    fireEvent.click(within(confirm).getByRole("button", { name: "恢复" }));
    await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledWith("canvas_1", "sec_b", { markdown: "约一半的增长" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(onRolledBack).toHaveBeenCalledOnce();
  });

  it("moves between records with arrow keys and Home/End; Enter selects", async () => {
    mockItems();
    renderIntl(<SectionRevisionDrawer canvasId="canvas_1" open onClose={vi.fn()} sectionLabel={() => "品类贡献拆解"} />);
    const list = await screen.findByRole("listbox", { name: "修改记录" });
    expect(list).toHaveAttribute("aria-orientation", "vertical");
    const rows = screen.getAllByRole("option");
    expect(rows.map((row) => row.tabIndex)).toEqual([0, -1, -1, -1]);
    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows[1]);
    fireEvent.keyDown(rows[1], { key: "End" });
    expect(document.activeElement).toBe(rows[3]);
    fireEvent.keyDown(rows[3], { key: "Home" });
    expect(document.activeElement).toBe(rows[0]);
    fireEvent.keyDown(rows[0], { key: "Enter" });
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
  });

  it("labels deleted sections “Restore this section…” and explains early records inline", async () => {
    mockItems([
      { id: "rev_del", sectionId: "sec_host", op: "delete", restorable: true, createdAt: "2026-09-26T06:00:00Z", prevPayload: { batch: "b1", batch_host: "sec_host" } },
      { id: "rev_old", sectionId: "sec_x", op: "delete", createdAt: "2026-09-25T06:00:00Z" },
    ]);
    renderIntl(<SectionRevisionDrawer canvasId="canvas_1" open onClose={vi.fn()} />);
    const rows = await screen.findAllByRole("option");
    expect(rows[1].textContent).toContain("早期记录，不能自动恢复");
    fireEvent.click(rows[0]);
    expect(screen.getByRole("button", { name: "恢复这一节…" })).toBeEnabled();
    fireEvent.click(rows[1]);
    expect(screen.getByRole("button", { name: "恢复此版本…" })).toBeDisabled();
  });

  it("read-only: no restore footer, records stay readable", async () => {
    mockItems();
    renderIntl(<SectionRevisionDrawer canvasId="canvas_1" open onClose={vi.fn()} canEdit={false} />);
    const rows = await screen.findAllByRole("option");
    expect(screen.queryByRole("button", { name: /恢复/ })).toBeNull();
    fireEvent.click(rows[0]);
    expect(rows[0].querySelector("ins")).not.toBeNull();
  });

  it("shows the empty state in plain words", async () => {
    mockItems([]);
    renderIntl(<SectionRevisionDrawer canvasId="canvas_1" open onClose={vi.fn()} />);
    expect(await screen.findByText("这一节还没有修改记录。")).toBeInTheDocument();
  });
});

describe("revision wording helpers", () => {
  it("revisionTurnOrdinal：只认服务端给的正整数序号，照原值；缺失或不合法时没有数字（前端不自己数）", () => {
    expect(revisionTurnOrdinal({ turnOrdinal: 7 })).toBe(7);
    expect(revisionTurnOrdinal({ turnOrdinal: 1 })).toBe(1);
    expect(revisionTurnOrdinal({})).toBeNull();
    expect(revisionTurnOrdinal({ turnOrdinal: null })).toBeNull();
    expect(revisionTurnOrdinal({ turnOrdinal: 0 })).toBeNull();
    expect(revisionTurnOrdinal({ turnOrdinal: -2 })).toBeNull();
    expect(revisionTurnOrdinal({ turnOrdinal: 1.5 })).toBeNull();
    expect(revisionTurnOrdinal({ turnOrdinal: "3" })).toBeNull();
  });

  it("formatRevisionTime：短写法，年份不同才带年", () => {
    const now = new Date("2026-10-01T00:00:00Z");
    expect(formatRevisionTime("2026-09-26T12:25:00Z", "zh-CN", now)).toMatch(/^9月26日 \d{2}:25$/);
    expect(formatRevisionTime("2025-09-26T12:25:00Z", "zh-CN", now)).toMatch(/^2025年9月26日 \d{2}:25$/);
    expect(formatRevisionTime(undefined, "zh-CN", now)).toBe("");
  });

  it("revisionChanges: chart type as before → after, internal fields only counted", () => {
    const kind = (value: string) => ({ bar: "柱状", line: "折线" })[value] ?? value;
    const result = revisionChanges(
      { spec: { kind: "bar", title: "Q1 渠道占比", columns: [] }, style: { pad: 1 } },
      { spec: { kind: "line", title: "Q2 渠道占比", columns: [{ key: "x" }] }, style: { pad: 2 } },
      kind,
    );
    expect(result.changes.map((change) => change.label)).toEqual(["chart_type", "chart_title"]);
    expect(result.changes[0].value).toEqual({ kind: "text", text: "柱状 → 折线" });
    expect(result.other).toBe(2);
  });
});
