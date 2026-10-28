import { useRef, useState } from "react";
import { chooseSegment, segmentDisabled, segmentedValue } from "@/__tests__/segmented-field";
import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import messages from "@/messages/en-US.json";

vi.mock("@/lib/api", () => ({
  getWorkspaceMetadata: vi.fn(),
  getWorkspaceSettings: vi.fn(),
  setWorkspaceSetting: vi.fn(),
}));
import {
  getWorkspaceMetadata,
  getWorkspaceSettings,
  setWorkspaceSetting,
} from "@/lib/api";
import { SettingsPageCanvas } from "./settings-page-canvas";
import { ConfigurationCanvasHost } from "./configuration-canvas-host";
import { WorkspaceSettingsCanvasProvider, useWorkspaceSettingsCanvasBridge } from "./workspace-settings-canvas-context";
import { WorkspaceSettingsCanvasBridge } from "@/lib/workspace-settings-canvas-bridge";
import { apiJson } from "@/lib/api-client";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";
import type { ConfigLiteProposePayload } from "@/lib/types";

/** The host's tab × (D1): the page registers its close request instead of rendering a “Close page” button. */
function ClosableSettings(props: Omit<React.ComponentProps<typeof SettingsPageCanvas>, "onRegisterClose">) {
  const closer = useRef<(() => void) | null>(null);
  return <>
    <button type="button" onClick={() => closer.current?.()}>Close tab</button>
    <SettingsPageCanvas {...props} onRegisterClose={(close) => { closer.current = close; }} />
  </>;
}

vi.mock("@/lib/api-client", async (original) => ({
  ...(await original<typeof import("@/lib/api-client")>()),
  apiJson: vi.fn(),
}));

const target = {
  page: "workspace_i18n" as const,
  userId: "u_owner",
  teamId: "team_original",
  workspaceId: "ws_original",
};
const labels = messages.settings.workspace_i18n;
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getWorkspaceMetadata).mockResolvedValue({
    name: "Original Space",
    description: "",
    revision: 1,
  });
  vi.mocked(getWorkspaceSettings).mockResolvedValue({
    week_start_day: "monday",
    default_data_language: "en-US",
  });
  vi.mocked(setWorkspaceSetting).mockResolvedValue(undefined);
});
afterEach(cleanup);

it("hosts all original manual controls and saves through the existing exact-Space API", async () => {
  renderIntl(
    <SettingsPageCanvas target={target} readOnly={false} onClose={vi.fn()} />,
    { locale: "en-US" },
  );
  const week = await screen.findByRole("radiogroup", {
    name: labels.hosted_week_label,
  });
  expect(screen.getAllByRole("radiogroup")).toHaveLength(2);
  // The rollout switch stays on the full settings page; Canvas shows the user-facing preferences only.
  expect(screen.queryAllByRole("switch")).toHaveLength(0);
  chooseSegment(week, "sunday");
  await waitFor(() =>
    expect(setWorkspaceSetting).toHaveBeenCalledWith(
      target.workspaceId,
      "week_start_day",
      "sunday",
    ),
  );
  vi.mocked(getWorkspaceSettings).mockResolvedValue({
    week_start_day: "sunday",
  });
  fireEvent.click(
    screen.getByRole("button", { name: messages.settings_canvas.refresh }),
  );
  await waitFor(() =>
    expect(
      segmentedValue(
        screen.getByRole("radiogroup", {
          name: labels.hosted_week_label,
        }),
      ),
    ).toBe("sunday"),
  );
  expect(setWorkspaceSetting).toHaveBeenCalledTimes(1);
  expect(getWorkspaceSettings).toHaveBeenLastCalledWith(target.workspaceId);
});

