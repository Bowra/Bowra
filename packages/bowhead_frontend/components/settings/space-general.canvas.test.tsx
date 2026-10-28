import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { chooseSegment, segmentDisabled, segmentedValue } from "@/__tests__/segmented-field";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/en-US.json";
import { SettingsPageCanvas } from "@/components/canvas/settings-page-canvas";
import { WorkspaceSettingsCanvasProvider, useWorkspaceSettingsCanvasBridge } from "@/components/canvas/workspace-settings-canvas-context";
import type { WorkspaceSettingsCanvasBridge } from "@/lib/workspace-settings-canvas-bridge";
import { applyLiteProposal, resetLiteForTests } from "@/lib/config-copilot-lite";
import { ApiError, apiJson } from "@/lib/api-client";
import { getWorkspaceMetadata, getWorkspaceSettings, updateWorkspaceMetadata } from "@/lib/api";
import { notifyWorkspaceMetadataSaved } from "@/lib/workspace-settings-notifications";
import type { ConfigLiteProposePayload } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  getWorkspaceMetadata: vi.fn(),
  updateWorkspaceMetadata: vi.fn(),
  getWorkspaceSettings: vi.fn(),
  setWorkspaceSetting: vi.fn(),
}));
vi.mock("@/lib/team-storage", () => ({ getActiveTeamId: () => "team_original" }));
vi.mock("@/lib/use-space", () => ({ useSpace: () => ({ activeId: "ws_original", items: [] }), invalidateSpaces: vi.fn() }));
vi.mock("@/lib/api-client", async (original) => ({ ...(await original<typeof import("@/lib/api-client")>()), apiJson: vi.fn() }));
const target = { page: "space_general" as const, userId: "u_owner", teamId: "team_original", workspaceId: "ws_original" };
const original = { name: "Original Space", description: "Original description", revision: 1 };
const labels = messages.settings.space_general;
const close = vi.fn();
let bridge: WorkspaceSettingsCanvasBridge;
let closer: (() => void) | null = null;
function Harness({ visible = true }: { visible?: boolean }) {
  bridge = useWorkspaceSettingsCanvasBridge()!;
  // The host's tab × (D1) calls the page's registered close request.
  return visible ? <>
    <button onClick={() => closer?.()}>Close tab</button>
    <SettingsPageCanvas target={target} readOnly={false} onClose={close} onRegisterClose={(value) => { closer = value; }} />
  </> : null;
}
const suggestion: ConfigLiteProposePayload = {
  proposal_id: "ccd_metadata",
  turn_id: "t_metadata",
  form_id: "workspace_metadata",
  team_id: target.teamId,
  workspace_id: target.workspaceId,
  workspace_name: original.name,
  rows: [{ field: "name", from: original.name, to: "Finance", from_unset: false, baseline_revision: 1 }],
  notes: {},
};
function mount() {
  return renderIntl(
    <WorkspaceSettingsCanvasProvider>
      <Harness />
    </WorkspaceSettingsCanvasProvider>,
    { locale: "en-US" },
  );
}
async function ready() {
  return screen.findByRole("textbox", { name: labels.name_label });
}
beforeEach(() => {
  vi.resetAllMocks();
  resetLiteForTests();
  vi.mocked(getWorkspaceMetadata).mockResolvedValue(original);
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  vi.mocked(updateWorkspaceMetadata).mockResolvedValue({ ...original, name: "Manual name", revision: 2 });
  vi.mocked(apiJson).mockImplementation(async (url) =>
    String(url).endsWith("settings-review")
      ? { ...suggestion, status: "proposed", owner_user_id: target.userId, settings_target: null }
      : { status: "proposed" },
  );
});
afterEach(() => {
  cleanup();
  resetLiteForTests();
});

