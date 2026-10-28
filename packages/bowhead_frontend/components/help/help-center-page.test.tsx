import { PRODUCT_NAME } from "@/lib/product-identity";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { resetServerVersionCacheForTests } from "@/lib/app-version";
import { loadHelpArticles, type HelpArticle } from "@/lib/help-content";

import { HelpCenterPage, THIRD_PARTY_NOTICES_PATH, visibleHelpArticles, withApplicationName } from "./help-center-page";

const { push, search, session, toastMock } = vi.hoisted(() => ({
  toastMock: vi.fn(),
  push: vi.fn(),
  search: { article: "overview" },
  session: { capabilities: undefined as Record<string, unknown> | undefined },
}));

vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () =>
    session.capabilities ? { me: { capabilities: session.capabilities } } : null,
}));

vi.mock("@/lib/use-toast", () => ({ toast: toastMock }));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams({ article: search.article }),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push, replace: vi.fn() }),
}));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <main>{children}</main>,
}));
vi.mock("@/components/shell/Topbar", () => ({
  Topbar: () => null,
}));
vi.mock("./mermaid-diagram", () => ({
  MermaidDiagram: ({ source }: { source: string }) => (
    <div data-testid="mermaid-diagram">{source}</div>
  ),
}));

const articles: HelpArticle[] = [
  {
    id: "home",
    title: "帮助中心首页",
    description: "按你的目标找到下一步",
    group: null,
    content: "这里帮助你完成工作。",
  },
  {
    id: "getting-started",
    title: "开始一次分析工作",
    description: "选择空间、说明目标并查看结果",
    group: "getting_started",
    content: "# 开始一次分析工作\n",
  },
  {
    id: "overview",
    title: "权限与访问说明书",
    description: "先理解完整权限模型和配置流程",
    group: "team_access",
    content:
      "# 权限与访问说明书\n\n总览。\n\n```mermaid\nflowchart LR\nA --> B\n```\n\n[下一篇](/help/?article=space-access)",
  },
  {
    id: "security-sandbox",
    title: "安全与离线沙盒",
    description: "了解执行环境、隔离边界和防护措施",
    group: "security_data",
    content: "# 安全与离线沙盒\n\n离线执行。",
  },
  {
    id: "space-access",
    title: "开通空间访问",
    description: "添加成员并选择空间角色",
    group: "team_access",
    content: "# 给同事开通空间访问权限\n\n|角色|范围|\n|---|---|\n|成员|当前空间|",
  },
  {
    id: "reference",
    title: "角色与操作参考",
    description: "快速核对谁能执行哪些操作",
    group: "reference",
    content: "# 权限参考\n",
  },
];

describe("HelpCenterPage", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    search.article = "overview";
    session.capabilities = undefined;
    delete window.__BOWHEAD_BASE_PATH__;
  });

  it("renders the overview and turns Mermaid source into a diagram", () => {
    renderIntl(<HelpCenterPage articles={articles} />);

    expect(
      screen.getByRole("heading", { name: "权限与访问说明书", level: 1 }),
    ).toBeTruthy();
    expect(screen.getByTestId("mermaid-diagram").textContent).toContain(
      "flowchart LR",
    );
    expect(
      screen.getByRole("link", { name: /权限与访问说明书/ }).getAttribute(
        "aria-current",
      ),
    ).toBe("page");
  });

  it("selects an article from the URL and keeps navigation inside the app", () => {
    search.article = "space-access";
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    renderIntl(<HelpCenterPage articles={articles} />);

    expect(
      screen.getByRole("heading", {
        name: "给同事开通空间访问权限",
        level: 1,
      }),
    ).toBeTruthy();
    const overview = screen.getByRole("link", {
      name: /权限与访问说明书/,
    });
    expect(overview.getAttribute("href")).toBe(
      "/bowhead/help/?article=overview",
    );
    fireEvent.click(overview);
    expect(push).toHaveBeenCalledWith("/help/?article=overview");
  });

  it("shows the sandbox guide in its own security group", () => {
    search.article = "security-sandbox";
    renderIntl(<HelpCenterPage articles={articles} />);

    expect(
      screen.getByRole("heading", { name: "安全与离线沙盒", level: 1 }),
    ).toBeTruthy();
    expect(screen.getByRole("heading", { name: "安全与数据" })).toBeTruthy();
  });

  it("uses the Help Center home as the default landing page", () => {
    search.article = "home";
    renderIntl(<HelpCenterPage articles={articles} />);

    // The H1 repeats the top-bar title (large-title pattern); the former headline stays as the tagline.
    expect(
      screen.getByRole("heading", { name: "帮助中心", level: 1 }),
    ).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByText("从一件工作开始")).toBeTruthy();
    expect(
      screen.getAllByRole("link", { name: /开始一次分析工作/ }),
    ).toHaveLength(3);
    expect(
      screen.getByRole("link", { name: /帮助中心首页/ }).getAttribute(
        "aria-current",
      ),
    ).toBe("page");
  });

  it("routes links inside rendered Help content without a full reload", () => {
    renderIntl(<HelpCenterPage articles={articles} />);

    fireEvent.click(screen.getByRole("link", { name: "下一篇" }));
    expect(push).toHaveBeenCalledWith("/help/?article=space-access");
  });
});

