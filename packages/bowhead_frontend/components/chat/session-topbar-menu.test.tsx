/**
 * Session ⋯ menu in the title bar (Chat Cards Spec D12): "Copy Diagnostic Information" (menu item, Title Case) copies the same text
 * the title-bar chip used to (ids first, transport line second) and confirms with a toast.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { SessionTopbarMenu, sessionDiagnosticText } from "./session-topbar-menu";

const mocks = vi.hoisted(() => ({ copy: vi.fn(), toast: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyTextWithFallback: (...args: unknown[]) => mocks.copy(...args) }));
vi.mock("@/lib/use-toast", () => ({ toast: (...args: unknown[]) => mocks.toast(...args) }));

afterEach(() => {
  mocks.copy.mockReset();
  mocks.toast.mockReset();
});

describe("SessionTopbarMenu", () => {
  it("keeps context maintenance in the shared menu and explains its busy state", () => {
    const compact = vi.fn();
    renderIntl(<SessionTopbarMenu sessionId="s_1" onCompactContext={compact} compactDisabledReason="请先完成当前任务。" />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    const item = screen.getByRole("menuitem", { name: "整理上下文" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    expect(item.getAttribute("title")).toBe("请先完成当前任务。");
    fireEvent.click(item);
    expect(compact).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
  });
  it("keeps the session and turn ids first", () => {
    expect(sessionDiagnosticText("s_1", "t_2")).toBe("s_1 · t_2");
    expect(sessionDiagnosticText("s_1", null)).toBe("s_1");
  });

  it("copies the diagnostic information from the ⋯ menu and confirms with a toast", async () => {
    mocks.copy.mockResolvedValue(true);
    renderIntl(<SessionTopbarMenu sessionId="s_bf39" turnId="t_3dff" />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "复制诊断信息" }));
    await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith("s_bf39 · t_3dff", expect.any(String)));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith({ kind: "success", text: "已复制诊断信息" }));
  });

  it("does not claim success when the clipboard falls back to the prompt", async () => {
    mocks.copy.mockResolvedValue(false);
    renderIntl(<SessionTopbarMenu sessionId="s_1" />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "复制诊断信息" }));
    await waitFor(() => expect(mocks.copy).toHaveBeenCalled());
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("offers pin and archive, with archive unavailable while the session runs", () => {
    const onArchive = vi.fn();
    renderIntl(
      <SessionTopbarMenu
        sessionId="s_1"
        pinned={false}
        onTogglePin={vi.fn()}
        onArchive={onArchive}
        archiveDisabledReason="这项工作仍在运行，完成或停止后才能归档。"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    const labels = screen.getAllByRole("menuitem").map((item) => item.textContent);
    expect(labels).toEqual(["置顶", "归档会话", "复制诊断信息"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "归档会话" }));
    expect(onArchive).not.toHaveBeenCalled();
  });

  it("puts Rename first, matching the session row menu", async () => {
    const onRename = vi.fn();
    renderIntl(<SessionTopbarMenu sessionId="s_1" pinned onTogglePin={vi.fn()} onArchive={vi.fn()} onRename={onRename} />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "重命名…",
      "取消置顶",
      "归档会话",
      "复制诊断信息",
    ]);
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名…" }));
    await waitFor(() => expect(onRename).toHaveBeenCalledTimes(1));
  });

  it("matches the row menu: shared items first, then a separator and the destructive Delete Session", () => {
    renderIntl(
      <SessionTopbarMenu
        sessionId="s_1"
        title="华东 Q3 下滑归因"
        pinned={false}
        onTogglePin={vi.fn()}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        onRename={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作：华东 Q3 下滑归因" }));
    const menu = screen.getByRole("menu");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "重命名…",
      "置顶",
      "归档会话",
      "复制诊断信息",
      "删除会话",
    ]);
    const separator = menu.querySelector("[role='separator']");
    expect(separator?.nextElementSibling?.textContent).toBe("删除会话");
    expect(screen.getByRole("menuitem", { name: "删除会话" }).getAttribute("data-danger")).toBe("true");
  });

  it("deletes without confirmation and offers the same eight-second Undo as the row", async () => {
    const onDelete = vi.fn().mockResolvedValue("success");
    const onUndelete = vi.fn().mockResolvedValue(undefined);
    renderIntl(<SessionTopbarMenu sessionId="s_1" title="华东 Q3 下滑归因" onDelete={onDelete} onUndelete={onUndelete} />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作：华东 Q3 下滑归因" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
    const call = mocks.toast.mock.calls[0]![0] as { undo: boolean; durationMs: number; text: string; action: { onAction: () => Promise<void> } };
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(call).toMatchObject({ kind: "success", undo: true, durationMs: 8_000 });
    expect(call.text).toContain("华东 Q3 下滑归因");
    await call.action.onAction();
    expect(onUndelete).toHaveBeenCalledTimes(1);
  });

  it("keeps Delete unavailable with its reason while the session runs", () => {
    const onDelete = vi.fn();
    renderIntl(<SessionTopbarMenu sessionId="s_1" onDelete={onDelete} deleteDisabledReason="这项工作仍在运行，请先完成或停止。" />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
    expect(onDelete).not.toHaveBeenCalled();
  });

  it("does not toast when the page cancels the delete", async () => {
    const onDelete = vi.fn().mockResolvedValue("cancelled");
    renderIntl(<SessionTopbarMenu sessionId="s_1" onDelete={onDelete} />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalled());
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("sends one request while a delete is in flight", async () => {
    let finish!: (value: "success") => void;
    const onDelete = vi.fn(() => new Promise<"success">((resolve) => { finish = resolve; }));
    renderIntl(<SessionTopbarMenu sessionId="s_1" onDelete={onDelete} />);
    for (let attempt = 0; attempt < 2; attempt += 1) {
      fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
    }
    expect(onDelete).toHaveBeenCalledTimes(1);
    finish("success");
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
  });

  it("explains a refused delete with the running-session reason", async () => {
    const { SessionArchiveError } = await import("@/lib/api");
    const error = Object.assign(Object.create(SessionArchiveError.prototype), { code: "session_delete_active_turn" });
    const onDelete = vi.fn().mockRejectedValue(error);
    renderIntl(<SessionTopbarMenu sessionId="s_1" onDelete={onDelete} />);
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除会话" }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
    expect(mocks.toast.mock.calls[0]![0]).toMatchObject({ kind: "error" });
    expect((mocks.toast.mock.calls[0]![0] as { text: string }).text).toContain("仍在运行");
  });
});