it("denies the original page when its authoritative metadata admission fails", async () => {
  vi.mocked(getWorkspaceMetadata).mockRejectedValue(new Error("Forbidden"));
  renderIntl(
    <SettingsPageCanvas target={target} readOnly={false} onClose={vi.fn()} />,
    { locale: "en-US" },
  );
  await screen.findByText(messages.settings_canvas.unavailable);
  expect(getWorkspaceSettings).not.toHaveBeenCalled();
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("disables original manual controls for a read-only caller and closes only the view", async () => {
  const close = vi.fn();
  renderIntl(<ClosableSettings target={target} readOnly onClose={close} />, {
    locale: "en-US",
  });
  const week = await screen.findByRole("radiogroup", {
    name: labels.hosted_week_label,
  });
  expect(segmentDisabled(week)).toBe(true);
  fireEvent.click(
    screen.getByRole("button", { name: "Close tab" }),
  );
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("retains an Agent's input and routes a late settings save to the hidden original page", async () => {
  let finishSave!: () => void;
  vi.mocked(setWorkspaceSetting).mockImplementation(
    () =>
      new Promise((resolve) => {
        finishSave = resolve;
      }),
  );
  function Host() {
    const [selected, select] = useState("settings");
    const [input, setInput] = useState("");
    return (
      <ConfigurationCanvasHost
        selectedKey={selected}
        onSelect={select}
        objects={[
          {
            key: "agent",
            kind: "agent",
            title: "Sales Agent",
            content: (
              <input
                aria-label="Agent purpose"
                value={input}
                onChange={(event) => setInput(event.target.value)}
              />
            ),
          },
          {
            key: "settings",
            kind: "settings",
            title: "Space preferences",
            content: (
              <SettingsPageCanvas
                target={target}
                readOnly={false}
                onClose={vi.fn()}
              />
            ),
          },
        ]}
      />
    );
  }
  renderIntl(<Host />, { locale: "en-US" });
  const week = await screen.findByRole("radiogroup", {
    name: labels.hosted_week_label,
  });
  chooseSegment(week, "sunday");
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("tab", { name: "Sales Agent" }));
  const input = screen.getByRole("textbox", { name: "Agent purpose" });
  fireEvent.change(input, { target: { value: "Keep my unfinished purpose" } });
  await act(async () => finishSave());
  expect(
    screen
      .getByRole("tab", { name: "Sales Agent" })
      .getAttribute("aria-selected"),
  ).toBe("true");
  fireEvent.click(screen.getByRole("tab", { name: "Space preferences" }));
  expect(screen.getByRole("radiogroup", { name: labels.hosted_week_label })).toBe(
    week,
  );
  expect(segmentedValue(week)).toBe("sunday");
  fireEvent.click(screen.getByRole("tab", { name: "Sales Agent" }));
  expect(screen.getByRole("textbox", { name: "Agent purpose" })).toBe(input);
  expect((input as HTMLInputElement).value).toBe("Keep my unfinished purpose");
  expect(getWorkspaceSettings).toHaveBeenCalledOnce();
  expect(setWorkspaceSetting).toHaveBeenCalledWith(
    "ws_original",
    "week_start_day",
    "sunday",
  );
});

it("keeps the original page mounted while a manual save is pending", async () => {
  let finish!: () => void;
  vi.mocked(setWorkspaceSetting).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const close = vi.fn();
  renderIntl(<ClosableSettings target={target} readOnly={false} onClose={close} />, { locale: "en-US" });
  await waitFor(() => expect(segmentDisabled(screen.getByRole("radiogroup", { name: labels.hosted_week_label }))).toBe(false));
  const week = screen.getByRole("radiogroup", { name: labels.hosted_week_label });
  const reads = vi.mocked(getWorkspaceSettings).mock.calls.length;
  chooseSegment(week, "sunday");
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: messages.settings_canvas.refresh }));
  fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
  await screen.findByText(messages.settings_canvas.save_before_close, { exact: false });
  expect(getWorkspaceSettings).toHaveBeenCalledTimes(reads);
  expect(close).not.toHaveBeenCalled();
  expect(screen.getByRole("radiogroup", { name: labels.hosted_week_label })).toBe(week);
  // The object closes by itself once the pending write settles; no second click and no dialog.
  await act(async () => finish());
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
});

it("reveals the original busy settings object when aggregate navigation is blocked", async () => {
  let finish!: () => void;
  vi.mocked(setWorkspaceSetting).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const activate = vi.fn();
  renderIntl(<SettingsPageCanvas target={target} readOnly={false} onClose={vi.fn()} onActivate={activate} />, { locale: "en-US" });
  await waitFor(() => expect(segmentDisabled(screen.getByRole("radiogroup", { name: labels.hosted_week_label }))).toBe(false));
  chooseSegment(screen.getByRole("radiogroup", { name: labels.hosted_week_label }), "sunday");
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledOnce());
  await act(async () => { expect(await confirmCanvasNavigation()).toBe(false); });
  expect(activate).toHaveBeenCalledOnce();
  await act(async () => finish());
});

