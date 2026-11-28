/** Access control: the read-only knowledge base grant report appears only while knowledge bases are on. */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const mocks = vi.hoisted(() => ({ knowledgeBases: true }));
const api = vi.hoisted(() => ({
  fetchAccessGroups: vi.fn(),
  fetchResourceGrants: vi.fn(),
  fetchGroupMembers: vi.fn(),
}));
const report = vi.hoisted(() => ({ fetchKnowledgeGrantReport: vi.fn() }));
vi.mock("@/lib/admin-access-api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), ...api }));
vi.mock("@/lib/knowledge-grant-report-api", async (orig) => ({ ...(await orig<Record<string, unknown>>()), ...report }));
vi.mock("@/lib/auth/use-auth-redirect", () => ({ useAuthRedirect: () => ({ status: "authed" }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { knowledge_bases: mocks.knowledgeBases } } }),
}));
vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/lib/use-error-diagnostics-capability", () => ({ useErrorDiagnosticsCapability: () => false }));

import AdminAccessPage from "./page";

beforeEach(() => {
  mocks.knowledgeBases = true;
  api.fetchAccessGroups.mockResolvedValue({ items: [] });
  api.fetchResourceGrants.mockResolvedValue({ items: [] });
  api.fetchGroupMembers.mockResolvedValue({ items: [] });
  report.fetchKnowledgeGrantReport.mockResolvedValue({ enforced: true, items: [], total: 0, next_cursor: null });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("访问控制 · 知识库授权对照", () => {
  it("shows the report section with knowledge bases on and reloads it with the page", async () => {
    renderIntl(<AdminAccessPage />);
    expect(await screen.findByRole("heading", { name: "知识库授权对照" })).toBeInTheDocument();
    expect(await screen.findByText("团队里没有作用于知识库范围或知识库文件的授权，不需要对照。")).toBeInTheDocument();
    expect(screen.getByTestId("kb-grant-report-enforcement")).toHaveTextContent("强制执行中");
    expect(report.fetchKnowledgeGrantReport).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(report.fetchKnowledgeGrantReport).toHaveBeenCalledTimes(2));
  });

  it("keeps today's page with knowledge bases off", async () => {
    mocks.knowledgeBases = false;
    renderIntl(<AdminAccessPage />);
    await screen.findByRole("heading", { name: "资源授权" });
    expect(screen.queryByTestId("kb-grant-report")).toBeNull();
    expect(screen.queryByText("知识库授权对照")).toBeNull();
    expect(report.fetchKnowledgeGrantReport).not.toHaveBeenCalled();
  });
});
