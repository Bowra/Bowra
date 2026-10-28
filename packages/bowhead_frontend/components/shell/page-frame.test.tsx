/**
 * PageFrame / PageHeader / PageTabs (Header, Language & Paging Spec §2, D4–D6): one header and column for every
 * first-level work page.
 */

import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Plus } from "lucide-react";

const push = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push }) }));
vi.mock("./AppShell", () => ({
  AppShell: ({
    children,
    mobileTitle,
    mobileActions,
    topbar,
  }: {
    children: ReactNode;
    mobileTitle: string;
    mobileActions?: ReactNode;
    topbar?: ReactNode;
  }) => (
    <main data-testid="shell" data-title={mobileTitle} data-has-topbar={topbar ? "true" : "false"}>
      <div data-testid="mobile-bar">{mobileActions}</div>
      {children}
    </main>
  ),
}));

import {
  PageActionButton,
  PageActionLink,
  PageFrame,
  PageFrameMobileActions,
  PageHeader,
  PageMobileAction,
  PageTabs,
} from "./page-frame";

afterEach(() => {
  cleanup();
  push.mockReset();
  delete window.__BOWHEAD_BASE_PATH__;
});

describe("PageFrame", () => {
  it("gives every page the shared top bar, a scrolling area and the 1120 work column", () => {
    render(
      <PageFrame title="我的报告">
        <p>content</p>
      </PageFrame>,
    );
    const shell = screen.getByTestId("shell");
    // One title string for the desktop top bar, the phone top bar and the H1.
    expect(shell.getAttribute("data-title")).toBe("我的报告");
    expect(shell.getAttribute("data-has-topbar")).toBe("true");
    const scroller = shell.querySelector(".ui-page-frame-scroll");
    const column = scroller?.firstElementChild as HTMLElement;
    expect(column.className).toBe("ui-page-frame");
    expect(column.getAttribute("data-width")).toBe("table");
    expect(column.contains(screen.getByText("content"))).toBe(true);
  });

  it("puts phone actions in the top bar, including ones the page content portals there", () => {
    render(
      <PageFrame title="技能与流程" mobileActions={<PageMobileAction label="创建智能体" icon={Plus} href="/agents/new/" />}>
        <PageFrameMobileActions>
          <PageMobileAction label="上传技能" icon={Plus} onClick={() => {}} />
        </PageFrameMobileActions>
      </PageFrame>,
    );
    const bar = screen.getByTestId("mobile-bar");
    const slot = bar.querySelector(".ui-page-mobile-actions") as HTMLElement;
    expect(within(slot).getByRole("link", { name: "创建智能体" }).className).toBe("ui-page-mobile-action");
    expect(within(slot).getByRole("button", { name: "上传技能" })).toBeTruthy();
  });
});

describe("PageHeader", () => {
  it("repeats the frame title as the H1, one description line, and orders ⋯ · secondary · primary", () => {
    render(
      <PageFrame title="能力中心">
        <PageHeader
          description="发现可用能力"
          moreActions={<button type="button">更多操作</button>}
          secondary={<PageActionButton variant="secondary">新建技能</PageActionButton>}
          primary={<PageActionLink href="/agents/new/" icon={Plus}>创建智能体</PageActionLink>}
        />
      </PageFrame>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "能力中心" }).className).toBe("ui-page-title");
    expect(screen.getByText("发现可用能力").className).toBe("ui-page-desc");
    const actions = document.querySelector(".ui-page-header__actions") as HTMLElement;
    expect(Array.from(actions.children).map((child) => child.textContent)).toEqual(["更多操作", "新建技能", "创建智能体"]);
    const primary = screen.getByRole("link", { name: "创建智能体" });
    expect(primary.className).toContain("settings-button--primary");
    expect(primary.className).toContain("settings-button--md");
    // Create actions lead with a 14px icon.
    expect(primary.querySelector("svg")?.getAttribute("width")).toBe("14");
    expect(screen.getByRole("button", { name: "新建技能" }).className).toContain("settings-button--secondary");
  });

  it("renders no action group when the page has no actions, and navigates in-app with the base path kept", () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    render(
      <PageFrame title="智能体">
        <PageHeader description="d" />
        <PageActionLink href="/agents/new/">创建智能体</PageActionLink>
      </PageFrame>,
    );
    expect(document.querySelector(".ui-page-header__actions")).toBeNull();
    const link = screen.getByRole("link", { name: "创建智能体" });
    expect(link.getAttribute("href")).toBe("/bowhead/agents/new/");
    fireEvent.click(link);
    expect(push).toHaveBeenCalledWith("/agents/new/");
  });
});

describe("PageTabs", () => {
  const items = (current: string) =>
    [
      { key: "files", label: "文件", href: "/knowledge?tab=files", current: current === "files" },
      { key: "matrix", label: "文档矩阵", href: "/knowledge?tab=matrix", current: current === "matrix", count: 2 },
      { key: "records", label: "提取结果", href: "/knowledge?tab=records", current: current === "records", count: null },
      { key: "templates", label: "模板", href: "/knowledge?tab=templates", current: current === "templates", count: 14 },
    ];

  it("renders sibling pages as links with the current one marked, counts and loading placeholders", () => {
    render(<PageTabs label="知识库内容" items={items("matrix")} testId="kb-nav" />);
    const nav = screen.getByRole("navigation", { name: "知识库内容" });
    expect(nav.getAttribute("data-count")).toBe("4");
    expect(nav.getAttribute("data-testid")).toBe("kb-nav");
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => [link.textContent, link.getAttribute("aria-current")])).toEqual([
      ["文件", null],
      ["文档矩阵2", "page"],
      ["提取结果", null],
      ["模板14", null],
    ]);
    expect(links[2].querySelector(".cnt--loading")).not.toBeNull();
  });

  it("navigates with the page's handler on plain clicks and leaves modified clicks to the browser", () => {
    const onNavigate = vi.fn();
    render(<PageTabs label="视图" items={items("files")} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("link", { name: /模板/ }));
    expect(onNavigate).toHaveBeenCalledWith(expect.objectContaining({ key: "templates" }));
    fireEvent.click(screen.getByRole("link", { name: /文档矩阵/ }), { metaKey: true });
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(push).not.toHaveBeenCalled();
  });
});
