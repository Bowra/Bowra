import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction } from "@/__tests__/row-actions";

const api = vi.hoisted(() => ({
  fetchReports: vi.fn(),
  /** Paged override; by default a page wraps the fetchReports rows with their count and no further page. */
  fetchReportsPage: null as null | ((options: Record<string, unknown>) => Promise<unknown>),
}));

const deleteCanvasMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/use-toast", async (orig) => ({ ...(await orig<Record<string, unknown>>()), toast: toastMock }));
vi.mock("@/lib/api", () => ({
  deleteCanvas: deleteCanvasMock,
  exportJobDownloadUrl: vi.fn(),
  fetchReports: api.fetchReports,
  fetchReportsPage: (options: Record<string, unknown>) =>
    api.fetchReportsPage
      ? api.fetchReportsPage(options)
      : Promise.resolve(api.fetchReports(options)).then((items: unknown[]) => ({
          items,
          total: items.length,
          next_cursor: null,
          has_more: false,
        })),
  setCanvasStatus: vi.fn(),
}));
vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/canvas/export-ui", () => ({
  ExportToast: () => null,
  ExportsDrawer: () => null,
}));
vi.mock("@/components/canvas/report-status", () => ({
  ReportStatusControl: () => null,
}));
vi.mock("@/components/share/share-dialog", () => ({
  ShareDialog: () => null,
}));
vi.mock("@/lib/metrics", () => ({
  track: vi.fn(),
}));
vi.mock("@/lib/use-canvas-export", () => ({
  useCanvasExport: () => ({
    busy: false,
    dismiss: vi.fn(),
    retry: vi.fn(),
    retryWithDefaultTheme: vi.fn(),
    start: vi.fn(),
    state: { lastArgs: null },
  }),
}));
// Reports default scope follows the current Space. The mutable stub covers no-current-space
// and current-space cases; default "" preserves existing-case behavior verbatim.
const spaceState = vi.hoisted(() => ({ activeId: "" }));
vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({ items: [], activeId: spaceState.activeId || null }),
  getActiveSpace: (fallback: string) => spaceState.activeId || fallback,
}));

import ReportsPage from "./page";

describe("ReportsPage loading summary", () => {
  afterEach(() => {
    cleanup();
    api.fetchReports.mockReset();
    spaceState.activeId = "";
  });

  it("does not present the initial empty array as zero reports", async () => {
    let resolveReports: ((reports: []) => void) | undefined;
    const reportsPromise = new Promise<[]>((resolve) => {
      resolveReports = resolve;
    });
    api.fetchReports.mockImplementation((options: { scope?: string }) => (
      options.scope === "shared" ? Promise.resolve([]) : reportsPromise
    ));

    renderIntl(<ReportsPage />);

    // First batch: skeleton, no count yet; the header carries the fixed description (no eyebrow, no Back to home).
    expect(screen.getByRole("status", { name: "报告加载中" })).toBeTruthy();
    expect(screen.queryByTestId("reports-total")).toBeNull();
    expect(screen.getByText("对话里生成的报告，可以打开、分享或下载。")).toBeTruthy();
    expect(screen.queryByText("回主页")).toBeNull();

    await act(async () => {
      resolveReports?.([]);
      await reportsPromise;
    });

    expect((await screen.findByTestId("reports-total")).textContent).toBe("共 0 份");
  });

  it("filters empty canvases on the server before LIMIT and refetches when explicitly shown", async () => {
    api.fetchReports.mockResolvedValue([]);

    renderIntl(<ReportsPage />);

    await waitFor(() => {
      expect(api.fetchReports).toHaveBeenCalledWith(expect.objectContaining({
        include_empty: false,
        limit: 50,
      }));
      expect(api.fetchReports).toHaveBeenCalledWith(expect.objectContaining({
        include_empty: false,
        limit: 50,
        scope: "shared",
      }));
    });

    fireEvent.click(screen.getByRole("button", { name: "显示未生成内容" }));

    await waitFor(() => expect(api.fetchReports).toHaveBeenCalledWith(
      expect.objectContaining({ include_empty: true, limit: 50 }),
    ));
  });

  // An empty default omits workspace_id and lists reports from every Space.
  // workspace_id, listing all spaces. Sessions already defaulted to current space; enforce Reports alignment here.
  it("defaults the library to the current Space instead of listing every Space", async () => {
    spaceState.activeId = "ws_current";
    api.fetchReports.mockResolvedValue([]);

    renderIntl(<ReportsPage />);

    await waitFor(() => expect(api.fetchReports).toHaveBeenCalledWith(
      expect.objectContaining({ workspace_id: "ws_current" }),
    ));
    // Negative control: never fall back to the parameterless cross-space path.
    expect(api.fetchReports).not.toHaveBeenCalledWith(
      expect.objectContaining({ workspace_id: undefined, scope: undefined }),
    );
  });

  it("falls back to every Space when no current Space is known", async () => {
    api.fetchReports.mockResolvedValue([]);

    renderIntl(<ReportsPage />);

    // Empty localStorage (new browser/private mode) makes getActiveSpace return "" ⇒ still cross-space overview.
    await waitFor(() => expect(api.fetchReports).toHaveBeenCalledWith(
      expect.objectContaining({ sort: "updated_desc", include_empty: false }),
    ));
    for (const [options] of api.fetchReports.mock.calls) {
      expect((options as { workspace_id?: string }).workspace_id).toBeUndefined();
    }
  });
});

