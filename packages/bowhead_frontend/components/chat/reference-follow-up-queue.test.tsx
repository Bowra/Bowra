import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";

const api = vi.hoisted(() => ({
  cancel: vi.fn(async () => undefined),
  fetch: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock("@/lib/api", () => ({
  cancelQueuedReferenceSubmission: api.cancel,
  fetchQueuedReferenceSubmissions: api.fetch,
}));

import { ReferenceFollowUpQueue } from "./reference-follow-up-queue";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ReferenceFollowUpQueue", () => {
  it.each([
    { workspaceId: "ws_1", sessionId: null, actorScopeKey: "user-1:team-1:generation-1" },
    { workspaceId: null, sessionId: "s_1", actorScopeKey: "user-1:team-1:generation-1" },
    { workspaceId: "ws_1", sessionId: "s_1", actorScopeKey: null },
  ])("does not show a load failure before the session scope is ready: %j", (scope) => {
    const view = render(
      <ReferenceFollowUpQueue {...scope} refreshKey="home" canCancel />,
    );
    expect(view.container.textContent).toBe("");
    expect(api.fetch).not.toHaveBeenCalled();
  });

  it("shows a real load failure and reloads on retry", async () => {
    api.fetch.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce([]);
    render(
      <ReferenceFollowUpQueue
        workspaceId="ws_1"
        sessionId="s_1"
        actorScopeKey="user-1:team-1:generation-1"
        refreshKey="home"
        canCancel
      />,
    );
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "queue_retry" }));
    await waitFor(() => expect(api.fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("shows accepted resources and cancels only the chosen follow-up", async () => {
    api.fetch.mockResolvedValueOnce([
      {
        client_submission_id: "submission-1",
        display_text: "@分析师 /杜邦分析 盈利能力",
        resources: [
          { kind: "agent", label: "分析师" },
          { kind: "skill", label: "杜邦分析" },
        ],
        attachment_count: 1,
        accepted_at: "2026-09-17T01:02:03+00:00",
      },
    ]).mockResolvedValueOnce([]);

    render(
      <ReferenceFollowUpQueue
        workspaceId="ws_1"
        sessionId="s_1"
        actorScopeKey="user-1:team-1:generation-1"
        refreshKey="turn-1:1:running:0"
        canCancel
      />,
    );

    expect(await screen.findByText("@分析师 /杜邦分析 盈利能力"))
      .toBeTruthy();
    expect(screen.getByText("@分析师")).toBeTruthy();
    expect(screen.getByText("/杜邦分析")).toBeTruthy();
    expect(screen.getByText("queue_attachments")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "queue_cancel" }));
    await waitFor(() => expect(api.cancel).toHaveBeenCalledWith(
      "ws_1",
      "s_1",
      "submission-1",
    ));
    await waitFor(() => expect(
      screen.queryByText("@分析师 /杜邦分析 盈利能力"),
    ).toBeNull());
  });

  it("keeps a failed cancellation visible for an explicit retry", async () => {
    api.fetch.mockResolvedValueOnce([{
      client_submission_id: "submission-1",
      display_text: "/杜邦分析 盈利能力",
      resources: [{ kind: "skill", label: "杜邦分析" }],
      attachment_count: 0,
      accepted_at: "2026-09-17T01:02:03+00:00",
    }]);
    api.cancel.mockRejectedValueOnce(new Error("offline"));

    render(
      <ReferenceFollowUpQueue
        workspaceId="ws_1"
        sessionId="s_1"
        actorScopeKey="user-1:team-1:generation-1"
        refreshKey="turn-1:1:running:0"
        canCancel
      />,
    );
    fireEvent.click(await screen.findByRole("button", {
      name: "queue_cancel",
    }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "queue_cancel_failed",
    );
    expect(screen.getByText("/杜邦分析 盈利能力")).toBeTruthy();
  });

  it("keeps the queue readable without exposing a cancel action in read-only mode", async () => {
    api.fetch.mockResolvedValueOnce([{
      client_submission_id: "submission-1",
      display_text: "/杜邦分析 盈利能力",
      resources: [{ kind: "skill", label: "杜邦分析" }],
      attachment_count: 0,
      accepted_at: "2026-09-17T01:02:03+00:00",
    }]);

    render(
      <ReferenceFollowUpQueue
        workspaceId="ws_1"
        sessionId="s_1"
        actorScopeKey="user-1:team-1:generation-1"
        refreshKey="turn-1:1:running:0"
        canCancel={false}
      />,
    );

    expect(await screen.findByText("/杜邦分析 盈利能力")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "queue_cancel" })).toBeNull();
    expect(screen.getByRole("button", { name: "queue_source" })).toBeTruthy();
  });

  it("never renders a late response from another login scope", async () => {
    let resolveFirst: ((items: unknown[]) => void) | undefined;
    let resolveSecond: ((items: unknown[]) => void) | undefined;
    api.fetch
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFirst = resolve;
      }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveSecond = resolve;
      }));
    const view = render(
      <ReferenceFollowUpQueue
        workspaceId="ws_1"
        sessionId="s_1"
        actorScopeKey="user-1:team-1:generation-1"
        refreshKey="turn-1:1:running:0"
        canCancel
      />,
    );
    view.rerender(
      <ReferenceFollowUpQueue
        workspaceId="ws_1"
        sessionId="s_1"
        actorScopeKey="user-2:team-1:generation-2"
        refreshKey="turn-1:1:running:0"
        canCancel
      />,
    );
    await act(async () => {
      resolveSecond?.([{
        client_submission_id: "submission-2",
        display_text: "second login task",
        resources: [{ kind: "skill", label: "second" }],
        attachment_count: 0,
        accepted_at: "2026-09-17T01:02:04+00:00",
      }]);
    });
    expect(await screen.findByText("second login task")).toBeTruthy();

    await act(async () => {
      resolveFirst?.([{
        client_submission_id: "submission-1",
        display_text: "first login private task",
        resources: [{ kind: "skill", label: "first" }],
        attachment_count: 0,
        accepted_at: "2026-09-17T01:02:03+00:00",
      }]);
    });
    expect(screen.getByText("second login task")).toBeTruthy();
    expect(screen.queryByText("first login private task")).toBeNull();
  });

  it.each([404, 409])(
    "removes a task and reloads when cancellation returns terminal %s",
    async (status) => {
      api.fetch
        .mockResolvedValueOnce([{
          client_submission_id: "submission-1",
          display_text: "/杜邦分析 盈利能力",
          resources: [{ kind: "skill", label: "杜邦分析" }],
          attachment_count: 0,
          accepted_at: "2026-09-17T01:02:03+00:00",
        }])
        .mockResolvedValueOnce([]);
      api.cancel.mockRejectedValueOnce(new ApiError(status, "settled"));

      render(
        <ReferenceFollowUpQueue
          workspaceId="ws_1"
          sessionId="s_1"
          actorScopeKey="user-1:team-1:generation-1"
          refreshKey="turn-1:1:running:0"
          canCancel
        />,
      );
      fireEvent.click(await screen.findByRole("button", {
        name: "queue_cancel",
      }));

      await waitFor(() => expect(
        screen.queryByText("/杜邦分析 盈利能力"),
      ).toBeNull());
      await waitFor(() => expect(api.fetch).toHaveBeenCalledTimes(2));
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );
});
