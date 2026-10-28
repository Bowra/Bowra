import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  fetchSessions,
  SessionArchiveError,
} from "@/lib/api";
import { SESSION_LIST_CHANGED_EVENT } from "@/lib/session-list-events";
import { resetSessionTitleOverridesForTest } from "@/lib/session-rename";
import type { SessionGroup } from "@/lib/types";

import { WorkRecovery } from "./work-recovery";

const { toastMock } = vi.hoisted(() => ({
  toastMock: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchSessions: vi.fn(),
  };
});

vi.mock("@/lib/use-toast", () => ({
  toast: toastMock,
}));

const ACTIVE_GROUPS: SessionGroup[] = [
  {
    id: "today",
    label: "TODAY",
    items: [
      {
        id: "s_done",
        title: "季度复盘",
        intents: [],
        time: "2m",
        state: "done",
        pinned: true,
        unread: true,
      },
      {
        id: "s_running",
        title: "渠道分析",
        intents: [],
        time: "just now",
        state: "active",
      },
    ],
  },
];

function renderRecovery(
  overrides: Partial<React.ComponentProps<typeof WorkRecovery>> = {},
  locale: "zh-CN" | "en-US" = "zh-CN",
) {
  const props: React.ComponentProps<typeof WorkRecovery> = {
    groups: ACTIVE_GROUPS,
    currentSessionId: null,
    query: "",
    onQueryChange: vi.fn(),
    onPick: vi.fn(),
    onTogglePin: vi.fn(),
    ...overrides,
  };
  return { ...renderIntl(<WorkRecovery {...props} />, { locale }), props };
}

async function openSessionMenu(title: string): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: `更多会话操作：${title}` }));
  await screen.findByRole("menu");
}

async function chooseSessionAction(title: string, action: string): Promise<void> {
  await openSessionMenu(title);
  fireEvent.click(screen.getByRole("menuitem", { name: action }));
}

function sessionGroups(count: number): SessionGroup[] {
  return [
    {
      id: "today",
      label: "TODAY",
      items: Array.from({ length: count }, (_, index) => ({
        id: `s_${index}`,
        title: `会话 ${index}`,
        intents: [],
        time: "2m",
        state: "done" as const,
      })),
    },
  ];
}