describe("ReportsPage load failure copy", () => {
  afterEach(() => {
    cleanup();
    api.fetchReports.mockReset();
  });

  it.each([
    ["zh-CN", "请稍后点「重试」；如果一直失败，请联系团队管理员。"],
    ["en-US", "Select Retry in a moment. If it keeps failing, contact your team admin."],
    ["de-DE", "Wählen Sie in Kürze „Erneut versuchen“. Bei wiederholten Fehlern wenden Sie sich an Ihren Teamadministrator."],
    ["fr-FR", "Sélectionnez Réessayer dans un instant. Si le problème persiste, contactez l’administrateur de votre équipe."],
  ] as const)("says what to do instead of showing the status code (%s)", async (locale, hint) => {
    api.fetchReports.mockRejectedValue(Object.assign(new Error("reports: 500"), { status: 500 }));

    renderIntl(<ReportsPage />, { locale });

    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert.textContent).toContain(hint));
    expect(alert.textContent).not.toMatch(/\b500\b|reports:/);
    // Server answered 5xx: not a network problem; never ask users to check their network.
    expect(alert.textContent).not.toMatch(/网络|connection/);
    // Diagnostics flag off: no status code on page, only Copy Diagnostics button outside the announcement container.
    const copy = await screen.findByTestId("error-copy-diagnostics");
    expect(alert.contains(copy)).toBe(false);
    expect(document.body.textContent).not.toMatch(/\b500\b/);
  });

  it("asks the user to check the connection only when the request never reached the server", async () => {
    api.fetchReports.mockRejectedValue(new TypeError("Failed to fetch"));

    renderIntl(<ReportsPage />);

    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert.textContent).toContain("请检查网络后点「重试」"));
  });

  it("offers Copy diagnostics on the delete-failed toast", async () => {
    const reportItem = {
      id: "cv_1",
      session_id: "s_1",
      workspace_id: "ws_1",
      title: "季度复盘",
      status: "active",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
      sections_count: 3,
    };
    api.fetchReports.mockImplementation((options: { scope?: string }) => Promise.resolve(options.scope === "shared" ? [] : [reportItem]));
    deleteCanvasMock.mockRejectedValue(Object.assign(new Error("delete canvas: 500"), { status: 500, requestId: "0123456789abcdef0123456789abcdef" }));
    renderIntl(<ReportsPage />);
    await screen.findByText("季度复盘");

    await chooseRowAction("「季度复盘」的更多操作", /^删除/, { danger: true });
    fireEvent.click(await screen.findByRole("button", { name: "删除报告" }));

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({
      kind: "error",
      text: "没能删除这份报告。请刷新列表确认后再试。",
      action: expect.objectContaining({ label: "复制诊断" }),
    })));
  });
});

