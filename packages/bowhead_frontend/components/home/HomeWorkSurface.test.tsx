import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ComponentProps } from "react";
import { reportDisplayTitle } from "@/lib/report-title";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/en-US.json";
import { ApiError } from "@/lib/api-client";
import { fetchMe, requestSpaceAssignment } from "@/lib/auth-api";
import { fetchHomeProposals, fetchHomeSnapshot, type HomeProposalResponse, type HomeSnapshot } from "@/lib/home-api";
// Two telemetry channels centralized in `@/lib/metrics` (2026-08-08). Deduplication is no longer an
// options parameter but **which entry is used**: impression is a counter via `track` (no deduplication),
// start is a click via `trackInteraction` (100ms deduplication). Separate assertions accordingly.
import { track, trackInteraction } from "@/lib/metrics";

import { HomeWorkSurface, homePageTitleKey } from "./HomeWorkSurface";

const push = vi.fn();

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push }),
}));

vi.mock("@/lib/home-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/home-api")>();
  return { ...actual, fetchHomeProposals: vi.fn(), fetchHomeSnapshot: vi.fn() };
});

vi.mock("@/lib/metrics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/metrics")>()),
  track: vi.fn(),
  trackInteraction: vi.fn(),
}));

vi.mock("@/lib/auth-api", () => ({
  fetchMe: vi.fn(),
  requestSpaceAssignment: vi.fn(),
}));

function mockTeamRole(role: string | null): void {
  vi.mocked(fetchMe).mockResolvedValue({
    id: "usr_1",
    email: "someone@example.com",
    team_role: role,
    is_team_admin: role === "team_admin",
    capabilities: { space_admin_v2: true },
  } as Awaited<ReturnType<typeof fetchMe>>);
}

const snapshot: HomeSnapshot = {
  version: "home.snapshot.v1",
  generatedAt: "2026-07-15T09:00:00+00:00",
  scope: { workspaceId: "ws_1", workspaceName: "Bright International" },
  attentionScope: "user",
  attention: [
    {
      id: "notice_1",
      source: "notification",
      nativeType: "approval.required",
      category: "approval",
      priority: "high",
      // Backend snapshots in Chinese; home rewrites by type + metadata in current language.
      title: "有操作需要审批",
      body: "finance_write 请求执行写操作, 风险等级 high",
      metadata: { toolName: "finance_write", riskLevel: "high" },
      actionUrl: "/admin/approvals/",
      subjectType: "approval",
      subjectId: "approval_1",
      createdAt: "2026-07-15T08:55:00+00:00",
    },
  ],
  active: [
    {
      id: "chat:turn_1",
      engine: "chat",
      nativeStatus: "running",
      title: "Prepare the board update",
      subtitle: null,
      progress: null,
      actionUrl: "/?session=session_1",
      sessionId: "session_1",
      startedAt: "2026-07-15T08:50:00+00:00",
      updatedAt: "2026-07-15T08:58:00+00:00",
      metadata: {},
    },
    {
      id: "matrix:run_1",
      engine: "matrix",
      nativeStatus: "running",
      title: "Contract review matrix",
      subtitle: null,
      progress: { completed: 4, total: 10, failed: 0, etaSeconds: null },
      actionUrl: "/knowledge?tab=matrix&view=matrix_1",
      sessionId: null,
      startedAt: "2026-07-15T08:40:00+00:00",
      updatedAt: "2026-07-15T08:59:00+00:00",
      metadata: {},
    },
  ],
  outputs: [
    {
      id: "report:canvas_1",
      kind: "report",
      title: "Q2 operations review",
      format: null,
      actionUrl: "/?session=session_2&report=1",
      sessionId: "session_2",
      createdAt: "2026-07-14T08:00:00+00:00",
      updatedAt: "2026-07-15T07:00:00+00:00",
      metadata: {},
    },
  ],
  counts: { attention: 1, active: 2, outputs: 1 },
  partial: false,
  issues: [],
};

const proposals: HomeProposalResponse = {
  version: "home.proposals.v1",
  generatedAt: "2026-07-15T09:00:00+00:00",
  strategy: "deterministic",
  cached: false,
  generationId: "hp_1",
  items: [
    {
      id: "hpi_1",
      kind: "create",
      title: "Create an editable board deck",
      reason: "Presentation output is available in this space.",
      prompt: "Create an editable board deck from the materials I provide.",
      capabilityRefs: ["skill:pptx"],
      contextRefs: [],
    },
  ],
};

