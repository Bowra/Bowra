// Knowledge bases (FEATURE_KNOWLEDGE_BASES): the one-time notices after the upgrade (PRD §4.11 ⑩).
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({
  role: "member" as "admin" | "member" | "viewer",
  activeId: "ws_1" as string | null,
  me: { id: "u_1", team_id: "team_1" } as { id: string; team_id: string } | null,
}));

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    items: [
      { id: "ws_1", name: "商超零售场景", desc: "", role: mocks.role },
      { id: "ws_2", name: "医药", desc: "", role: "member" },
    ],
    activeId: mocks.activeId,
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => (mocks.me ? { status: "authenticated", me: mocks.me } : null),
}));

import { KbMigrationNotices, migrationNoticeKey, viewerNoticeKey } from "./kb-migration-notice";

function renderNotices() {
  return renderIntl(<KbMigrationNotices personalName="我的知识" spaceName="商超零售场景" teamName="团队知识库" />);
}

beforeEach(() => {
  mocks.role = "member";
  mocks.activeId = "ws_1";
  mocks.me = { id: "u_1", team_id: "team_1" };
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("KbMigrationNotices", () => {
  it("says once where the files went and that access is unchanged; Got it remembers it per person and Team", () => {
    const { unmount } = renderNotices();
    const notice = screen.getByTestId("kb-migration-notice");
    expect(notice).toHaveTextContent(
      "知识库已经按原来的范围整理好了：个人文件在「我的知识」，空间文件在「商超零售场景」，团队文件在「团队知识库」。谁能看到什么没有变化。",
    );
    expect(screen.queryByTestId("kb-viewer-notice")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(screen.queryByTestId("kb-migration-notice")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(migrationNoticeKey("u_1", "team_1"))).toBe("1");
    unmount();

    renderNotices();
    expect(screen.queryByTestId("kb-migration-notice")).not.toBeInTheDocument();
    cleanup();

    // Another Team of the same person sees it again.
    mocks.me = { id: "u_1", team_id: "team_2" };
    renderNotices();
    expect(screen.getByTestId("kb-migration-notice")).toBeInTheDocument();
  });

  it("tells Space viewers, and only them, about their changed rights, once per Space", () => {
    window.localStorage.setItem(migrationNoticeKey("u_1", "team_1"), "1");
    mocks.role = "viewer";
    const { unmount } = renderNotices();
    expect(screen.getByTestId("kb-viewer-notice")).toHaveTextContent("不能再添加或删除里面的文件。需要时请联系空间管理员。");
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(screen.queryByTestId("kb-viewer-notice")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(viewerNoticeKey("u_1", "team_1", "ws_1"))).toBe("1");
    unmount();

    renderNotices();
    expect(screen.queryByTestId("kb-viewer-notice")).not.toBeInTheDocument();
    cleanup();

    // In a Space where the person is a member, no viewer notice.
    mocks.activeId = "ws_2";
    renderNotices();
    expect(screen.queryByTestId("kb-viewer-notice")).not.toBeInTheDocument();
  });

  it("renders and dismisses without storage (private windows, blocked site data)", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    renderNotices();
    expect(screen.getByTestId("kb-migration-notice")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(screen.queryByTestId("kb-migration-notice")).not.toBeInTheDocument();
  });

  it("shows nothing until the person is known", () => {
    mocks.me = null;
    renderNotices();
    expect(screen.queryByTestId("kb-migration-notice")).not.toBeInTheDocument();
  });
});