describe("ReportsPage list rows and controls", () => {
  afterEach(() => {
    cleanup();
    api.fetchReports.mockReset();
  });

  it("writes section counts as copy, dates in a <time> with the full value on hover, and menu buttons instead of selects", async () => {
    const report = {
      id: "cv_1",
      session_id: "s_1",
      workspace_id: "ws_1",
      title: "季度复盘",
      status: "active",
      created_at: "2025-12-30T12:00:00Z",
      updated_at: "2025-12-30T12:00:00Z",
      sections_count: 12,
    };
    api.fetchReports.mockImplementation((options: { scope?: string }) => Promise.resolve(options.scope === "shared" ? [] : [report]));
    renderIntl(<ReportsPage />);
    await screen.findByText("季度复盘");

    expect(screen.getByText("12 个区块")).toBeTruthy();
    expect(screen.queryByText(/sections/)).toBeNull();
    const time = document.querySelector("time.reports-list-time")!;
    expect(time.getAttribute("dateTime")).toBe(new Date(report.updated_at).toISOString());
    expect(time.textContent).toBe("2025年12月30日");
    expect(time.getAttribute("title")).toMatch(/^2025年12月30日 \d{2}:\d{2}$/);

    expect(document.querySelector(".reports-library-controls select")).toBeNull();
    expect(screen.getByTestId("reports-space-filter").textContent).toBe("空间：全部");
    const sort = screen.getByTestId("reports-sort");
    expect(sort.textContent).toBe("排序：最近更新");
    fireEvent.click(sort, { detail: 0 });
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "区块最多" }));
    await waitFor(() => expect(api.fetchReports).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "sections_desc" })));
  });
});

