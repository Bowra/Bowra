/**
 * Report library cards (page-by-page acceptance B): “复制权限链接” and Delete live in the row-end “⋯” menu; deletion still requires confirmation.
 */
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate, menuDangerNames, menuItemNames, openRowMenu } from "@/__tests__/row-actions";

const api = vi.hoisted(() => ({ fetchReports: vi.fn(), deleteCanvas: vi.fn() }));
const share = vi.hoisted(() => ({ copy: vi.fn() }));
vi.mock("@/lib/api", () => ({
  deleteCanvas: api.deleteCanvas,
  exportJobDownloadUrl: vi.fn(),
  fetchReports: api.fetchReports,
  fetchReportsPage: (options: Record<string, unknown>) =>
    Promise.resolve(api.fetchReports(options)).then((items: unknown[]) => ({ items, total: items.length, next_cursor: null, has_more: false })),
  setCanvasStatus: vi.fn(),
}));
vi.mock("@/lib/report-share", () => ({
  copyPermissionedReportLink: share.copy,
  reportExportHrefForSession: () => "/",
  reportHrefForSession: () => "/",
}));
vi.mock("@/components/shell/AppShell", () => ({ AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/components/canvas/export-ui", () => ({ ExportToast: () => null, ExportsDrawer: () => null }));
vi.mock("@/components/canvas/report-status", () => ({ ReportStatusControl: () => null }));
vi.mock("@/components/share/share-dialog", () => ({ ShareDialog: () => null }));
vi.mock("@/lib/metrics", () => ({ track: vi.fn() }));
vi.mock("@/lib/use-canvas-export", () => ({
  useCanvasExport: () => ({ busy: false, dismiss: vi.fn(), retry: vi.fn(), retryWithDefaultTheme: vi.fn(), start: vi.fn(), state: { lastArgs: null } }),
}));
const ent = vi.hoisted(() => ({ readOnly: false }));
vi.mock("@/lib/entitlement-store", () => ({ useEntitlement: () => ({ readOnly: ent.readOnly }) }));
vi.mock("@/lib/use-space", () => ({ useSpace: () => ({ items: [], activeId: null }), getActiveSpace: (fallback: string) => fallback }));

import ReportsPage from "./page";

const report = {
  id: "cv_1",
  session_id: "s_1",
  workspace_id: "ws_1",
  title: "季度复盘",
  status: "active",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  sections_count: 3,
};
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  ent.readOnly = false;
});

function renderWithReport() {
  api.fetchReports.mockImplementation((options: { scope?: string }) => Promise.resolve(options.scope === "shared" ? [] : [report]));
  api.deleteCanvas.mockResolvedValue(undefined);
  renderIntl(<ReportsPage />);
}

describe("报告库卡片行内操作", () => {
  it("puts both actions in the row menu; delete is a danger item and confirms first", async () => {
    renderWithReport();
    const menu = await openRowMenu("「季度复盘」的更多操作");
    // An active report has no status chip; its status transitions sit in the row menu before the link and delete.
    expect(menuItemNames(menu)).toEqual(["标记为已核对", "归档", "复制权限链接", "删除…"]);
    expect(screen.queryByText("报告状态：活跃")).toBeNull();
    expect(menuDangerNames(menu)).toEqual(["删除…"]);
    expect(screen.queryByRole("button", { name: "删除报告 季度复盘" })).toBeNull();

    fireEvent.click(within(menu).getByRole("menuitem", { name: "复制权限链接" }));
    expect(share.copy).toHaveBeenCalledWith("s_1");

    await chooseRowAction("「季度复盘」的更多操作", /^删除/, { danger: true });
    await expectConfirmGate({
      reopen: () => chooseRowAction("「季度复盘」的更多操作", /^删除/),
      dialogName: "删除「季度复盘」？",
      action: api.deleteCanvas,
      confirmName: "删除报告",
    });
    await vi.waitFor(() => expect(api.deleteCanvas).toHaveBeenCalledTimes(1));
  });
});

describe("report rows have no primary button (Component Spec: one primary per view)", () => {
  it("renders the row Open as a secondary button and keeps the visibility chip for phones in the meta line", async () => {
    api.fetchReports.mockImplementation((options: { scope?: string }) =>
      Promise.resolve(options.scope === "shared" ? [] : [{ ...report, audience: "private", share_count: 0 }]),
    );
    renderIntl(<ReportsPage />);
    const open = await screen.findByRole("link", { name: "打开" });
    expect(open.className).toContain("settings-button--secondary");
    expect(document.querySelectorAll(".reports-row .settings-button--primary")).toHaveLength(0);
    expect(document.querySelector(".reports-row-phone-audience")).not.toBeNull();
  });
});

describe("报告库卡片「分享」与只读账号（第 4b 批，与工作台报告工具条同一条规则）", () => {
  it("hides Share for read-only accounts and shows it for regular accounts", async () => {
    ent.readOnly = true;
    renderWithReport();
    await openRowMenu("「季度复盘」的更多操作");
    expect(screen.queryByRole("button", { name: "分享" })).toBeNull();
    cleanup();
    ent.readOnly = false;
    renderWithReport();
    expect(await screen.findByRole("button", { name: "分享" })).toBeTruthy();
  });
});
