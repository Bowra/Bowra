// credential-readiness R9: contextual status banner for the three kb capabilities on /knowledge (PRD C2).
import "@testing-library/jest-dom/vitest";

import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { KbCapabilities } from "@/lib/kb-capabilities-api";

const mocks = vi.hoisted(() => ({
  fetchKbCapabilities: vi.fn(),
  authMe: { capabilities: { team_onboarding: true } } as Record<string, unknown>,
  routerPush: vi.fn(),
  spaces: {
    items: [{ id: "ws_1", name: "Space one", desc: "" }],
    activeId: "ws_1",
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as {
    items: Array<{ id: string; name: string; desc: string }>;
    activeId: string | null;
    loading: boolean;
    error: string | null;
    refresh: ReturnType<typeof vi.fn>;
  },
}));

vi.mock("@/lib/kb-capabilities-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-capabilities-api")>();
  return {
    ...actual,
    fetchKbCapabilities: (...args: unknown[]) => mocks.fetchKbCapabilities(...args),
  };
});

vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/knowledge/kb-files-manager", () => ({
  KbFilesManager: () => <div data-testid="stub-files" />,
}));
vi.mock("@/components/knowledge/matrix/matrix-tab", () => ({
  MatrixTab: () => <div />,
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => ({ status: "authed", me: mocks.authMe, isTeamAdmin: false, error: null }),
}));
vi.mock("@/lib/kb-knowledge-tables-api", () => ({
  listDocTypesWithStats: vi.fn().mockResolvedValue({ items: [], stats: {} }),
}));
vi.mock("@/lib/kb-matrix/snapshot-api", () => ({
  listMatrices: vi.fn().mockResolvedValue([]),
}));
vi.mock("@/lib/kb-matrix/team-template-api", () => ({
  listTeamTemplates: vi.fn().mockResolvedValue([]),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => mocks.spaces,
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.routerPush, replace: vi.fn() }),
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));

import KnowledgePage from "@/app/(app)/knowledge/page";

function caps(overrides: Partial<KbCapabilities> = {}): KbCapabilities {
  return { kb_rerank: "enabled", kb_vision: "enabled", kb_row_extractor: "enabled", ...overrides };
}

describe("/knowledge · kb 能力提示条", () => {
  beforeEach(() => {
    mocks.fetchKbCapabilities.mockReset();
    mocks.routerPush.mockReset();
    mocks.spaces = {
      items: [{ id: "ws_1", name: "Space one", desc: "" }],
      activeId: "ws_1",
      loading: false,
      error: null,
      refresh: vi.fn(),
    };
    mocks.authMe = { capabilities: { team_onboarding: true } };
  });
  afterEach(cleanup);

  it("有缺失能力时显示提示条（名称+状态），admin 有直达 setup 入口", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(caps({ kb_vision: "disabled", kb_rerank: "invalid" }));
    renderIntl(<KnowledgePage />);
    const notice = await screen.findByTestId("kb-capability-notice");
    expect(notice).toHaveTextContent("视觉解析（未启用）");
    expect(notice).toHaveTextContent("检索重排（配置无效）");
    expect(notice).not.toHaveTextContent("行提取");
    screen.getByTestId("kb-capability-notice-setup").click();
    expect(mocks.routerPush).toHaveBeenCalledWith("/setup");
  });

  it("非 admin（无 team_onboarding capability）看到「可由团队管理员开启」而非死链接", async () => {
    mocks.authMe = { capabilities: {} };
    mocks.fetchKbCapabilities.mockResolvedValue(caps({ kb_vision: "disabled" }));
    renderIntl(<KnowledgePage />);
    const notice = await screen.findByTestId("kb-capability-notice");
    expect(notice).toHaveTextContent("可由团队管理员开启");
    expect(screen.queryByTestId("kb-capability-notice-setup")).not.toBeInTheDocument();
  });

  it("三项全 enabled 时不显示提示条", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(caps());
    renderIntl(<KnowledgePage />);
    await waitFor(() => {
      expect(mocks.fetchKbCapabilities).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("kb-capability-notice")).not.toBeInTheDocument();
  });

  it("能力状态取不到（null）时静默无提示条", async () => {
    mocks.fetchKbCapabilities.mockResolvedValue(null);
    renderIntl(<KnowledgePage />);
    await waitFor(() => {
      expect(mocks.fetchKbCapabilities).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("kb-capability-notice")).not.toBeInTheDocument();
  });

  it("没有可访问 Space 时只显示加入引导，不渲染知识库内容或请求能力状态", () => {
    mocks.spaces = {
      items: [],
      activeId: null,
      loading: false,
      error: null,
      refresh: vi.fn(),
    };

    renderIntl(<KnowledgePage />);

    expect(screen.getByText("加入 Space 后即可使用知识库")).toBeInTheDocument();
    expect(screen.getByText(/你目前尚未加入任何 Space/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新访问权限" })).toBeInTheDocument();
    expect(screen.queryByTestId("stub-files")).not.toBeInTheDocument();
    expect(mocks.fetchKbCapabilities).not.toHaveBeenCalled();
  });
});