describe("WorkRecovery session lifecycle", () => {
  beforeEach(() => {
    toastMock.mockReset();
    vi.mocked(fetchSessions).mockReset().mockResolvedValue({
      groups: [],
      archived_count: 0,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps the server label for a group id without a catalog key instead of showing the raw key", () => {
    renderRecovery({
      groups: [{ ...ACTIVE_GROUPS[0]!, id: "yesterday", label: "昨天" }, { ...sessionGroups(1)[0]!, id: "today", label: "x" }],
    });
    expect(screen.getByText("昨天")).toBeTruthy();
    expect(screen.queryByText(/rail_group_/)).toBeNull();
  });

  it("keeps lifecycle state textual", () => {
    renderRecovery();

    expect(screen.getByText("进行中")).not.toBeNull();
  });


  it.each([49, 50])(
    "keeps %i-session lists on the plain DOM path",
    (count) => {
      renderRecovery({ groups: sessionGroups(count) });

      expect(
        document.querySelector(".unified-work-virtual-list"),
      ).toBeNull();
      expect(
        document.querySelectorAll(".unified-work-item"),
      ).toHaveLength(count);
    },
  );

  it.each([51, 100, 300])(
    "windows %i-session lists above the rendering threshold",
    (count) => {
      renderRecovery({ groups: sessionGroups(count) });

      const list = document.querySelector<HTMLElement>(
        ".unified-work-virtual-list",
      );
      expect(list?.dataset.virtualized).toBe("true");
      expect(list?.dataset.sessionCount).toBe(String(count));
      expect(
        document.querySelectorAll(".unified-work-item").length,
      ).toBeLessThan(count);
    },
  );

  it("recalculates the virtual list when expanded navigation changes its viewport", async () => {
    let offsetTop = 96;
    const callbacks: ResizeObserverCallback[] = [];
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        callbacks.push(callback);
      }

      disconnect() {}
      observe() {}
      unobserve() {}
    }
    vi.stubGlobal(
      "ResizeObserver",
      FakeResizeObserver as unknown as typeof ResizeObserver,
    );
    const originalOffsetTop = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "offsetTop",
    );
    Object.defineProperty(HTMLElement.prototype, "offsetTop", {
      configurable: true,
      get() {
        return this.classList.contains("unified-work-virtual-list")
          ? offsetTop
          : 0;
      },
    });
    try {
      renderRecovery({ groups: sessionGroups(51) });
      const virtualList = document.querySelector<HTMLElement>(
        ".unified-work-virtual-list",
      );
      if (!virtualList) throw new Error("Expected the virtual work list");

      await waitFor(() =>
        expect(virtualList.dataset.scrollMargin).toBe("96"),
      );
      expect(callbacks).toHaveLength(1);

      act(() => {
        offsetTop = 408;
        window.dispatchEvent(new Event("resize"));
      });

      await waitFor(() =>
        expect(virtualList.dataset.scrollMargin).toBe("408"),
      );
    } finally {
      if (originalOffsetTop) {
        Object.defineProperty(
          HTMLElement.prototype,
          "offsetTop",
          originalOffsetTop,
        );
      } else {
        delete (HTMLElement.prototype as { offsetTop?: number }).offsetTop;
      }
    }
  });

  it("fails closed when the runtime capability is unavailable", async () => {
    renderRecovery({ archivedCount: 3 });

    expect(
      screen.queryByRole("button", { name: /已归档/ }),
    ).toBeNull();
    await openSessionMenu("季度复盘");
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["取消置顶"]);
  });


  it("shows the archived-list entry and offers Archive for a finished session", async () => {
    renderRecovery({
      archiveEnabled: true,
      archivedCount: 2,
      onArchive: vi.fn(() => "success" as const),
      onRestore: vi.fn(),
    });

    expect(
      screen.getByRole("button", { name: /已归档/ }),
    ).not.toBeNull();
    await openSessionMenu("季度复盘");
    expect(
      screen
        .getByRole("menuitem", { name: "归档会话" })
        .getAttribute("aria-disabled"),
    ).not.toBe("true");
  });


  it("removes an archived row after success and restores it through Undo", async () => {
    const onArchive = vi.fn().mockResolvedValue("success");
    const onRestore = vi.fn().mockResolvedValue(undefined);
    renderRecovery({
      groups: [ACTIVE_GROUPS[0]],
      archiveEnabled: true,
      onArchive,
      onRestore,
    });

    await chooseSessionAction("季度复盘", "归档会话");
    await waitFor(() =>
      expect(screen.queryByText("季度复盘")).toBeNull(),
    );
    expect(onArchive).toHaveBeenCalledWith("s_done");
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        text: "已归档“季度复盘”",
        durationMs: 8_000,
        undo: true,
      }),
    );

    const successToast = toastMock.mock.calls.at(-1)?.[0] as {
      action: { onAction: () => Promise<void> };
    };
    await act(() => successToast.action.onAction());

    expect(onRestore).toHaveBeenCalledWith("s_done");
    expect(screen.getByText("季度复盘")).not.toBeNull();
  });

  it("keeps the row when current-session navigation is cancelled", async () => {
    const onArchive = vi.fn().mockResolvedValue("cancelled");
    renderRecovery({
      archiveEnabled: true,
      onArchive,
      onRestore: vi.fn(),
    });

    await chooseSessionAction("季度复盘", "归档会话");
    await waitFor(() => expect(onArchive).toHaveBeenCalled());

    expect(screen.getByText("季度复盘")).not.toBeNull();
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("keeps and refreshes a row when the server reports active work", async () => {
    const onChanged = vi.fn();
    window.addEventListener(SESSION_LIST_CHANGED_EVENT, onChanged);
    const onArchive = vi
      .fn()
      .mockRejectedValue(
        new SessionArchiveError(
          409,
          "session_archive_active_turn",
        ),
      );
    renderRecovery({
      archiveEnabled: true,
      onArchive,
      onRestore: vi.fn(),
    });

    await chooseSessionAction("季度复盘", "归档会话");
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          text: "这项工作仍在运行，完成或停止后才能归档。",
        }),
      ),
    );

    expect(screen.getByText("季度复盘")).not.toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
    window.removeEventListener(SESSION_LIST_CHANGED_EVENT, onChanged);
  });

  it("loads archived sessions directly and opens them without restoring", async () => {
    const onPick = vi.fn();
    const onRestore = vi.fn();
    vi.mocked(fetchSessions).mockResolvedValue({
      archived_count: 1,
      groups: [
        {
          id: "archived",
          label: "ARCHIVED",
          items: [
            {
              id: "s_old",
              title: "旧版定价分析",
              intents: [],
              time: "3d",
              state: "done",
              archived: true,
            },
          ],
        },
      ],
    });
    renderRecovery({
      archiveEnabled: true,
      archivedCount: 1,
      workspaceId: "ws_current",
      onArchive: vi.fn(() => "success" as const),
      onRestore,
      onPick,
    });

    fireEvent.click(
      screen.getByRole("button", { name: /已归档/ }),
    );
    await waitFor(() =>
      expect(screen.getByText("旧版定价分析")).not.toBeNull(),
    );
    expect(fetchSessions).toHaveBeenCalledWith(
      expect.objectContaining({
        view: "archived",
        limit: 100,
        workspaceId: "ws_current",
      }),
    );
    expect(
      screen.queryByRole("button", { name: "置顶" }),
    ).toBeNull();

    fireEvent.click(screen.getByTitle("旧版定价分析"));
    expect(onPick).toHaveBeenCalledWith("s_old");
    expect(onRestore).not.toHaveBeenCalled();
  });

  it("keeps the touch action menu open contract keyboard-accessible", async () => {
    renderRecovery({
      groups: [ACTIVE_GROUPS[0]],
      archiveEnabled: true,
      onArchive: vi.fn(() => "success" as const),
      onRestore: vi.fn(),
    });
    const trigger = screen.getAllByRole("button", {
      name: /^更多会话操作/,
    })[0];

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await screen.findByRole("menuitem", {
      name: "归档会话",
    });
    // Menu portals into body, avoiding row overflow clipping; bottom-edge flip uses useMenu
    // common positioning (components/ui/menu.test.tsx “menu positioning”).
    const menu = screen.getByRole("menu");
    expect(menu.closest("[data-floating-ui-portal]")?.parentElement).toBe(
      document.body,
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("menuitem", { name: "取消置顶" }),
      ),
    );
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(
      screen.getByRole("menuitem", { name: "归档会话" }),
    );

    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    await waitFor(() =>
      expect(
        screen.queryByRole("menuitem", { name: "归档会话" }),
      ).toBeNull(),
    );
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("closes the session action menu on a press outside", async () => {
    renderRecovery({
      groups: [ACTIVE_GROUPS[0]],
      archiveEnabled: true,
      onArchive: vi.fn(() => "success" as const),
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: /^更多会话操作/ })[0],
    );
    expect(await screen.findByRole("menu")).not.toBeNull();

    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  // ── feedback-triage-2026-08-24: customer asks “对话要可以删掉” ──────────────────
  //
  // Soft delete + 8-second undo (same structure as archive). No confirmation modal: undo Toast for reversible deletes
  // is standard; extra modal only adds friction.

  it("★ 删除入口在「更多」菜单里，且标成破坏性动作", async () => {
    renderRecovery({ onDelete: vi.fn(() => "success" as const) });

    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    const item = await screen.findByRole("menuitem", { name: "删除会话" });
    expect(item.getAttribute("data-danger")).toBe("true");
  });

  it("shares the floating-menu style and Title Case English labels", async () => {
    renderRecovery(
      {
        archiveEnabled: true,
        onArchive: vi.fn(() => "success" as const),
        onDelete: vi.fn(() => "success" as const),
      },
      "en-US",
    );

    fireEvent.click(
      screen.getAllByRole("button", { name: /^More session actions/ })[0],
    );
    const menu = await screen.findByRole("menu");
    expect(menu.classList.contains("app-floating-menu")).toBe(true);
    for (const label of ["Unpin", "Archive Session", "Delete Session"]) {
      expect(
        screen
          .getByRole("menuitem", { name: label })
          .classList.contains("app-floating-menu-item"),
      ).toBe(true);
    }
  });

  it("★ 归档关掉时删除入口仍在 —— 菜单原本整体门控在 archiveEnabled 上", async () => {
    renderRecovery({ archiveEnabled: false, onDelete: vi.fn(() => "success" as const) });

    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    expect(await screen.findByRole("menuitem", { name: "删除会话" })).not.toBeNull();
    expect(screen.queryByRole("menuitem", { name: "归档会话" })).toBeNull();
  });

  it("删除成功后行消失，并可经撤销恢复", async () => {
    const onDelete = vi.fn().mockResolvedValue("success");
    const onUndelete = vi.fn().mockResolvedValue(undefined);
    renderRecovery({ groups: [ACTIVE_GROUPS[0]], onDelete, onUndelete });

    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除会话" }));

    await waitFor(() => expect(screen.queryByText("季度复盘")).toBeNull());
    expect(onDelete).toHaveBeenCalledWith("s_done");
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ text: "已删除“季度复盘”", durationMs: 8_000 }),
    );

    const successToast = toastMock.mock.calls.at(-1)?.[0] as {
      action?: { onAction: () => Promise<void> };
    };
    expect(successToast.action).toBeTruthy();
    await successToast.action!.onAction();
    expect(onUndelete).toHaveBeenCalledWith("s_done");
    await waitFor(() => expect(screen.queryByText("季度复盘")).not.toBeNull());
  });

  it.each([
    ["terminal_protocol_writer_not_ready", "对话写入服务正在恢复，请稍后重试。"],
    ["terminal_protocol_malformed", "该会话的已保存状态不一致，请联系管理员检查后再试。"],
    ["session_transitioning", "该会话正在删除或恢复，请等待操作完成后重试。"],
  ])("explains a blocked deletion for %s and keeps the session visible", async (code, message) => {
    renderRecovery({ groups: [ACTIVE_GROUPS[0]], onDelete: vi.fn().mockRejectedValue(new SessionArchiveError(503, code)) });
    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除会话" }));
    await waitFor(() => expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ kind: "error", text: message })));
    expect(screen.queryByText("季度复盘")).not.toBeNull();
  });

  it("explains recovery blocking Undo and keeps the deleted session hidden", async () => {
    const error = new SessionArchiveError(409, "terminal_dependency_recovering");
    renderRecovery({
      groups: [ACTIVE_GROUPS[0]],
      onDelete: vi.fn().mockResolvedValue("success"),
      onUndelete: vi.fn().mockRejectedValue(error),
    });
    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除会话" }));
    await waitFor(() => expect(screen.queryByText("季度复盘")).toBeNull());
    const undo = toastMock.mock.calls.at(-1)?.[0].action.onAction;
    await expect(undo()).rejects.toBe(error);
    expect(toastMock).toHaveBeenLastCalledWith({ kind: "error", text: "该会话正在恢复先前任务，请等待恢复完成后重试。" });
    expect(screen.queryByText("季度复盘")).toBeNull();
  });

  it("★ 后端说「有活跃 turn」时给专门的话术，别混进通用失败", async () => {
    const onDelete = vi
      .fn()
      .mockRejectedValue(new SessionArchiveError(409, "session_delete_active_turn"));
    renderRecovery({ groups: [ACTIVE_GROUPS[0]], onDelete });

    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除会话" }));

    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "error",
          text: "这项工作仍在运行，完成或停止后才能删除。",
        }),
      ),
    );
    // Failure must not hide row.
    expect(screen.queryByText("季度复盘")).not.toBeNull();
  });
});

