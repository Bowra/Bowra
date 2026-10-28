import { useState } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";
import { ProductShell } from "./ProductShell";

const { appNavigationAllowed, navigateAppDocument } = vi.hoisted(() => ({
  appNavigationAllowed: vi.fn(() => true),
  navigateAppDocument: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) =>
    key === "skip_to_content" ? "跳到主要内容" : key,
}));
vi.mock("@/components/mobile-topbar", () => ({
  MobileTopBar: ({ rightSlot }: { rightSlot?: React.ReactNode }) => (
    <div data-testid="mobile-topbar">{rightSlot}</div>
  ),
}));
vi.mock("./global-controls", () => ({
  ShellGlobalControls: ({ surface }: { surface: string }) => (
    <span data-testid="shell-global-controls" data-surface={surface} />
  ),
}));
vi.mock("@/components/unified-sidebar", () => ({
  UnifiedSidebar: ({
    onNewSession,
    preferCompactDesktop,
    sessions,
  }: {
    onNewSession: () => void;
    preferCompactDesktop?: boolean;
    sessions?: {
      groups: unknown[];
      workspaceId?: string;
      loading?: boolean;
      error?: boolean;
      onArchive?: (sessionId: string) => unknown;
    };
  }) => (
    <div
      data-testid="unified-sidebar"
      data-compact={preferCompactDesktop ? "true" : "false"}
      data-session-loading={sessions?.loading ? "true" : "false"}
      data-session-error={sessions?.error ? "true" : "false"}
      data-session-groups={sessions?.groups.length ?? 0}
      data-workspace-id={sessions?.workspaceId ?? ""}
      data-has-archive={sessions?.onArchive ? "true" : "false"}
    >
      <button type="button" onClick={onNewSession}>
        sidebar-new
      </button>
    </div>
  ),
}));
vi.mock("@/components/sessions-drawer", () => ({
  SessionsDrawer: () => null,
}));
vi.mock("@/lib/use-app-router", () => ({
  appNavigationAllowed,
  navigateAppDocument,
}));
vi.mock("@/lib/use-session-archive-capability", () => ({
  useSessionArchiveCapability: () => true,
}));

function StatefulMain() {
  const [count, setCount] = useState(0);
  return <button onClick={() => setCount((value) => value + 1)}>{count}</button>;
}