describe("ReportsPage download menu", () => {
  afterEach(() => {
    cleanup();
    api.fetchReports.mockReset();
  });

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

  async function openByKeyboard() {
    api.fetchReports.mockImplementation((options: { scope?: string }) => (
      Promise.resolve(options.scope === "shared" ? [] : [report])
    ));
    renderIntl(<ReportsPage />);
    const trigger = await screen.findByRole("button", { name: "下载" });
    fireEvent.click(trigger, { detail: 0 });
    const [pdf] = await screen.findAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(pdf));
    return trigger;
  }

  it("moves between formats with the arrow keys and returns focus on Escape", async () => {
    const trigger = await openByKeyboard();
    const menu = screen.getByRole("menu");
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toContain("PPTX");

    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("closes on a press outside", async () => {
    await openByKeyboard();

    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

// Header, Language & Paging Spec §3 (D8): batches of 50, a manual "Load more" footer under the list, the true
// total in the toolbar, and the footer states (loading, failure + retry, all shown, search wording).
describe("ReportsPage paging footer", { timeout: 20_000 }, () => {
  type Opts = { scope?: string; cursor?: string | null; q?: string; include_empty?: boolean; limit?: number; sort?: string };
  const row = (n: number) => ({
    id: `cv_${n}`,
    session_id: `s_${n}`,
    workspace_id: "ws_1",
    title: `报告 ${n}`,
    status: "active",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    sections_count: 3,
  });
  const rows = (from: number, to: number) => Array.from({ length: to - from }, (_, index) => row(from + index));
  const empty = { items: [], total: 0, next_cursor: null, has_more: false };

  /** Three batches of a 120-report library: 50 + 50 + 20; the first page also counts 8 hidden empty reports. */
  function library(total = 120, overrides: Partial<Record<string, () => Promise<unknown>>> = {}) {
    const calls: Opts[] = [];
    api.fetchReportsPage = (options: Record<string, unknown>) => {
      const opts = options as Opts;
      calls.push(opts);
      if (opts.scope === "shared") return Promise.resolve(empty);
      const key = opts.cursor ?? "first";
      if (overrides[key]) return overrides[key]!();
      if (key === "first") return Promise.resolve({ items: rows(0, Math.min(50, total)), total, empty_total: opts.include_empty ? null : 8, next_cursor: total > 50 ? "c1" : null, has_more: total > 50 });
      if (key === "c1") return Promise.resolve({ items: rows(50, Math.min(100, total)), total: null, next_cursor: total > 100 ? "c2" : null, has_more: total > 100 });
      return Promise.resolve({ items: rows(100, total), total: null, next_cursor: null, has_more: false });
    };
    return calls;
  }

  afterEach(() => {
    cleanup();
    api.fetchReportsPage = null;
    api.fetchReports.mockReset();
  });

  it("a single batch shows only the toolbar count, and the empty-report count on its toggle", async () => {
    library(3);
    renderIntl(<ReportsPage />);
    await screen.findByText("报告 2", {}, { timeout: 5000 });
    expect(screen.getByTestId("reports-total").textContent).toBe("共 3 份");
    expect(screen.queryByTestId("reports-list-footer")).toBeNull();
    await waitFor(() => expect(screen.getByRole("button", { name: /显示未生成内容/ }).textContent).toBe("显示未生成内容8"));
  });

  it("loads the next batch in place: busy button, focus on the first new row, progress announced", async () => {
    let releaseSecond: () => void = () => {};
    const calls = library(120, {
      c1: () => new Promise((resolve) => {
        releaseSecond = () => resolve({ items: rows(50, 100), total: null, next_cursor: "c2", has_more: true });
      }),
    });
    renderIntl(<ReportsPage />);
    await screen.findByText("报告 49", {}, { timeout: 5000 });
    expect(screen.getByTestId("reports-total").textContent).toBe("共 120 份");
    expect(screen.getByTestId("reports-progress").textContent).toBe("显示 50 / 共 120 份");

    fireEvent.click(screen.getByTestId("reports-load-more"));
    const busy = screen.getByTestId("reports-load-more");
    expect(busy.getAttribute("aria-busy")).toBe("true");
    expect(busy.textContent).toBe("正在加载…");
    // A second click while loading does not request again.
    fireEvent.click(busy);
    expect(calls.filter((call) => call.cursor === "c1")).toHaveLength(1);

    await act(async () => releaseSecond());
    await screen.findByText("报告 99", {}, { timeout: 5000 });
    expect(screen.getByTestId("reports-progress").textContent).toBe("显示 100 / 共 120 份");
    await waitFor(() => expect(document.activeElement?.textContent).toBe("报告 50"));
    expect(await screen.findByText("已加载 50 份，显示 100 / 共 120 份")).toBeTruthy();

    fireEvent.click(screen.getByTestId("reports-load-more"));
    await screen.findByText("报告 119", {}, { timeout: 5000 });
    expect(screen.getByTestId("reports-all-loaded").textContent).toBe("已显示全部 120 份");
    expect(screen.queryByTestId("reports-load-more")).toBeNull();
  });

  it("a failed batch keeps the loaded rows and retries with the same cursor", async () => {
    let fail = true;
    const calls = library(120, {
      c1: () => (fail ? Promise.reject(Object.assign(new Error("reports: 500"), { status: 500 })) : Promise.resolve({ items: rows(50, 100), total: null, next_cursor: "c2", has_more: true })),
    });
    renderIntl(<ReportsPage />);
    await screen.findByText("报告 49", {}, { timeout: 5000 });
    fireEvent.click(screen.getByTestId("reports-load-more"));
    const error = await screen.findByTestId("reports-load-more-error", {}, { timeout: 5000 });
    expect(error.textContent).toContain("没有加载出更多报告。已显示的 50 份不受影响。");
    expect(error.textContent).not.toMatch(/\b500\b/);
    expect(screen.getByText("报告 0")).toBeTruthy();

    fail = false;
    fireEvent.click(within(error).getByRole("button", { name: "重试" }));
    await screen.findByText("报告 99", {}, { timeout: 5000 });
    expect(calls.filter((call) => call.cursor === "c1")).toHaveLength(2);
  });

  it("search uses the found wording and a new condition restarts from the first batch", async () => {
    const calls = library(74);
    renderIntl(<ReportsPage />);
    await screen.findByText("报告 49", {}, { timeout: 5000 });
    fireEvent.change(screen.getByPlaceholderText("搜索标题或正文全文…"), { target: { value: "复盘" } });
    await waitFor(() => expect(screen.getByTestId("reports-total").textContent).toBe("找到 74 份"));
    expect(screen.getByTestId("reports-progress").textContent).toBe("显示 50 / 找到 74 份");
    const searchCalls = calls.filter((call) => call.q === "复盘" && call.limit === 50 && call.scope !== "shared");
    expect(searchCalls[0].cursor).toBeUndefined();
  });

  it("a silent refresh merges the first batch on top and keeps later batches", async () => {
    const calls = library(120);
    renderIntl(<ReportsPage />);
    await screen.findByText("报告 49", {}, { timeout: 5000 });
    fireEvent.click(screen.getByTestId("reports-load-more"));
    await screen.findByText("报告 99", {}, { timeout: 5000 });

    await act(async () => {
      window.dispatchEvent(new Event("bowhead.canvas.changed"));
    });
    await waitFor(() => expect(calls.filter((call) => !call.cursor && call.limit === 50 && call.scope !== "shared")).toHaveLength(2));
    expect(screen.getByText("报告 99")).toBeTruthy();
    expect(screen.getByTestId("reports-progress").textContent).toBe("显示 100 / 共 120 份");
  });
});