const gatedArticle: HelpArticle = {
  id: "access-requests",
  title: "申请访问与处理申请",
  description: "无权访问时在线申请",
  group: "team_access",
  content: "# 申请访问与处理申请\n\n在无权访问页面提交申请。",
  requiresCapability: "access_requests",
};

function renderedHelpCenter(pageArticles: HelpArticle[], article: string): string {
  search.article = article;
  const { container } = renderIntl(<HelpCenterPage articles={pageArticles} />);
  const html = container.innerHTML;
  cleanup();
  return html;
}

describe("HelpCenterPage capability-gated articles", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    search.article = "overview";
    session.capabilities = undefined;
  });

  it("lists and opens a gated article when /me reports its capability", () => {
    session.capabilities = { access_requests: true };
    search.article = "access-requests";
    renderIntl(<HelpCenterPage articles={[...articles, gatedArticle]} />);

    expect(
      screen.getByRole("heading", { name: "申请访问与处理申请", level: 1 }),
    ).toBeTruthy();
    const link = screen.getByRole("link", { name: /申请访问与处理申请/ });
    expect(link.getAttribute("href")).toBe("/help/?article=access-requests");
    expect(link.getAttribute("aria-current")).toBe("page");
  });

  it.each([
    ["no session", undefined],
    ["capability off", { access_requests: false }],
    ["capability absent", {}],
  ])("renders the Help Center exactly as without the article (%s)", (_label, capabilities) => {
    for (const article of ["home", "overview", "space-access"]) {
      session.capabilities = undefined;
      const baseline = renderedHelpCenter(articles, article);
      session.capabilities = capabilities;
      expect(renderedHelpCenter([...articles, gatedArticle], article)).toBe(baseline);
    }
  });

  it("treats a deep link to a hidden article like an unknown article id", () => {
    const unknown = renderedHelpCenter(articles, "no-such-article");
    session.capabilities = { access_requests: false };
    const hidden = renderedHelpCenter([...articles, gatedArticle], "access-requests");

    expect(hidden).toBe(unknown);
    expect(hidden).not.toContain("申请访问与处理申请");
  });

  it("gates the real access-requests article in the registry", () => {
    const all = loadHelpArticles(undefined, "en-US");
    const ungated = all.filter(({ requiresCapability }) => !requiresCapability);

    expect(all.find(({ id }) => id === "access-requests")?.requiresCapability).toBe(
      "access_requests",
    );
    expect(visibleHelpArticles(all, { access_requests: false })).toEqual(ungated);
    expect(visibleHelpArticles(all, undefined)).toEqual(ungated);
    expect(
      visibleHelpArticles(all, { access_requests: true, google_workspace_feature: true, shopify_plugin: true, agent_eval_alignment: true, account_deletion: true }).map(({ id }) => id),
    ).toEqual(all.map(({ id }) => id));
  });

  it("shows the Google Workspace article only while the feature is switched on", () => {
    const all = loadHelpArticles(undefined, "en-US");
    const ids = (capabilities: Parameters<typeof visibleHelpArticles>[1]) =>
      visibleHelpArticles(all, capabilities).map(({ id }) => id);

    expect(ids({ google_workspace_feature: false })).not.toContain("google-workspace");
    expect(ids({ google_workspace_feature: true })).toContain("google-workspace");
  });

  it("shows About without any capability", () => {
    const all = loadHelpArticles(undefined, "en-US");
    expect(visibleHelpArticles(all, undefined).map(({ id }) => id)).toEqual(
      expect.arrayContaining(["about"]),
    );
  });
});