function renderHome(props: Partial<ComponentProps<typeof HomeWorkSurface>> = {}) {
  const onStartPrompt = vi.fn().mockResolvedValue(true);
  const result = render(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <HomeWorkSurface
        workspaceId="ws_1"
        workspaceName="Fallback space"
        workspaceLoading={false}
        readOnly={false}
        onStartPrompt={onStartPrompt}
        {...props}
      />
    </NextIntlClientProvider>,
  );
  return { onStartPrompt, ...result };
}

function homeElement(props: Partial<ComponentProps<typeof HomeWorkSurface>> = {}) {
  return (
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <HomeWorkSurface
        workspaceId="ws_1"
        workspaceName="Fallback space"
        workspaceLoading={false}
        readOnly={false}
        onStartPrompt={vi.fn()}
        {...props}
      />
    </NextIntlClientProvider>
  );
}

describe("HomeWorkSurface", () => {
  beforeEach(() => {
    delete window.__BOWHEAD_BASE_PATH__;
    vi.mocked(track).mockReset();
    vi.mocked(trackInteraction).mockReset();
    vi.mocked(fetchHomeProposals).mockReset();
    vi.mocked(fetchHomeProposals).mockRejectedValue(new ApiError(404, "disabled"));
    vi.mocked(fetchHomeSnapshot).mockReset();
    vi.mocked(fetchHomeSnapshot).mockResolvedValue(snapshot);
    vi.mocked(requestSpaceAssignment).mockResolvedValue({
      status: "submitted",
    });
    push.mockReset();
  });

  afterEach(() => {
    delete window.__BOWHEAD_BASE_PATH__;
    cleanup();
  });

  it.each([
    [{ workspaceId: "ws_1", workspaceLoading: false }, "title"],
    [{ workspaceId: null, workspaceLoading: true }, "title"],
    [{ workspaceId: null, workspaceLoading: false }, "noSpacePageTitle"],
    [{ workspaceId: "ws_1", workspaceLoading: false, workspaceError: "boom" }, "noSpacePageTitle"],
  ] as const)("renders the H1 the chat page uses as its top-bar title (%j)", (props, key) => {
    // The chat page passes homePageTitleKey's text as the bar title so the large-title pattern shows it once.
    expect(homePageTitleKey(props)).toBe(key);
    mockTeamRole("member");
    renderHome(props);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(messages.home[key]);
  });

  it("titles an untitled report output with the localized dated fallback, not a fixed English label", async () => {
    vi.mocked(fetchHomeSnapshot).mockResolvedValue({
      ...snapshot,
      outputs: [{ ...snapshot.outputs[0], title: "" }],
    });
    const { container } = renderHome();
    await screen.findByText(messages.notifications.copy.approval_required_title);
    const title = container.querySelector(".home-work-row-output .home-work-row-title");
    // Same helper as the report list (it reads the static UI locale, which is not the provider locale here).
    expect(title?.textContent).toBe(reportDisplayTitle({ title: "", first_section_title: null, created_at: snapshot.outputs[0].createdAt, id: "" }));
    expect(title?.textContent).toMatch(/^2026-07-1[45] \d{2}:\d{2} /);
  });

  it("renders real action, active work, matrix progress, and output rows", async () => {
    const { container } = renderHome();

    expect(await screen.findByText(messages.notifications.copy.approval_required_title)).toBeInTheDocument();
    expect(screen.getByText("finance_write requests a write operation · risk level high")).toBeInTheDocument();
    expect(screen.queryByText("有操作需要审批")).not.toBeInTheDocument();
    expect(screen.getByText("Prepare the board update")).toBeInTheDocument();
    expect(screen.getByText("Contract review matrix")).toBeInTheDocument();
    expect(screen.getByText("4/10 complete")).toBeInTheDocument();
    expect(screen.getByText("Q2 operations review")).toBeInTheDocument();
    expect(container.querySelector(".home-work-summary-text")).toHaveTextContent("1 item needs your attention.");
    expect(container.querySelector(".home-work-summary-text strong")).toHaveTextContent("1 item");
    expect(screen.queryByText(/2 in progress/i)).not.toBeInTheDocument();
    expect(container.querySelectorAll(".home-work-progress")).toHaveLength(1);
  });

  it("uses the snapshot clock for relative times when the client clock is behind", async () => {
    const clientNow = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-07-15T08:59:30+00:00"));
    try {
      vi.mocked(fetchHomeSnapshot).mockResolvedValueOnce({
        ...snapshot,
        attention: snapshot.attention.map((item) => ({ ...item, createdAt: snapshot.generatedAt })),
      });
      const { container } = renderHome();

      expect(await screen.findByText("Updated now")).toBeInTheDocument();
      expect(container.querySelector(".home-work-row-meta")).toHaveTextContent("now");
      expect(screen.queryByText("Updated in 30 seconds")).not.toBeInTheDocument();
    } finally {
      clientNow.mockRestore();
    }
  });

  it("keeps the calibrated clock moving when a quiet refresh fails", async () => {
    vi.useFakeTimers();
    vi.setSystemTime("2026-07-15T08:59:30+00:00");
    try {
      vi.mocked(fetchHomeSnapshot).mockResolvedValueOnce({
        ...snapshot,
        generatedAt: "2026-07-15T09:00:00+00:00",
      });
      renderHome();
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByText("Updated now")).toBeInTheDocument();

      vi.mocked(fetchHomeSnapshot).mockRejectedValue(new Error("refresh failed"));
      fireEvent.focus(window);
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByText("Some status could not be refreshed. You can still start new work.")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });
      expect(screen.getByText("Updated 2 minutes ago")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows native Matrix state and failed-cell progress", async () => {
    vi.mocked(fetchHomeSnapshot).mockResolvedValueOnce({
      ...snapshot,
      active: snapshot.active.map((item) =>
        item.engine === "matrix"
          ? {
              ...item,
              nativeStatus: "paused",
              progress: { completed: 4, total: 10, failed: 2, etaSeconds: null },
            }
          : item,
      ),
    });

    const { container } = renderHome();

    expect(await screen.findByText("4/10 complete · 2 failed")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(container.querySelector(".home-work-live-paused")).toBeInTheDocument();
  });

  it("does not claim the workbench is clear when the snapshot is partial", async () => {
    vi.mocked(fetchHomeSnapshot).mockResolvedValueOnce({
      ...snapshot,
      attention: [],
      active: [],
      outputs: [],
      counts: { attention: 0, active: 0, outputs: 0 },
      partial: true,
      issues: [{ source: "sessions", code: "source_unavailable" }],
    });

    renderHome();

    expect(await screen.findByText("Some work status is temporarily unavailable, so this view may be incomplete.")).toBeInTheDocument();
    expect(screen.getByText("Some status could not be refreshed. You can still start new work.")).toBeInTheDocument();
    expect(screen.queryByText("Your workbench is clear")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing needs attention or is currently running. Start a new piece of work.")).not.toBeInTheDocument();
  });

  it("never renders a previous space snapshot under the next space", async () => {
    let resolveSecond: ((value: HomeSnapshot) => void) | undefined;
    const second = new Promise<HomeSnapshot>((resolve) => {
      resolveSecond = resolve;
    });
    vi.mocked(fetchHomeSnapshot).mockResolvedValueOnce(snapshot).mockReturnValueOnce(second);

    const { rerender } = render(homeElement());
    expect(await screen.findByText(messages.notifications.copy.approval_required_title)).toBeInTheDocument();

    rerender(homeElement({ workspaceId: "ws_2", workspaceName: "Second space" }));

    expect(screen.queryByText(messages.notifications.copy.approval_required_title)).not.toBeInTheDocument();
    expect(screen.queryByText("Prepare the board update")).not.toBeInTheDocument();
    expect(screen.getByText("Second space")).toBeInTheDocument();
    expect(document.querySelector(".home-work-skeleton")).toBeInTheDocument();

    resolveSecond?.({
      ...snapshot,
      scope: { workspaceId: "ws_2", workspaceName: "Second space" },
      attention: [],
      active: [],
      outputs: [],
      counts: { attention: 0, active: 0, outputs: 0 },
    });
    expect(await screen.findByText("Your workbench is clear")).toBeInTheDocument();
  });

  it("refreshes the space list and clears facts when snapshot returns 404", async () => {
    const onWorkspaceInvalidated = vi.fn();
    vi.mocked(fetchHomeSnapshot).mockRejectedValueOnce(new ApiError(404, "workspace not found"));

    renderHome({ onWorkspaceInvalidated });

    await waitFor(() => expect(onWorkspaceInvalidated).toHaveBeenCalledOnce());
    expect(await screen.findByText("Work status is temporarily unavailable")).toBeInTheDocument();
    expect(document.querySelector(".home-work-skeleton")).not.toBeInTheDocument();
  });

  it("treats a disabled snapshot endpoint as partial instead of invalidating the space", async () => {
    const onWorkspaceInvalidated = vi.fn();
    vi.mocked(fetchHomeSnapshot).mockRejectedValueOnce(new ApiError(404, "home snapshot disabled", { code: "home_snapshot_disabled" }));

    renderHome({ onWorkspaceInvalidated });

    expect(await screen.findByText("Some work status is temporarily unavailable, so this view may be incomplete.")).toBeInTheDocument();
    expect(onWorkspaceInvalidated).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "Work status is temporarily unavailable" })).not.toBeInTheDocument();
    expect(screen.queryByText("Your workbench is clear")).not.toBeInTheDocument();
  });

  it("opens snapshot items through their server-provided action URL", async () => {
    renderHome();
    fireEvent.click(await screen.findByText(messages.notifications.copy.approval_required_title));
    expect(push).toHaveBeenCalledWith("/admin/approvals/");
  });

  it("opens a stored legacy /memory action URL in Settings > Memory", async () => {
    vi.mocked(fetchHomeSnapshot).mockResolvedValue({
      ...snapshot,
      attention: [{ ...snapshot.attention[0], actionUrl: "/memory/?item=mem_1" }],
    });
    renderHome();
    fireEvent.click(await screen.findByText(messages.notifications.copy.approval_required_title));
    expect(push).toHaveBeenCalledWith("/settings/memory/?item=mem_1");
  });

  it("delegates prefixed snapshot navigation to the app router", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    renderHome();

    fireEvent.click(await screen.findByText(messages.notifications.copy.approval_required_title));

    expect(push).toHaveBeenCalledWith("/admin/approvals/");
  });

  it("starts a proposal from one whole-row button without nested actions", async () => {
    const { container, onStartPrompt } = renderHome();
    const action = await screen.findByRole("button", { name: "Start: Produce a finished deliverable" });
    const title = screen.getByText("Produce a finished deliverable");
    const detail = screen.getByText("Start from existing material and confirm audience, format, and completion criteria.");

    expect(onStartPrompt).not.toHaveBeenCalled();
    expect(title.closest("button")).toBe(action);
    expect(detail.closest("button")).toBe(action);
    expect(container.querySelector("button button")).not.toBeInTheDocument();

    fireEvent.click(title);
    expect(onStartPrompt).toHaveBeenCalledWith(messages.home.proposal.createPrompt);
    await waitFor(() =>
      expect(trackInteraction).toHaveBeenCalledWith("bowhead_home_proposal_ui_total", {
        action: "start",
        strategy: "local",
        kind: "create",
        accepted: "true",
      }),
    );
  });

  it("uses capability-backed proposals when the endpoint succeeds", async () => {
    vi.mocked(fetchHomeProposals).mockResolvedValueOnce(proposals);
    const { onStartPrompt } = renderHome();

    const action = await screen.findByRole("button", { name: "Start: Create an editable board deck" });
    expect(screen.getByText("Presentation output is available in this space.")).toBeInTheDocument();
    fireEvent.click(action);

    expect(onStartPrompt).toHaveBeenCalledWith("Create an editable board deck from the materials I provide.");
  });

  it("records proposal impressions only after the endpoint settles", async () => {
    let resolveProposals: ((value: HomeProposalResponse) => void) | undefined;
    vi.mocked(fetchHomeProposals).mockReturnValueOnce(
      new Promise<HomeProposalResponse>((resolve) => {
        resolveProposals = resolve;
      }),
    );
    renderHome();

    expect(await screen.findByText(messages.notifications.copy.approval_required_title)).toBeInTheDocument();
    expect(track).not.toHaveBeenCalledWith("bowhead_home_proposal_ui_total", expect.objectContaining({ action: "impression" }));

    resolveProposals?.(proposals);

    await waitFor(() =>
      expect(track).toHaveBeenCalledWith("bowhead_home_proposal_ui_total", {
        action: "impression",
        strategy: "deterministic",
        kind: "create",
        accepted: "na",
      }),
    );
    expect(track).not.toHaveBeenCalledWith("bowhead_home_proposal_ui_total", expect.objectContaining({ action: "impression", strategy: "local" }));
  });

  it("records every same-kind proposal impression in a deliberate batch", async () => {
    vi.mocked(fetchHomeProposals).mockResolvedValueOnce({
      ...proposals,
      items: [
        proposals.items[0],
        {
          ...proposals.items[0],
          id: "hpi_2",
          title: "Create a second deliverable",
          prompt: "Create a second editable deliverable from the materials I provide.",
        },
      ],
    });
    renderHome();

    expect(await screen.findByText("Create a second deliverable")).toBeInTheDocument();
    await waitFor(() => {
      const impressions = vi.mocked(track).mock.calls.filter(([, labels]) => labels?.action === "impression" && labels.kind === "create");
      expect(impressions).toHaveLength(2);
      // No deduplication is now structural: impression uses the non-deduplicating `track` entry,
      // rather than passing `{dedupe:false}` to `trackInteraction`.
      expect(vi.mocked(trackInteraction).mock.calls.some(([, labels]) => labels?.action === "impression")).toBe(false);
    });
  });

  it("never renders a previous space proposal under the next space", async () => {
    let resolveSecond: ((value: HomeProposalResponse) => void) | undefined;
    const second = new Promise<HomeProposalResponse>((resolve) => {
      resolveSecond = resolve;
    });
    vi.mocked(fetchHomeProposals).mockResolvedValueOnce(proposals).mockReturnValueOnce(second);
    vi.mocked(fetchHomeSnapshot).mockImplementation(async (workspaceId) => ({
      ...snapshot,
      scope: { workspaceId, workspaceName: workspaceId === "ws_1" ? "Bright International" : "Second space" },
    }));

    const { rerender } = render(homeElement());
    expect(await screen.findByText("Create an editable board deck")).toBeInTheDocument();

    rerender(homeElement({ workspaceId: "ws_2", workspaceName: "Second space" }));

    expect(screen.queryByText("Create an editable board deck")).not.toBeInTheDocument();
    expect(await screen.findByText("Produce a finished deliverable")).toBeInTheDocument();

    resolveSecond?.({
      ...proposals,
      generationId: "hp_2",
      items: [
        {
          ...proposals.items[0],
          id: "hpi_2",
          title: "Create a second-space brief",
        },
      ],
    });
    expect(await screen.findByText("Create a second-space brief")).toBeInTheDocument();
  });

  it("keeps whole proposal rows disabled in read-only mode without start telemetry", async () => {
    const { onStartPrompt } = renderHome({ readOnly: true });
    await waitFor(() => expect(fetchHomeSnapshot).toHaveBeenCalled());
    const action = screen.getByRole("button", { name: "Start: Turn an outcome into an action plan" });
    expect(action).toBeDisabled();
    expect(screen.getByRole("button", { name: "Start: Produce a finished deliverable" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Start: Design a recurring workflow" })).toBeDisabled();

    fireEvent.click(action);
    expect(onStartPrompt).not.toHaveBeenCalled();
    expect(vi.mocked(trackInteraction).mock.calls.some(([, labels]) => labels?.action === "start")).toBe(false);
  });

  it("offers space management to an admin when no space exists", async () => {
    mockTeamRole("owner");
    renderHome({ workspaceId: null });

    expect(screen.getByRole("heading", { name: "Create or join a space" })).toBeInTheDocument();
    expect(screen.queryByText("Your workbench is clear")).not.toBeInTheDocument();
    expect(fetchHomeSnapshot).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("button", { name: /Manage spaces/i }));
    expect(push).toHaveBeenCalledWith("/team/settings/spaces/?from=no-space");
  });

  it("tells a plain member to ask an admin instead of offering a page they cannot open", async () => {
    // A freshly created member lands here with no space. Team settings rejects
    // non-admins, so offering "Manage spaces" strands them on a denial page —
    // which reads as "I can't log in".
    mockTeamRole("member");
    renderHome({ workspaceId: null });

    expect(await screen.findByText(messages.home.noSpaceAskAdminDetail)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Manage spaces/i })).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalledWith("/team/settings/spaces/?from=no-space");

    fireEvent.click(screen.getByRole("button", { name: /Request space access/i }));
    await waitFor(() => expect(requestSpaceAssignment).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: /Request sent/i })).toBeDisabled();
  });

  it("keeps the space-management CTA hidden when the role cannot be resolved", async () => {
    vi.mocked(fetchMe).mockRejectedValue(new Error("offline"));
    renderHome({ workspaceId: null });

    expect(await screen.findByText(messages.home.noSpaceAskAdminDetail)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Manage spaces/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request space access/i })).not.toBeInTheDocument();
  });

  it("shows a retry state instead of treating a failed space lookup as no access", async () => {
    const refresh = vi.fn();
    renderHome({
      workspaceId: null,
      workspaceError: "workspaces: 503",
      onWorkspaceInvalidated: refresh,
    });

    expect(
      screen.getByRole("heading", { name: messages.home.spaceListUnavailableTitle }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request space access/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Reload/i }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("shows a retry state instead of an empty workbench when the snapshot fails", async () => {
    vi.mocked(fetchHomeSnapshot).mockRejectedValueOnce(new Error("unavailable"));
    renderHome();

    expect(await screen.findByRole("heading", { name: "Work status is temporarily unavailable" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: messages.home.proposalsTitle })).toBeInTheDocument();
    expect(screen.getByText("Produce a finished deliverable")).toBeInTheDocument();
    expect(screen.queryByText("Your workbench is clear")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(track).toHaveBeenCalledWith("bowhead_home_proposal_ui_total", expect.objectContaining({ action: "impression", strategy: "local" })),
    );
  });

  it("contains a malformed snapshot response as a local retry state", async () => {
    vi.mocked(fetchHomeSnapshot).mockResolvedValueOnce({} as HomeSnapshot);
    renderHome();

    expect(await screen.findByRole("heading", { name: "Work status is temporarily unavailable" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: messages.home.proposalsTitle })).toBeInTheDocument();
    expect(screen.queryByText("Your workbench is clear")).not.toBeInTheDocument();
  });

  describe("single-column home (Workbench Spec section 1)", () => {
    /** Document order (also Tab/screen-reader order): title, composer slot, first proposal card, first work row. */
    function documentOrder(container: HTMLElement): string[] {
      const marks: Array<[string, Element | null]> = [
        ["title", container.querySelector(".home-work-header h1")],
        ["composer", container.querySelector("[data-testid='slot-composer']")],
        ["proposal", container.querySelector(".home-work-proposal")],
        ["work", container.querySelector(".home-work-row")],
      ];
      return marks
        .filter((entry): entry is [string, Element] => entry[1] !== null)
        .sort((a, b) => (a[1].compareDocumentPosition(b[1]) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
        .map(([name]) => name);
    }

    it("title, composer, proposal cards, work groups: DOM order matches the visual order", async () => {
      vi.mocked(fetchHomeProposals).mockResolvedValue(proposals);
      const { container } = renderHome({ composerSlot: <textarea data-testid="slot-composer" aria-label="Composer" /> });

      await screen.findByText("Create an editable board deck");
      await screen.findByText("Q2 operations review");
      expect(documentOrder(container)).toEqual(["title", "composer", "proposal", "work"]);
      expect(container.querySelector(".home-work-composer-slot")).toContainElement(screen.getByTestId("slot-composer"));
      // Both section headings remain for screen readers; proposals precede brief.
      const sections = Array.from(container.querySelectorAll(".home-work-layout > section"), (el) => el.className);
      expect(sections).toEqual(["home-work-proposals", "home-work-brief"]);
    });

    it("brand appears once: the home page has no watermark or wordmark, only the updated time", async () => {
      vi.mocked(fetchHomeProposals).mockResolvedValue(proposals);
      const { container } = renderHome();
      await screen.findByText("Q2 operations review");
      expect(container.querySelector(".home-work-watermark")).toBeNull();
      expect(container.querySelector(".home-work-brand-copy, .home-work-brand-graphic")).toBeNull();
      expect(container.querySelector(".home-work-brand--time-only time.home-work-updated")).not.toBeNull();
    });

    it("read-only proposal cards are disabled and the reason is written below them", async () => {
      vi.mocked(fetchHomeProposals).mockResolvedValue(proposals);
      renderHome({ readOnly: true });
      const card = await screen.findByRole("button", { name: /Create an editable board deck/ });
      expect(card).toBeDisabled();
      expect(screen.getByTestId("home-proposals-blocked")).toHaveTextContent(messages.home.proposalsReadOnly);
    });
  });
});
