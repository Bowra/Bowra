import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastMock = vi.fn();
vi.mock("@/lib/use-toast", () => ({ toast: (...args: unknown[]) => toastMock(...args) }));
const pushMock = vi.fn();
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: pushMock }) }));
vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) => (vals ? `${key}:${JSON.stringify(vals)}` : key),
}));
const apiFetchMock = vi.fn();
vi.mock("@/lib/api-client", () => ({ apiFetch: (...args: unknown[]) => apiFetchMock(...args) }));

import { TurnCompletionNotifier } from "./turn-completion-notifier";

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
const item = (session_id: string, outcome = "completed") => ({
  turn_id: `t_${session_id}`, session_id, title: `Q3 ${session_id}`, outcome, ended_at: "2026-10-21T09:00:00+00:00",
});

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("TurnCompletionNotifier", () => {
  it("announces turns that end after it starts, with an Open action, but not the session being viewed", async () => {
    window.history.replaceState(null, "", "/?session=s_here");
    apiFetchMock
      .mockResolvedValueOnce(json({ items: [], cursor: "c0" }))
      .mockResolvedValueOnce(json({ items: [item("s_away", "partial"), item("s_here")], cursor: "c1" }));
    render(<TurnCompletionNotifier />);
    await tick(0);
    expect(toastMock).not.toHaveBeenCalled();
    await tick(15_000);

    expect(apiFetchMock.mock.calls[1][0]).toContain("after=c0");
    expect(toastMock).toHaveBeenCalledTimes(1);
    const shown = toastMock.mock.calls[0][0];
    expect(shown.kind).toBe("warning");
    expect(shown.text).toBe('partial:{"title":"Q3 s_away"}');
    shown.action.onAction();
    expect(pushMock).toHaveBeenCalledWith("/?session=s_away");
  });

  it("never announces turns that ended before the page loaded", async () => {
    apiFetchMock.mockResolvedValueOnce(json({ items: [item("s_old")], cursor: "c0" }));
    render(<TurnCompletionNotifier />);
    await tick(0);
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("also shows a browser notification when the tab is hidden, the context is secure and permission was granted", async () => {
    const created: Array<{ title: string; body?: string }> = [];
    class FakeNotification {
      static permission = "granted";
      onclick: (() => void) | null = null;
      constructor(title: string, options?: { body?: string }) {
        created.push({ title, body: options?.body });
      }
      close() {}
    }
    vi.stubGlobal("Notification", FakeNotification);
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    apiFetchMock
      .mockResolvedValueOnce(json({ items: [], cursor: "c0" }))
      .mockResolvedValueOnce(json({ items: [item("s_away")], cursor: "c1" }));
    try {
      render(<TurnCompletionNotifier />);
      await tick(0);
      await tick(60_000);
      expect(created).toEqual([{ title: 'completed:{"title":"Q3 s_away"}', body: "notification_body" }]);
    } finally {
      vi.unstubAllGlobals();
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    }
  });
});