describe("HelpCenterPage About", () => {
  const about: HelpArticle = {
    id: "about",
    title: "About {applicationName}",
    description: "Version information",
    group: "reference",
    content: "# About {applicationName}\n\nIntro.\n\n<!-- about-version -->\n\n## Third-party licenses\n\n{applicationName} uses open-source software.",
  };
  const writeText = vi.fn();

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    resetServerVersionCacheForTests();
    session.capabilities = undefined;
    search.article = "overview";
    delete window.__BOWHEAD_BASE_PATH__;
  });

  function renderAbout(serverVersion: string) {
    vi.stubEnv("NEXT_PUBLIC_BOWHEAD_VERSION", "1.4.8");
    vi.stubEnv("NEXT_PUBLIC_BUILD_LOCALE", "en-US");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ version: serverVersion }), { status: 200 })),
    );
    writeText.mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    search.article = "about";
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    renderIntl(<HelpCenterPage articles={[...articles, about]} />, { locale: "en-US" });
  }

  it("puts the version card between the introduction and the sections and links the notes and licenses", async () => {
    renderAbout("1.4.8");

    expect(screen.getByRole("heading", { name: `About ${PRODUCT_NAME}`, level: 1 })).toBeTruthy();
    const card = screen.getByRole("region", { name: "Version information" });
    const intro = screen.getByText("Intro.");
    const sections = screen.getByRole("heading", { name: "Third-party licenses", level: 2 });
    expect(intro.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.compareDocumentPosition(sections) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.textContent).toContain(`${PRODUCT_NAME} 1.4.8`);
    await waitFor(() => expect(card.querySelector("dd[aria-live]")?.textContent).toBe("1.4.8"));
    expect(card.textContent).not.toContain("Refresh the page");

    const notes = screen.getByRole("link", { name: "Release notes" });
    expect(notes.getAttribute("href")).toBe("/bowhead/release-notes/");
    fireEvent.click(notes);
    expect(push).toHaveBeenCalledWith("/release-notes/");
    expect(screen.getByRole("link", { name: "Third-party licenses" }).getAttribute("href")).toBe(
      `/bowhead${THIRD_PARTY_NOTICES_PATH}`,
    );
  });

  it("copies the version details and warns when the server runs another version", async () => {
    renderAbout("1.4.9");

    await screen.findByText("The web app and server versions differ. Refresh the page to load the latest version.");
    fireEvent.click(screen.getByRole("button", { name: "Copy version details" }));
    expect(writeText).toHaveBeenCalledWith(`${PRODUCT_NAME} 1.4.8\nWeb: 1.4.8 (en-US build)\nServer: 1.4.9`);
    await waitFor(() => expect(toastMock).toHaveBeenCalledWith({ kind: "success", text: "Version details copied" }));
  });
});

describe("withApplicationName", () => {
  it("names the product with the team's application name in About only", () => {
    const [about, other] = withApplicationName(
      [
        { id: "about", title: "About {applicationName}", description: "d", group: "reference", content: "# About {applicationName}\n\n{applicationName} 1.4.8" },
        articles[1],
      ],
      "Example Insights",
    );
    expect(about.title).toBe("About Example Insights");
    expect(about.content).toBe("# About Example Insights\n\nExample Insights 1.4.8");
    expect(other).toBe(articles[1]);
    expect(withApplicationName([about], "  ")[0].title).toBe("About Example Insights");
  });
});