// Desktop-sidebar deletion (design “行为批 A：菜单” session-deletion section): rows show “⋯” as the only row action
// (app/globals.session-row-more.test.ts and e2e/sidebar-session-delete.spec.ts); screen-reader name includes session title.
describe("WorkRecovery desktop “⋯”", () => {
  beforeEach(() => {
    toastMock.mockReset();
  });
  afterEach(() => {
    cleanup();
  });

  it("renders “⋯” with the session title in its accessible name and marks the row has-more", () => {
    renderRecovery({
      groups: [ACTIVE_GROUPS[0]],
      archiveEnabled: true,
      onArchive: vi.fn(() => "success" as const),
      onDelete: vi.fn(() => "success" as const),
    });
    const row = document.querySelector(".unified-work-item")!;
    expect(row.classList.contains("has-more")).toBe(true);
    // No inline buttons: “⋯” is the only row action.
    expect(row.querySelector(".unified-work-actions")).toBeNull();
    const trigger = screen.getByRole("button", { name: "更多会话操作：季度复盘" });
    expect(row.contains(trigger)).toBe(true);
  });

  it("键盘：回车打开菜单、焦点进第一项，菜单里有删除会话；Esc 关闭回到「⋯」", async () => {
    renderRecovery({
      groups: [ACTIVE_GROUPS[0]],
      archiveEnabled: true,
      onArchive: vi.fn(() => "success" as const),
      onDelete: vi.fn(() => "success" as const),
    });
    const trigger = screen.getByRole("button", { name: "更多会话操作：季度复盘" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(trigger, { detail: 0 });
    const menu = await screen.findByRole("menu", { name: "更多会话操作：季度复盘" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "取消置顶" })));
    const del = screen.getByRole("menuitem", { name: "删除会话" });
    expect(del.textContent).toBe("删除会话");
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("删除会话：不确认，直接删并给 8 秒撤销；撤销调用恢复接口、行放回", async () => {
    const onDelete = vi.fn().mockResolvedValue("success");
    const onUndelete = vi.fn().mockResolvedValue(undefined);
    renderRecovery({ groups: [ACTIVE_GROUPS[0]], archiveEnabled: true, onArchive: vi.fn(), onDelete, onUndelete });
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作：季度复盘" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除会话" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith("s_done"));
    await waitFor(() => expect(screen.queryByText("季度复盘")).toBeNull());
    const undoToast = toastMock.mock.calls.at(-1)?.[0] as {
      text: string;
      durationMs: number;
      action?: { label: string; onAction: () => Promise<void> };
    };
    expect(undoToast).toMatchObject({ text: "已删除“季度复盘”", durationMs: 8_000, action: { label: "撤销" } });
    await undoToast.action!.onAction();
    expect(onUndelete).toHaveBeenCalledWith("s_done");
    await waitFor(() => expect(screen.queryByText("季度复盘")).not.toBeNull());
  });
});

// Behavior batch A (design “行为批 A：菜单”): deletes without confirmation/“…” (undoable deletes no longer confirm);
// running/processing disabled items retain the action name, always show the reason beneath, and remain keyboard-highlightable.
describe("WorkRecovery session “⋯” (behavior batch A)", () => {
  afterEach(() => {
    cleanup();
    toastMock.mockReset();
  });

  it("删除会话不确认、不带「…」：选中直接删除，删后给 8 秒撤销，撤销能把会话放回来", async () => {
    const onDelete = vi.fn().mockResolvedValue("success");
    const onUndelete = vi.fn().mockResolvedValue(undefined);
    renderRecovery({ groups: [ACTIVE_GROUPS[0]], onDelete, onUndelete });

    fireEvent.click(screen.getAllByRole("button", { name: /^更多会话操作/ })[0]);
    const item = await screen.findByRole("menuitem", { name: "删除会话" });
    expect(item.getAttribute("data-danger")).toBe("true");
    fireEvent.click(item);
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith("s_done"));
    await waitFor(() => expect(screen.queryByText("季度复盘")).toBeNull());
    const undoToast = toastMock.mock.calls.at(-1)?.[0] as {
      text: string;
      durationMs: number;
      action?: { onAction: () => Promise<void> };
    };
    // Behavior batch B: Undo Toast appearance (no status marker).
    expect(undoToast).toMatchObject({ text: "已删除“季度复盘”", durationMs: 8_000, undo: true });
    await undoToast.action!.onAction();
    expect(onUndelete).toHaveBeenCalledWith("s_done");
    await waitFor(() => expect(screen.queryByText("季度复盘")).not.toBeNull());
  });

  it("运行中的会话：归档与删除保留动作名，原因第二行常显并经 aria-describedby 读出；可被键盘高亮但不执行", async () => {
    const onDelete = vi.fn();
    const onArchive = vi.fn();
    renderRecovery({
      groups: [{ ...ACTIVE_GROUPS[0], items: [ACTIVE_GROUPS[0].items[1]] }],
      archiveEnabled: true,
      onArchive,
      onDelete,
    });

    const trigger = screen.getAllByRole("button", { name: /^更多会话操作/ })[0];
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const archive = await screen.findByRole("menuitem", { name: "归档会话" });
    const del = screen.getByRole("menuitem", { name: "删除会话" });
    for (const [item, reason] of [
      [archive, "这项工作仍在运行，完成或停止后才能归档。"],
      [del, "这项工作仍在运行，完成或停止后才能删除。"],
    ] as const) {
      expect(item.getAttribute("aria-disabled")).toBe("true");
      expect(item.hasAttribute("disabled")).toBe(false);
      expect(document.getElementById(item.getAttribute("aria-describedby") ?? "")?.textContent).toBe(reason);
    }
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "置顶" })));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(archive);
    fireEvent.keyDown(archive, { key: "Enter" });
    fireEvent.click(archive);
    fireEvent.click(del);
    expect(onArchive).not.toHaveBeenCalled();
    expect(onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

// Session row feedback: hover shows just “⋯” (Pin / Archive / Delete live in its menu, no
// duplicated inline buttons) and unread is the bold title alone, spoken through hidden text instead of a dot.
describe("WorkRecovery session rows", () => {
  beforeEach(() => {
    toastMock.mockReset();
  });
  afterEach(() => {
    cleanup();
  });

  const fullProps = () => ({
    archiveEnabled: true,
    archivedCount: 1,
    onArchive: vi.fn(() => "success" as const),
    onRestore: vi.fn(),
    onDelete: vi.fn(() => "success" as const),
  });

  it("renders only the “⋯” trigger per row: no inline Pin or Archive buttons", () => {
    renderRecovery(fullProps());
    const rows = document.querySelectorAll(".unified-work-item");
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(row.querySelector(".unified-work-actions")).toBeNull();
      expect(row.querySelectorAll("button")).toHaveLength(2);
      expect(row.querySelector(".unified-work-more")).not.toBeNull();
      expect(row.classList.contains("has-more")).toBe(true);
      expect(row.classList.contains("has-actions")).toBe(false);
      expect(row.classList.contains("has-two-actions")).toBe(false);
    }
    expect(screen.queryByRole("button", { name: "置顶" })).toBeNull();
    expect(screen.queryByRole("button", { name: "取消置顶" })).toBeNull();
    expect(screen.queryByRole("button", { name: "归档会话" })).toBeNull();
    expect(screen.queryByRole("button", { name: "运行中的会话无法归档" })).toBeNull();
  });

  it("the menu holds Pin/Unpin, Archive and Delete in that order, and each item runs its action", async () => {
    const props = fullProps();
    const onTogglePin = vi.fn();
    renderRecovery({ ...props, onTogglePin });
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作：季度复盘" }));
    await screen.findByRole("menu");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["取消置顶", "归档会话", "删除会话"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "取消置顶" }));
    await waitFor(() => expect(onTogglePin).toHaveBeenCalledWith("s_done", true));

    fireEvent.click(screen.getByRole("button", { name: "更多会话操作：渠道分析" }));
    await screen.findByRole("menu");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["置顶", "归档会话", "删除会话"]);
  });

  it("keeps Pin reachable through “⋯” even when archive and delete are unavailable", async () => {
    const onTogglePin = vi.fn();
    renderRecovery({ onTogglePin });
    fireEvent.click(screen.getByRole("button", { name: "更多会话操作：季度复盘" }));
    await screen.findByRole("menu");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["取消置顶"]);
  });

  it("keyboard: Tab reaches “⋯”, ArrowDown opens the menu on the first item, arrows move, Esc closes back to “⋯”", async () => {
    renderRecovery({ ...fullProps(), groups: [ACTIVE_GROUPS[0]] });
    const trigger = screen.getByRole("button", { name: "更多会话操作：季度复盘" });
    expect(trigger.tabIndex).toBe(0);
    expect(trigger.hasAttribute("disabled")).toBe(false);
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const menu = await screen.findByRole("menu", { name: "更多会话操作：季度复盘" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "取消置顶" })));
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "归档会话" }));
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "删除会话" }));
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("archived list: Restore lives only in “⋯”", async () => {
    vi.mocked(fetchSessions).mockResolvedValue({
      groups: [{ id: "archived", label: "ARCHIVED", items: [{ ...ACTIVE_GROUPS[0].items[0], archived: true, pinned: false }] }],
      archived_count: 1,
    });
    renderRecovery(fullProps());
    fireEvent.click(screen.getByRole("button", { name: /已归档/ }));
    const trigger = await screen.findByRole("button", { name: "更多会话操作：季度复盘" });
    const row = trigger.closest(".unified-work-item")!;
    expect(row.querySelector(".unified-work-actions")).toBeNull();
    expect(screen.queryByRole("button", { name: "恢复会话" })).toBeNull();
    fireEvent.click(trigger);
    await screen.findByRole("menu");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["恢复会话"]);
  });

  it("unread: no dot, bold-title class kept, and “未读” is part of the row's accessible name", () => {
    renderRecovery();
    expect(document.querySelector(".unified-work-read-state")).toBeNull();
    expect(screen.queryByRole("img", { name: "未读" })).toBeNull();
    const [unreadRow, readRow] = document.querySelectorAll(".unified-work-item");
    expect(unreadRow.classList.contains("is-unread")).toBe(true);
    expect(readRow.classList.contains("is-unread")).toBe(false);
    expect(screen.getByRole("button", { name: /^季度复盘\s*未读/ })).not.toBeNull();
    expect(screen.getByRole("button", { name: /^渠道分析/ }).textContent).not.toContain("未读");
  });

  it("unread: the hidden text follows the interface language", () => {
    renderRecovery({}, "en-US");
    const unread = document.querySelector(".unified-work-item.is-unread .unified-work-sr-only");
    expect(unread?.textContent).toBe("Unread");
  });

  it("the current session reads as read: no hidden unread text", () => {
    renderRecovery({ currentSessionId: "s_done" });
    expect(document.querySelector(".unified-work-item.is-unread")).toBeNull();
    expect(screen.getByRole("button", { name: /^季度复盘/ }).textContent).not.toContain("未读");
  });
});

