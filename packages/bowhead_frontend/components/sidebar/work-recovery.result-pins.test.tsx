/**
 * Result pins in the sidebar's pinned group (result-pins PRD §4.2, D1, D5): conversations and pinned results together
 * by last change, every pinned row with its kind's icon, Unpin from the row menu, and a load failure that keeps the
 * pinned conversations.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { fetchSessions } from "@/lib/api";
import { resetResultPinsForTests, type ResultPin } from "@/lib/result-pins";
import type { SessionGroup } from "@/lib/types";

import { WorkRecovery } from "./work-recovery";

const { toastMock } = vi.hoisted(() => ({ toastMock: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchSessions: vi.fn(),
}));
vi.mock("@/lib/use-toast", () => ({ toast: toastMock }));

const GROUPS: SessionGroup[] = [
  {
    id: "today",
    label: "TODAY",
    items: [
      {
        id: "s_pinned",
        title: "季度复盘会话",
        intents: [],
        time: "2m",
        state: "done",
        pinned: true,
        updatedAt: "2026-11-24T09:00:00+00:00",
      },
      {
        id: "s_plain",
        title: "渠道分析",
        intents: [],
        time: "5m",
        state: "done",
        updatedAt: "2026-11-24T08:30:00+00:00",
      },
    ],
  },
];

const FILE_PIN: ResultPin = {
  kind: "file",
  pin_id: "art_root",
  artifact_id: "art_v3",
  title: "季度复盘.pptx",
  mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  version_no: 3,
  session_id: "s_pinned",
  opens: "session",
  changed_at: "2026-11-24T10:00:00+00:00",
  pinned_at: "2026-11-24T10:01:00+00:00",
};

const REPORT_PIN: ResultPin = {
  kind: "report",
  pin_id: "cv_1",
  title: "",
  first_section_title: "门店经营三视角",
  created_at: "2026-11-20T08:00:00+00:00",
  session_id: "s_other",
  opens: "session",
  changed_at: "2026-11-24T08:00:00+00:00",
  pinned_at: "2026-11-24T08:01:00+00:00",
};

const fetchMock = vi.fn();

function respond(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function routeFetch(listStatus = 200) {
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/api/v1/result-pins") && (init?.method ?? "GET") === "GET") {
      return listStatus === 200 ? respond(200, { items: [FILE_PIN, REPORT_PIN] }) : respond(listStatus, {});
    }
    if (url.includes("/api/v1/result-pins/") && init?.method === "DELETE") return respond(204, null);
    return respond(404, {});
  });
}

function renderPins(overrides: Partial<React.ComponentProps<typeof WorkRecovery>> = {}) {
  const props: React.ComponentProps<typeof WorkRecovery> = {
    groups: GROUPS,
    currentSessionId: null,
    query: "",
    onQueryChange: vi.fn(),
    onPick: vi.fn(),
    onTogglePin: vi.fn(),
    resultPinsEnabled: true,
    onOpenResultPin: vi.fn((event) => event.preventDefault()),
    ...overrides,
  };
  return { ...renderIntl(<WorkRecovery {...props} />), props };
}

function pinnedGroup(): HTMLElement {
  const heading = screen.getByRole("heading", { name: "置顶", level: 3 });
  return heading.closest(".unified-work-group") as HTMLElement;
}

describe("WorkRecovery pinned results", () => {
  beforeEach(() => {
    resetResultPinsForTests();
    toastMock.mockReset();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.mocked(fetchSessions).mockReset().mockResolvedValue({ groups: [], archived_count: 0 });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    resetResultPinsForTests();
  });

  it("merges pinned conversations and results by last change, each with its kind's icon", async () => {
    routeFetch();
    renderPins();

    await waitFor(() => expect(screen.getByText("季度复盘.pptx")).toBeTruthy());
    const group = pinnedGroup();
    expect(
      Array.from(group.querySelectorAll(".unified-work-title")).map((node) => node.textContent),
    ).toEqual(["季度复盘.pptx", "季度复盘会话", "门店经营三视角"]);
    expect(group.querySelectorAll(".unified-work-item-icon")).toHaveLength(3);
    // Conversations outside the pinned group keep their plain rows.
    const plainRow = screen.getByText("渠道分析").closest(".unified-work-item") as HTMLElement;
    expect(plainRow.querySelector(".unified-work-item-icon")).toBeNull();
    // Second line: kind, version above 1.
    expect(within(group).getByText("PPTX · 第 3 版")).toBeTruthy();
    expect(within(group).getByText("报告")).toBeTruthy();
  });

  it("links each result to where it opens and hands the click to the owner", async () => {
    routeFetch();
    const { props } = renderPins();

    const fileLink = await screen.findByRole("link", { name: /季度复盘\.pptx/ });
    expect(fileLink.getAttribute("href")).toBe("/?session=s_pinned&result=art_v3&mode=preview");
    expect(screen.getByRole("link", { name: /门店经营三视角/ }).getAttribute("href")).toBe("/?session=s_other&report=1");

    fireEvent.click(fileLink);
    expect(props.onOpenResultPin).toHaveBeenCalledWith(expect.anything(), FILE_PIN);
  });

  it("unpins from the row menu and drops the row", async () => {
    routeFetch();
    renderPins();

    fireEvent.click(await screen.findByRole("button", { name: "更多成果操作：季度复盘.pptx" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "取消置顶" }));

    await waitFor(() => expect(screen.queryByText("季度复盘.pptx")).toBeNull());
    const deleteCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "DELETE");
    expect(String(deleteCall?.[0])).toContain("/api/v1/result-pins/file/art_root");
    expect(toastMock).toHaveBeenCalledWith({ kind: "success", text: "已取消置顶" });
  });

  it("keeps pinned conversations and offers Retry when the pinned results fail to load", async () => {
    routeFetch(500);
    renderPins();

    expect(await screen.findByText(/置顶的成果暂时无法加载/)).toBeTruthy();
    expect(within(pinnedGroup()).getByText("季度复盘会话")).toBeTruthy();

    routeFetch();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("季度复盘.pptx")).toBeTruthy();
  });

  it("hides the pinned group while searching", async () => {
    routeFetch();
    renderPins({ query: "复盘" });
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/v1/result-pins"))).toBe(true),
    );
    expect(screen.queryByRole("heading", { name: "置顶", level: 3 })).toBeNull();
    expect(document.querySelector("[data-result-pin]")).toBeNull();
  });

  it("hides pinned results in the archived view", async () => {
    routeFetch();
    renderPins({ archiveEnabled: true, archivedCount: 1 });
    expect(await screen.findByText("季度复盘.pptx")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "1 个已归档会话" }));

    await screen.findByRole("heading", { name: "已归档", level: 2 });
    expect(document.querySelector("[data-result-pin]")).toBeNull();
  });

  it("requests nothing and adds no icons when the owner does not enable result pins", async () => {
    routeFetch();
    renderPins({ resultPinsEnabled: false });

    const group = pinnedGroup();
    expect(within(group).getByText("季度复盘会话")).toBeTruthy();
    expect(group.querySelector(".unified-work-item-icon")).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/v1/result-pins"))).toBe(false);
  });
});
