import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  toast: vi.fn(),
  push: vi.fn(),
  apiFetch: vi.fn(),
  fetchNotificationCount: vi.fn(),
  fetchBrowserCandidates: vi.fn(),
  markNotificationRead: vi.fn(async (_id: string) => undefined),
  setLastTeam: vi.fn(async (_id: string) => ({ ok: "true", team_id: "team_b" })),
}));

vi.mock("@/lib/use-toast", () => ({ toast: (...args: unknown[]) => mocks.toast(...args) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: mocks.push }) }));
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: () => (key: string, vals?: Record<string, unknown>) => (vals ? `${key}:${JSON.stringify(vals)}` : key),
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  apiFetch: (...args: unknown[]) => mocks.apiFetch(...args),
}));
vi.mock("@/lib/api", () => ({
  fetchNotificationCount: () => mocks.fetchNotificationCount(),
  fetchBrowserCandidates: (since: string | null) => mocks.fetchBrowserCandidates(since),
  markNotificationRead: (id: string) => mocks.markNotificationRead(id),
}));
vi.mock("@/lib/auth-api", () => ({ setLastTeam: (id: string) => mocks.setLastTeam(id) }));

import { AttentionLoop } from "./attention-loop";
import { subscribeNotificationCount } from "@/lib/attention-loop";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
const turns = (cursor: string) => json({ items: [], cursor });
const count = (latest: string | null, action = 1) => ({ actionRequired: action, updatesUnread: 0, totalBadge: action, latestBrowserCandidateAt: latest });

function urgent(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    teamId: "team_a",
    category: "approval",
    type: "approval.required",
    priority: "p1",
    title: "stored",
    body: null,
    subjectType: "approval",
    subjectId: id,
    actorUserId: null,
    actionUrl: `/admin/approvals/?approvalId=${id}`,
    actions: [],
    metadata: { toolName: "crm_update", riskLevel: "high" },
    state: "open",
    actionRequired: true,
    recipientRole: "approver_admin",
    deliveredAt: "2026-11-08T09:00:00+00:00",
    readAt: null,
    archivedAt: null,
    createdAt: "2026-11-08T09:00:00+00:00",
    updatedAt: "2026-11-08T09:00:00+00:00",
    scope: "team",
    teamName: null,
    currentTeam: true,
    ...overrides,
  };
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