describe("WorkRecovery session rename", () => {
  beforeEach(() => {
    toastMock.mockReset();
    resetSessionTitleOverridesForTest();
  });
  afterEach(() => {
    cleanup();
    resetSessionTitleOverridesForTest();
  });

  const lifecycleProps = () => ({
    archiveEnabled: true,
    onArchive: vi.fn(() => "success" as const),
    onDelete: vi.fn(() => "success" as const),
  });

  it("puts Rename… first and keeps destructive Delete last, after a separator", async () => {
    renderRecovery({ ...lifecycleProps(), onRename: vi.fn(async () => undefined) });
    await openSessionMenu("季度复盘");
    const menu = screen.getByRole("menu");
    const separator = menu.querySelector('[role="separator"]');
    expect(separator).not.toBeNull();
    expect(separator?.nextElementSibling?.textContent).toBe("删除会话");
    expect(menu.querySelectorAll('[role="separator"]')).toHaveLength(1);
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "重命名…",
      "取消置顶",
      "归档会话",
      "删除会话",
    ]);
  });

  it("opens a single-field dialog with the title selected; Enter saves the trimmed title", async () => {
    const onRename = vi.fn(async () => undefined);
    const listChanged = vi.fn();
    window.addEventListener(SESSION_LIST_CHANGED_EVENT, listChanged);
    renderRecovery({ onRename });
    await chooseSessionAction("季度复盘", "重命名…");

    const dialog = await screen.findByRole("dialog", { name: "重命名会话" });
    const field = screen.getByRole("textbox", { name: "会话名称" }) as HTMLInputElement;
    expect(dialog.contains(field)).toBe(true);
    expect(field.maxLength).toBe(100);
    await waitFor(() => expect(document.activeElement).toBe(field));
    expect(field.value).toBe("季度复盘");
    expect([field.selectionStart, field.selectionEnd]).toEqual([0, "季度复盘".length]);

    fireEvent.change(field, { target: { value: "  Q3 复盘  " } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(onRename).toHaveBeenCalledWith("s_done", "Q3 复盘"));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(listChanged).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /^Q3 复盘/ })).not.toBeNull();
    window.removeEventListener(SESSION_LIST_CHANGED_EVENT, listChanged);
  });

  it("Escape and Cancel close the dialog without renaming", async () => {
    const onRename = vi.fn(async () => undefined);
    renderRecovery({ onRename });
    await chooseSessionAction("季度复盘", "重命名…");
    const field = await screen.findByRole("textbox", { name: "会话名称" });
    fireEvent.change(field, { target: { value: "不会保存" } });
    fireEvent.keyDown(field, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await chooseSessionAction("季度复盘", "重命名…");
    fireEvent.click(await screen.findByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^季度复盘/ })).not.toBeNull();
  });

  it("does not submit while an input method is composing, nor an empty title", async () => {
    const onRename = vi.fn(async () => undefined);
    renderRecovery({ onRename });
    await chooseSessionAction("季度复盘", "重命名…");
    const field = await screen.findByRole("textbox", { name: "会话名称" });
    fireEvent.change(field, { target: { value: "新名" } });
    fireEvent.keyDown(field, { key: "Enter", isComposing: true });
    // Safari ends a composition with an Enter whose isComposing is false but keyCode is 229.
    fireEvent.keyDown(field, { key: "Enter", keyCode: 229 });
    expect(onRename).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "   " } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(screen.getByRole("button", { name: "保存" }).hasAttribute("disabled")).toBe(true);
    expect(onRename).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).not.toBeNull();
  });

  it("shows the new title at once and rolls it back with a toast when saving fails", async () => {
    let reject!: (error: unknown) => void;
    const onRename = vi.fn(
      () =>
        new Promise<void>((_resolve, rejectPromise) => {
          reject = rejectPromise;
        }),
    );
    renderRecovery({ onRename });
    await chooseSessionAction("季度复盘", "重命名…");
    const field = await screen.findByRole("textbox", { name: "会话名称" });
    fireEvent.change(field, { target: { value: "新名字" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(screen.getByRole("button", { name: /^新名字/ })).not.toBeNull());
    await act(async () => {
      reject(new SessionArchiveError(500, "session_rename_failed"));
    });
    await waitFor(() => expect(screen.getByRole("button", { name: /^季度复盘/ })).not.toBeNull());
    expect(screen.queryByRole("button", { name: /^新名字/ })).toBeNull();
    expect(toastMock).toHaveBeenCalledWith({ kind: "error", text: "无法重命名会话，请重试。" });
  });

  it("explains a rejected title with its own message", async () => {
    const onRename = vi.fn(async () => {
      throw new SessionArchiveError(422, "session_title_invalid");
    });
    renderRecovery({ onRename }, "en-US");
    fireEvent.click(screen.getByRole("button", { name: /季度复盘$/ }));
    await screen.findByRole("menu");
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename…" }));
    const field = await screen.findByRole("textbox", { name: "Session name" });
    fireEvent.change(field, { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith({
        kind: "error",
        text: "Session names can't contain line breaks or control characters.",
      }),
    );
  });

  it("leaves Rename out when no handler is given", async () => {
    renderRecovery({ onTogglePin: vi.fn() });
    await openSessionMenu("季度复盘");
    expect(screen.queryByRole("menuitem", { name: "重命名…" })).toBeNull();
  });
});