it("hosts the original composite page and keeps metadata behind its explicit Save", async () => {
  mount();
  const name = await ready();
  await screen.findByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  expect(screen.getAllByRole("radiogroup")).toHaveLength(2);
  expect(screen.queryAllByRole("switch")).toHaveLength(0);
  fireEvent.change(name, { target: { value: "Manual name" } });
  expect(updateWorkspaceMetadata).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  await waitFor(() =>
    expect(updateWorkspaceMetadata).toHaveBeenCalledWith(target.workspaceId, {
      name: "Manual name",
      expected_revision: 1,
    }),
  );
  expect((name as HTMLInputElement).value).toBe("Manual name");
});

it.each([
  { input: "Updated description", expected: "Updated description" },
  { input: " ", expected: null },
])("saves only an edited description without resubmitting the saved name: $expected", async ({ input, expected }) => {
  const legacy = { ...original, name: "Original\uFF08Space\uFF09" };
  vi.mocked(getWorkspaceMetadata).mockResolvedValue(legacy);
  vi.mocked(updateWorkspaceMetadata).mockResolvedValue({ ...legacy, description: expected, revision: 2 });
  mount();
  const name = await ready();
  fireEvent.change(screen.getByRole("textbox", { name: labels.description_label }), { target: { value: input } });
  expect(updateWorkspaceMetadata).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  await waitFor(() => expect(updateWorkspaceMetadata).toHaveBeenCalledWith(target.workspaceId, {
    description: expected,
    expected_revision: 1,
  }));
  expect((name as HTMLInputElement).value).toBe(legacy.name);
});

it("stages with zero writes and blocks confirmation after newer manual input", async () => {
  mount();
  const name = await ready();
  act(() => {
    bridge.acceptToolOpen(suggestion.turn_id, target);
    applyLiteProposal(suggestion);
  });
  await screen.findByText("Finance");
  expect(updateWorkspaceMetadata).not.toHaveBeenCalled();
  expect(vi.mocked(apiJson).mock.calls.every(([, options]) => !options?.method)).toBe(true);
  fireEvent.change(name, { target: { value: "Newer candidate" } });
  await expect(bridge.perform(suggestion, "apply")).rejects.toThrow("local_changes");
  expect(screen.queryByRole("button", { name: messages.settings.config_copilot_lite.apply })).toBeNull();
  await waitFor(() => expect(screen.getByText(messages.settings_canvas.local_changes)).not.toBeNull());
  expect((name as HTMLInputElement).value).toBe("Newer candidate");
  expect(vi.mocked(apiJson).mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
});

it("canonical readback preserves newer input and its original write revision", async () => {
  mount();
  const name = await ready();
  fireEvent.change(name, { target: { value: "Newer candidate" } });
  vi.mocked(getWorkspaceMetadata).mockResolvedValue({ ...original, name: "Finance", revision: 2 });
  act(() => notifyWorkspaceMetadataSaved({ teamId: target.teamId, workspaceId: target.workspaceId }));
  await waitFor(() => expect(screen.getByText(labels.save_failed)).not.toBeNull());
  expect((name as HTMLInputElement).value).toBe("Newer candidate");
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  await waitFor(() => expect(updateWorkspaceMetadata).toHaveBeenCalledWith(target.workspaceId, expect.objectContaining({ expected_revision: 1 })));
});

it("confirms through the original receipt and reads back metadata without changing preference controls or focus", async () => {
  mount();
  const name = await ready();
  const week = await screen.findByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  act(() => { bridge.acceptToolOpen(suggestion.turn_id, target); applyLiteProposal(suggestion); });
  await screen.findByText("Finance");
  const receipt = { proposal_id: suggestion.proposal_id, form_id: suggestion.form_id, status: "applied", team_id: target.teamId, workspace_id: target.workspaceId, owner_user_id: target.userId };
  vi.mocked(apiJson).mockResolvedValue(receipt);
  vi.mocked(getWorkspaceMetadata).mockResolvedValue({ ...original, name: "Finance", revision: 2 });
  await waitFor(() => expect(segmentDisabled(week)).toBe(false));
  const weekOption = week.querySelector<HTMLElement>('[role="radio"]')!;
  weekOption.focus();
  expect(document.activeElement).toBe(weekOption);
  await act(async () => { expect(await bridge.perform(suggestion, "apply")).toEqual(receipt); });
  await waitFor(() => expect((name as HTMLInputElement).value).toBe("Finance"));
  expect(document.activeElement).toBe(weekOption);
  expect(segmentedValue(week)).toBe("monday");
  expect(updateWorkspaceMetadata).not.toHaveBeenCalled();
  expect(vi.mocked(apiJson).mock.calls.filter(([, options]) => options?.method === "POST")).toEqual([[`/api/v1/config-proposals/${suggestion.proposal_id}/apply`, { method: "POST" }]]);
});

it("blocks disposal during the original pending save and clears inaccessible controls", async () => {
  let finish!: (value: typeof original) => void;
  vi.mocked(updateWorkspaceMetadata).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  mount();
  fireEvent.change(await ready(), { target: { value: "Manual name" } });
  fireEvent.click(screen.getByRole("button", { name: messages.common.save }));
  fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
  // The tab × waits for the pending save and closes once it lands (Canvas Objects Spec C14).
  await screen.findByText(messages.settings_canvas.save_before_close);
  expect(close).not.toHaveBeenCalled();
  await act(async () => finish({ ...original, name: "Manual name", revision: 2 }));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  vi.mocked(getWorkspaceMetadata).mockRejectedValue(new ApiError(403, "Denied"));
  act(() => notifyWorkspaceMetadataSaved({ teamId: target.teamId, workspaceId: target.workspaceId }));
  await screen.findByText(messages.settings_canvas.unavailable);
  expect(screen.queryByRole("textbox", { name: labels.name_label })).toBeNull();
});

it("an in-flight review cannot transfer to a reopened same-key original page", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(apiJson).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const rendered = mount();
  await ready();
  act(() => bridge.acceptToolOpen(suggestion.turn_id, target));
  const old = bridge.stage(suggestion);
  const rejected = expect(old).rejects.toThrow("page_not_ready");
  rendered.rerender(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <WorkspaceSettingsCanvasProvider>
        <Harness visible={false} />
      </WorkspaceSettingsCanvasProvider>
    </NextIntlClientProvider>,
  );
  rendered.rerender(
    <NextIntlClientProvider locale="en-US" messages={messages}>
      <WorkspaceSettingsCanvasProvider>
        <Harness />
      </WorkspaceSettingsCanvasProvider>
    </NextIntlClientProvider>,
  );
  await ready();
  await act(async () => finish({ ...suggestion, status: "proposed", owner_user_id: target.userId, settings_target: null }));
  await rejected;
  expect(bridge.hasOriginal(suggestion)).toBe(false);
});