describe("AppShell unified sidebar baseline", () => {
  beforeEach(() => {
    appNavigationAllowed.mockReturnValue(true);
  });

  afterEach(() => {
    cleanup();
    appNavigationAllowed.mockClear();
    navigateAppDocument.mockClear();
  });

  it("always renders the unified shell and an accessible main target", () => {
    render(
      <ProductShell>
        <AppShell
          onNewSession={() => "created"}
          mobileTitle="Bowhead"
        >
          <div>main</div>
        </AppShell>
      </ProductShell>,
    );

    expect(screen.getByTestId("unified-sidebar")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "跳到主要内容" }).getAttribute("href"),
    ).toBe("#bowhead-main-content");
    expect(document.querySelector("main")?.id).toBe("bowhead-main-content");
    expect(document.querySelector("main")?.tabIndex).toBe(-1);
  });

  it("passes the page's session list status to the sidebar and follows it (116: cold load said No sessions yet)", () => {
    const session = (loading: boolean, error = false) => ({ groups: [], currentSessionId: null, loading, error, onPick: vi.fn() });
    const { rerender } = render(
      <ProductShell>
        <AppShell mobileTitle="Chat" unifiedSidebarSessions={session(true)}>chat</AppShell>
      </ProductShell>,
    );
    expect(screen.getByTestId("unified-sidebar").getAttribute("data-session-loading")).toBe("true");
    rerender(
      <ProductShell>
        <AppShell mobileTitle="Chat" unifiedSidebarSessions={session(false, true)}>chat</AppShell>
      </ProductShell>,
    );
    expect(screen.getByTestId("unified-sidebar").getAttribute("data-session-loading")).toBe("false");
    expect(screen.getByTestId("unified-sidebar").getAttribute("data-session-error")).toBe("true");
  });

  it("mirrors the Topbar's app-global controls into the mobile topbar only on pages that have a Topbar", () => {
    const { rerender } = render(
      <ProductShell>
        <AppShell mobileTitle="Chat" topbar={<div className="v2-main-head" />}>
          chat
        </AppShell>
      </ProductShell>,
    );
    const controls = screen.getByTestId("mobile-topbar").querySelector("[data-testid='shell-global-controls']");
    expect(controls?.getAttribute("data-surface")).toBe("mobile-topbar");

    rerender(
      <ProductShell>
        <AppShell mobileTitle="Agents">agents</AppShell>
      </ProductShell>,
    );
    expect(screen.getByTestId("mobile-topbar").querySelector("[data-testid='shell-global-controls']")).toBeNull();
  });

  it("puts the page's mobile actions (the session menu) after the global controls in the mobile topbar", () => {
    render(
      <ProductShell>
        <AppShell mobileTitle="Chat" topbar={<div className="v2-main-head" />} mobileActions={<button type="button">会话操作</button>}>
          chat
        </AppShell>
      </ProductShell>,
    );
    const bar = screen.getByTestId("mobile-topbar");
    const action = within(bar).getByRole("button", { name: "会话操作" });
    const controls = bar.querySelector("[data-testid='shell-global-controls']")!;
    expect(controls.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("preserves main state when the compact preference changes", () => {
    const props = {
      onNewSession: () => "created" as const,
      mobileTitle: "Bowhead",
    };
    const { rerender } = render(
      <ProductShell>
        <AppShell {...props}>
          <StatefulMain />
        </AppShell>
      </ProductShell>,
    );
    fireEvent.click(screen.getByRole("button", { name: "0" }));
    expect(screen.getByRole("button", { name: "1" })).toBeTruthy();

    rerender(
      <ProductShell>
        <AppShell {...props} preferCompactSidebar>
          <StatefulMain />
        </AppShell>
      </ProductShell>,
    );

    expect(screen.getByTestId("unified-sidebar").dataset.compact).toBe("true");
    expect(screen.getByRole("button", { name: "1" })).toBeTruthy();
  });

  it("keeps the sidebar instance mounted when the page slot changes", () => {
    const firstPage = (
      <AppShell
        key="chat"
        onNewSession={() => "created"}
        mobileTitle="Chat"
      >
        chat
      </AppShell>
    );
    const { rerender } = render(
      <ProductShell>{firstPage}</ProductShell>,
    );
    const sidebar = screen.getByTestId("unified-sidebar");

    rerender(
      <ProductShell>
        <AppShell
          key="files"
          onNewSession={() => "created"}
          mobileTitle="Files"
        >
          files
        </AppShell>
      </ProductShell>,
    );

    expect(screen.getByTestId("unified-sidebar")).toBe(sidebar);
    expect(screen.getByText("files")).toBeTruthy();
  });

  it("routes persistent sidebar actions to the current page bindings", async () => {
    const firstNewSession = vi.fn(() => "created" as const);
    const nextNewSession = vi.fn(() => "created" as const);
    const { rerender } = render(
      <ProductShell>
        <AppShell
          key="chat"
          onNewSession={firstNewSession}
          mobileTitle="Chat"
          unifiedSidebarSessions={{
            groups: [{ id: "today", label: "Today", items: [] }],
            currentSessionId: "s_1",
            onPick: () => "picked" as const,
          }}
        >
          chat
        </AppShell>
      </ProductShell>,
    );

    expect(
      screen.getByTestId("unified-sidebar").dataset.sessionGroups,
    ).toBe("1");
    expect(
      screen.getByTestId("unified-sidebar").dataset.hasArchive,
    ).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "sidebar-new" }));
    await waitFor(() => expect(firstNewSession).toHaveBeenCalledOnce());

    rerender(
      <ProductShell>
        <AppShell
          key="files"
          onNewSession={nextNewSession}
          mobileTitle="Files"
        >
          files
        </AppShell>
      </ProductShell>,
    );

    expect(
      screen.getByTestId("unified-sidebar").dataset.sessionGroups,
    ).toBe("0");
    fireEvent.click(screen.getByRole("button", { name: "sidebar-new" }));
    await waitFor(() => expect(nextNewSession).toHaveBeenCalledOnce());
    expect(firstNewSession).toHaveBeenCalledOnce();
  });

  it("forwards archive callbacks only when a page registers them", () => {
    render(
      <ProductShell>
        <AppShell
          mobileTitle="Chat"
          unifiedSidebarSessions={{
            groups: [],
            currentSessionId: null,
            onPick: () => "picked" as const,
            onArchive: () => "success",
            onRestore: () => {},
          }}
        >
          chat
        </AppShell>
      </ProductShell>,
    );

    expect(
      screen.getByTestId("unified-sidebar").dataset.hasArchive,
    ).toBe("true");
  });

  it("refreshes the persistent session binding when the active space changes", () => {
    const sessions = {
      groups: [],
      currentSessionId: null,
      workspaceId: "ws_a",
      onPick: () => "picked" as const,
    };
    const { rerender } = render(
      <ProductShell>
        <AppShell
          mobileTitle="Chat"
          unifiedSidebarSessions={sessions}
        >
          chat
        </AppShell>
      </ProductShell>,
    );

    expect(
      screen.getByTestId("unified-sidebar").dataset.workspaceId,
    ).toBe("ws_a");

    rerender(
      <ProductShell>
        <AppShell
          mobileTitle="Chat"
          unifiedSidebarSessions={{
            ...sessions,
            workspaceId: "ws_b",
          }}
        >
          chat
        </AppShell>
      </ProductShell>,
    );

    expect(
      screen.getByTestId("unified-sidebar").dataset.workspaceId,
    ).toBe("ws_b");
  });

  it("deduplicates concurrent New work requests across shell surfaces", async () => {
    let finish: ((result: "created") => void) | undefined;
    const onNewSession = vi.fn(
      () =>
        new Promise<"created">((resolve) => {
          finish = resolve;
        }),
    );
    render(
      <ProductShell>
        <AppShell onNewSession={onNewSession} mobileTitle="Chat">
          chat
        </AppShell>
      </ProductShell>,
    );

    const action = screen.getByRole("button", { name: "sidebar-new" });
    fireEvent.click(action);
    fireEvent.click(action);
    expect(onNewSession).toHaveBeenCalledOnce();
    finish?.("created");
  });

  it("uses the configured document route when a product page has no local reset", async () => {
    render(
      <ProductShell>
        <AppShell mobileTitle="Reports">reports</AppShell>
      </ProductShell>,
    );

    fireEvent.click(screen.getByRole("button", { name: "sidebar-new" }));
    await waitFor(() => expect(navigateAppDocument).toHaveBeenCalledOnce());
    expect(navigateAppDocument).toHaveBeenCalledWith("/");
  });
});