it("rejects an old discard decision after a newer manual edit", async () => {
  const { subscribeConfirm, settleConfirm } = await import("@/lib/confirm-dialog-store");
  let decisionId = 0;
  const unsubscribe = subscribeConfirm((value) => { if (value) decisionId = value.id; });
  try {
    vi.mocked(setWorkspaceSetting).mockRejectedValue(new Error("Save failed"));
    const close = vi.fn();
    renderIntl(<ClosableSettings target={target} readOnly={false} onClose={close} />, { locale: "en-US" });
    await waitFor(() => expect(segmentDisabled(screen.getByRole("radiogroup", { name: labels.hosted_week_label }))).toBe(false));
    const week = screen.getByRole("radiogroup", { name: labels.hosted_week_label });
    chooseSegment(week, "sunday");
    await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledOnce());
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
    await waitFor(() => expect(decisionId).toBeGreaterThan(0));
    chooseSegment(screen.getByRole("radiogroup", { name: labels.hosted_data_lang_label }), "zh-CN");
    await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledTimes(2));
    await act(async () => settleConfirm(decisionId, true));
    expect(close).not.toHaveBeenCalled();
    expect(screen.getByRole("radiogroup", { name: labels.hosted_week_label })).toBe(week);
  } finally { unsubscribe(); }
});

it("retains newer manual input while an Agent confirmation awaits its authorized review", async () => {
  const { subscribeConfirm, settleConfirm } = await import("@/lib/confirm-dialog-store");
  let decisionId = 0;
  let bridge!: WorkspaceSettingsCanvasBridge;
  let finishReview!: (value: unknown) => void;
  const unsubscribe = subscribeConfirm((value) => { if (value) decisionId = value.id; });
  const close = vi.fn();
  function CaptureBridge() {
    bridge = useWorkspaceSettingsCanvasBridge()!;
    return null;
  }
  try {
    vi.mocked(setWorkspaceSetting).mockRejectedValue(new Error("Save failed"));
    renderIntl(<WorkspaceSettingsCanvasProvider><CaptureBridge /><ClosableSettings target={target} readOnly={false} onClose={close} /></WorkspaceSettingsCanvasProvider>, { locale: "en-US" });
    await waitFor(() => expect(segmentDisabled(screen.getByRole("radiogroup", { name: labels.hosted_week_label }))).toBe(false));
    const binding = bridge.capture(target);
    bridge.acceptTurn("t_pending", binding);
    const proposal: ConfigLiteProposePayload = {
      proposal_id: "ccd_pending", turn_id: "t_pending", form_id: "workspace_i18n", team_id: target.teamId,
      workspace_id: target.workspaceId, workspace_name: "Original Space", notes: {},
      rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }],
      settings_target: JSON.parse(binding.clientInfo),
    };
    vi.mocked(apiJson).mockImplementation(() => new Promise((resolve) => { finishReview = resolve; }));
    let action!: Promise<unknown>;
    act(() => { action = bridge.perform(proposal, "apply").catch((error: unknown) => error); });
    expect(bridge.hasPendingAction(binding.controllerId)).toBe(true);
    const week = screen.getByRole("radiogroup", { name: labels.hosted_week_label });
    chooseSegment(week, "sunday");
    await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledOnce());
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
    await screen.findByText(messages.settings_canvas.save_before_close, { exact: false });
    expect(close).not.toHaveBeenCalled();
    expect(decisionId).toBe(0);
    expect(screen.getByRole("radiogroup", { name: labels.hosted_week_label })).toBe(week);
    await act(async () => {
      finishReview({ ...proposal, owner_user_id: target.userId, status: "proposed" });
      expect(await action).toMatchObject({ code: "local_changes" });
    });
    expect(apiJson).toHaveBeenCalledExactlyOnceWith("/api/v1/config-proposals/ccd_pending/settings-review");
    expect(bridge.hasPendingAction(binding.controllerId)).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
    await waitFor(() => expect(decisionId).toBeGreaterThan(0));
    await act(async () => settleConfirm(decisionId, true));
    expect(close).toHaveBeenCalledOnce();
  } finally { unsubscribe(); }
});

it("asks with refresh wording, not the close copy, before refreshing over unsaved input", async () => {
  const { subscribeConfirm } = await import("@/lib/confirm-dialog-store");
  let request: { title?: string; confirmLabel?: string } | null = null;
  const unsubscribe = subscribeConfirm((value) => { if (value) request = value; });
  try {
    vi.mocked(setWorkspaceSetting).mockRejectedValue(new Error("Save failed"));
    renderIntl(<ClosableSettings target={target} readOnly={false} onClose={vi.fn()} />, { locale: "en-US" });
    await waitFor(() => expect(segmentDisabled(screen.getByRole("radiogroup", { name: labels.hosted_week_label }))).toBe(false));
    chooseSegment(screen.getByRole("radiogroup", { name: labels.hosted_week_label }), "sunday");
    await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledOnce());
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: messages.settings_canvas.refresh }));
    await waitFor(() => expect(request).not.toBeNull());
    expect(request!.title).toBe(messages.canvas_object.refresh_guard_title);
    expect(request!.confirmLabel).toBe(messages.canvas_object.refresh_guard_discard);
  } finally { unsubscribe(); }
});