class FakeChannel {
  static instances: FakeChannel[] = [];
  listeners: Array<(event: MessageEvent) => void> = [];
  posted: string[] = [];
  constructor(public name: string) {
    FakeChannel.instances.push(this);
  }
  addEventListener(_type: string, listener: (event: MessageEvent) => void) {
    this.listeners.push(listener);
  }
  postMessage(value: string) {
    this.posted.push(value);
  }
  receive(value: string) {
    for (const listener of this.listeners) listener({ data: value } as MessageEvent);
  }
  close() {}
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeChannel.instances = [];
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  mocks.apiFetch.mockImplementation(async () => turns("c0"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("AttentionLoop", () => {
  it("runs one timer for turn completions and the notification count, and publishes the count to the bell", async () => {
    mocks.fetchNotificationCount.mockResolvedValue(count(null));
    const seen: unknown[] = [];
    const unsubscribe = subscribeNotificationCount((result) => seen.push(result));
    render(<AttentionLoop />);
    await tick(0);
    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    expect(mocks.fetchNotificationCount).toHaveBeenCalledTimes(1);

    await tick(15_000);
    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
    expect(mocks.fetchNotificationCount).toHaveBeenCalledTimes(2);
    expect(seen).toEqual([{ ok: true, counts: count(null) }, { ok: true, counts: count(null) }]);
    unsubscribe();
  });

  it("never announces urgent items that arrived before the page loaded", async () => {
    mocks.fetchNotificationCount.mockResolvedValue(count("2026-11-08T08:00:00+00:00"));
    render(<AttentionLoop />);
    await tick(0);
    await tick(15_000);
    expect(mocks.fetchBrowserCandidates).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("announces a newer urgent item once, with Open switching to its Team first", async () => {
    mocks.fetchNotificationCount
      .mockResolvedValueOnce(count("2026-11-08T08:00:00+00:00"))
      .mockResolvedValue(count("2026-11-08T09:00:00+00:00", 2));
    mocks.fetchBrowserCandidates.mockResolvedValue([urgent("apr_2", { teamId: "team_b", teamName: "Team B", currentTeam: false })]);
    render(<AttentionLoop />);
    await tick(0);
    await tick(15_000);

    expect(mocks.fetchBrowserCandidates).toHaveBeenCalledWith("2026-11-08T08:00:00+00:00");
    const shown = mocks.toast.mock.calls.filter(([arg]) => (arg as { kind: string }).kind === "warning");
    expect(shown).toHaveLength(1);
    expect(FakeChannel.instances.find((c) => c.name === "bowhead-notification-alerts")?.posted).toEqual(["apr_2"]);

    await act(async () => {
      await (shown[0][0] as { action: { onAction: () => Promise<void> } }).action.onAction();
    });
    expect(mocks.markNotificationRead).toHaveBeenCalledWith("apr_2");
    expect(mocks.setLastTeam).toHaveBeenCalledWith("team_b");
    expect(mocks.push).toHaveBeenCalledWith("/admin/approvals/?approvalId=apr_2");

    // The count stays the same on the next tick: nothing is fetched or announced again.
    await tick(15_000);
    expect(mocks.fetchBrowserCandidates).toHaveBeenCalledTimes(1);
  });

  it("does not announce an item another tab already announced", async () => {
    mocks.fetchNotificationCount
      .mockResolvedValueOnce(count(null))
      .mockResolvedValue(count("2026-11-08T09:00:00+00:00"));
    mocks.fetchBrowserCandidates.mockResolvedValue([urgent("apr_3")]);
    render(<AttentionLoop />);
    await tick(0);
    FakeChannel.instances.find((c) => c.name === "bowhead-notification-alerts")?.receive("apr_3");
    await tick(15_000);

    expect(mocks.fetchBrowserCandidates).toHaveBeenCalledTimes(1);
    expect(mocks.toast).not.toHaveBeenCalled();
  });

  it("shows a browser notification only while the tab is hidden and permission was granted", async () => {
    const created: string[] = [];
    class FakeNotification {
      static permission = "granted";
      onclick: (() => void) | null = null;
      constructor(title: string) {
        created.push(title);
      }
      close() {}
    }
    vi.stubGlobal("Notification", FakeNotification);
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    mocks.fetchNotificationCount
      .mockResolvedValueOnce(count(null))
      .mockResolvedValueOnce(count("2026-11-08T09:00:00+00:00"))
      .mockResolvedValue(count("2026-11-08T10:00:00+00:00"));
    mocks.fetchBrowserCandidates.mockResolvedValueOnce([urgent("apr_visible")]).mockResolvedValueOnce([urgent("apr_hidden")]);
    try {
      render(<AttentionLoop />);
      await tick(0);
      await tick(15_000);
      expect(created).toEqual([]);
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      await tick(60_000);
      expect(created).toEqual(['copy.approval_required_title']);
    } finally {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    }
  });

  it("offers browser notifications once, after the first announcement of either kind", async () => {
    class FakeNotification {
      static permission = "default";
      static requestPermission = vi.fn(async () => "granted");
    }
    vi.stubGlobal("Notification", FakeNotification);
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    const stored = new Map<string, string>();
    const originalStorage = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value) },
    });
    mocks.apiFetch
      .mockResolvedValueOnce(turns("c0"))
      .mockResolvedValueOnce(json({ items: [{ turn_id: "t1", session_id: "s1", title: "Q3", outcome: "completed", ended_at: "x" }], cursor: "c1" }))
      .mockResolvedValue(turns("c1"));
    mocks.fetchNotificationCount
      .mockResolvedValueOnce(count(null))
      .mockResolvedValue(count("2026-11-08T09:00:00+00:00"));
    mocks.fetchBrowserCandidates.mockResolvedValue([urgent("apr_4")]);
    try {
      render(<AttentionLoop />);
      await tick(0);
      await tick(15_000);
      const offers = mocks.toast.mock.calls.filter(([arg]) => (arg as { text: string }).text === "offer");
      expect(offers).toHaveLength(1);
    } finally {
      Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
      if (originalStorage) Object.defineProperty(window, "localStorage", originalStorage);
    }
  });

  it("stops asking for the count when the notification center is switched off", async () => {
    const { HttpStatusError } = await import("@/lib/api-client");
    mocks.fetchNotificationCount.mockRejectedValue(new HttpStatusError("notification count", new Response(null, { status: 404 })));
    render(<AttentionLoop />);
    await tick(0);
    await tick(15_000);
    expect(mocks.fetchNotificationCount).toHaveBeenCalledTimes(1);
    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
  });
});