it("a late confirmation receipt cannot read back into a reopened same-key page", async () => {
  const rendered = mount();
  await ready();
  act(() => bridge.acceptToolOpen(suggestion.turn_id, target));
  await bridge.stage(suggestion);
  let finish!: (value: unknown) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  let pending!: ReturnType<typeof bridge.perform>;
  act(() => { pending = bridge.perform(suggestion, "apply"); });
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  const tree = (visible: boolean) => <NextIntlClientProvider locale="en-US" messages={messages}><WorkspaceSettingsCanvasProvider><Harness visible={visible} /></WorkspaceSettingsCanvasProvider></NextIntlClientProvider>;
  rendered.rerender(tree(false));
  rendered.rerender(tree(true));
  const name = await ready();
  vi.mocked(getWorkspaceMetadata).mockClear();
  vi.mocked(getWorkspaceMetadata).mockResolvedValue({ ...original, name: "Finance", revision: 2 });
  await act(async () => {
    finish({ proposal_id: suggestion.proposal_id, form_id: suggestion.form_id, status: "applied", team_id: target.teamId, workspace_id: target.workspaceId, owner_user_id: target.userId });
    await pending;
  });
  expect(getWorkspaceMetadata).not.toHaveBeenCalled();
  expect((name as HTMLInputElement).value).toBe(original.name);
  expect(bridge.hasOriginal(suggestion)).toBe(false);
});
