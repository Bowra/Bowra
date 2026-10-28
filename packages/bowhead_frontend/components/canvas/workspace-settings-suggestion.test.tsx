import { useEffect, useRef, useState } from "react";
import { chooseSegment, segmentDisabled, segmentedValue } from "@/__tests__/segmented-field";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import messages from "@/messages/en-US.json";
import { apiJson, ApiError } from "@/lib/api-client";
import { getWorkspaceMetadata, getWorkspaceSettings, setWorkspaceSetting } from "@/lib/api";
import { applyLiteProposal, resetLiteForTests, _dropLiteStateCacheForTests } from "@/lib/config-copilot-lite";
import { LiteConfirmConnected } from "@/components/config-copilot/lite-confirm-connected";
import { SettingsPageCanvas } from "./settings-page-canvas";
import { WorkspaceSettingsCanvasProvider, useWorkspaceSettingsCanvasBridge } from "./workspace-settings-canvas-context";
import type { WorkspaceSettingsCanvasBridge } from "@/lib/workspace-settings-canvas-bridge";

vi.mock("@/lib/api", () => ({ getWorkspaceMetadata: vi.fn(), getWorkspaceSettings: vi.fn(), setWorkspaceSetting: vi.fn() }));
vi.mock("@/lib/api-client", async (original) => ({ ...await original<typeof import("@/lib/api-client")>(), apiJson: vi.fn() }));
const target = { page: "workspace_i18n" as const, userId: "u_1", teamId: "team_1", workspaceId: "ws_1" };
const proposal = { proposal_id: "ccd_1", turn_id: "t_1", form_id: "workspace_i18n", team_id: "team_1", workspace_id: "ws_1", workspace_name: "Original Space", notes: {}, rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }], settings_target: { form_id: "workspace_i18n" as const, user_id: "u_1", team_id: "team_1", workspace_id: "ws_1", edit_generation: 0 } };
let bridge: WorkspaceSettingsCanvasBridge;
function Host() {
  const value = useWorkspaceSettingsCanvasBridge();
  const [open, setOpen] = useState(true);
  const [readOnly, setReadOnly] = useState(false);
  // The host's tab × (D1) calls the page's registered close request.
  const closer = useRef<(() => void) | null>(null);
  useEffect(() => { bridge = value!; }, [value]);
  return <>
    <button onClick={() => setOpen(true)}>Reopen preferences</button>
    <button onClick={() => setReadOnly(true)}>Make page read-only</button>
    <button onClick={() => closer.current?.()}>Close tab</button>
    {open ? <SettingsPageCanvas target={target} readOnly={readOnly} onClose={() => setOpen(false)}
      onRegisterClose={(close) => { closer.current = close; }} /> : null}
    <LiteConfirmConnected proposalId={proposal.proposal_id} />
  </>;
}
async function open() {
  applyLiteProposal(proposal);
  renderIntl(<WorkspaceSettingsCanvasProvider><Host /></WorkspaceSettingsCanvasProvider>, { locale: "en-US" });
  const week = await screen.findByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  await waitFor(() => expect(() => bridge.capture(target)).not.toThrow());
  act(() => bridge.acceptTurn("t_1", bridge.capture(target)));
  await waitFor(() => expect(screen.getAllByTestId("lite-propose")).toHaveLength(2));
  return week;
}
beforeEach(() => {
  vi.resetAllMocks();
  resetLiteForTests();
  vi.mocked(getWorkspaceMetadata).mockResolvedValue({ name: "Original Space", description: "", revision: 1 });
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  vi.mocked(setWorkspaceSetting).mockResolvedValue(undefined);
  vi.mocked(apiJson).mockImplementation(async () => ({ ...proposal, owner_user_id: "u_1", status: "proposed" }));
});

it("reviews without saving, then applies from Chat and reads the original mounted page once", async () => {
  const week = await open();
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
  expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  vi.mocked(apiJson).mockImplementation(async (url) => ({ ...proposal, owner_user_id: "u_1", status: String(url).endsWith("/apply") ? "applied" : "proposed" }));
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "sunday" });
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.apply }).at(-1)!);
  await waitFor(() => expect(segmentedValue(week)).toBe("sunday"));
  expect(screen.getByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label })).toBe(week);
  expect(getWorkspaceSettings).toHaveBeenCalledTimes(2);
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("turns a failed apply into the Couldn't apply card whose Retry applies the same suggestion again", async () => {
  const week = await open();
  vi.mocked(apiJson).mockImplementation(async (url) => {
    if (String(url).endsWith("/apply")) throw new ApiError(503, "Unavailable");
    return { ...proposal, owner_user_id: "u_1", status: "proposed" };
  });
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.apply }).at(-1)!);
  await waitFor(() => expect(screen.getAllByTestId("lite-fail").length).toBeGreaterThan(0));
  expect(screen.getAllByText(messages.settings.config_copilot_lite.apply_fail_body).length).toBeGreaterThan(0);
  vi.mocked(apiJson).mockImplementation(async (url) => ({ ...proposal, owner_user_id: "u_1", status: String(url).endsWith("/apply") ? "applied" : "proposed" }));
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "sunday" });
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.retry }).at(-1)!);
  await waitFor(() => expect(segmentedValue(week)).toBe("sunday"));
  expect(vi.mocked(apiJson).mock.calls.filter(([url, init]) => init?.method === "POST" && String(url).endsWith("/ccd_1/apply"))).toHaveLength(2);
});

it("does not let Chat confirmation bypass manual changes in the original page", async () => {
  const week = await open();
  vi.mocked(setWorkspaceSetting).mockImplementation(() => new Promise(() => {}));
  chooseSegment(week, "sunday");
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledOnce());
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.apply }).at(-1)!);
  await screen.findByText(messages.settings_canvas.local_changes);
  expect(segmentedValue(week)).toBe("sunday");
  expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

it("keeps replayed old proposals detached after closing and reopening the same page", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
  await screen.findByText(messages.settings_canvas.review_unavailable);
  fireEvent.click(screen.getByRole("button", { name: "Reopen preferences" }));
  await screen.findByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  act(() => applyLiteProposal({ ...proposal }));
  await waitFor(() => expect(screen.queryAllByTestId("lite-propose")).toHaveLength(0));
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
  expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

it("clears the original page on an authoritative review denial without exposing its proposed values", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
  await waitFor(() => expect(screen.queryByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label })).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Reopen preferences" }));
  await waitFor(() => expect(() => bridge.capture(target)).not.toThrow());
  vi.mocked(apiJson).mockRejectedValue(new ApiError(403, "Forbidden"));
  act(() => {
    bridge.acceptTurn("t_2", bridge.capture(target));
    applyLiteProposal({ ...proposal, proposal_id: "ccd_2", turn_id: "t_2" });
  });
  await screen.findByText(messages.settings_canvas.unavailable);
  expect(screen.queryByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label })).toBeNull();
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});


it("preserves the refreshed original page when an old in-flight review loses access", async () => {
  let rejectReview!: (error: ApiError) => void;
  vi.mocked(apiJson).mockImplementation(async (url) => String(url).endsWith("/settings-review")
    ? new Promise((_, reject) => { rejectReview = reject; })
    : { ...proposal, owner_user_id: "u_1", status: "proposed" });
  applyLiteProposal(proposal);
  renderIntl(<WorkspaceSettingsCanvasProvider><Host /></WorkspaceSettingsCanvasProvider>, { locale: "en-US" });
  await waitFor(() => expect(() => bridge.capture(target)).not.toThrow());
  act(() => bridge.acceptTurn("t_1", bridge.capture(target)));
  await waitFor(() => expect(rejectReview).toBeDefined());
  const original = screen.getByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  fireEvent.click(screen.getByRole("button", { name: messages.settings_canvas.refresh }));
  await waitFor(() => expect(screen.getByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label })).not.toBe(original));
  await act(async () => { rejectReview(new ApiError(403, "Forbidden")); });
  expect(segmentedValue(screen.getByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label }))).toBe("monday");
  expect(screen.queryByText(messages.settings_canvas.unavailable)).toBeNull();
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});


it("removes old suggestion actions from both surfaces when the original page becomes read-only", async () => {
  const week = await open();
  fireEvent.click(screen.getByRole("button", { name: "Make page read-only" }));
  await waitFor(() => expect(screen.queryAllByRole("button", { name: messages.settings.config_copilot_lite.apply })).toHaveLength(0));
  expect(week.closest("fieldset")?.disabled).toBe(true);
  expect(() => bridge.capture(target)).toThrow("page_not_ready");
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
  expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});


it("explicitly reviews a persisted suggestion on the reopened page before confirmation", async () => {
  await open();
  fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
  await waitFor(() => expect(screen.queryByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label })).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Reopen preferences" }));
  const week = await screen.findByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  await waitFor(() => expect(screen.queryAllByTestId("lite-propose")).toHaveLength(0));
  const readCount = vi.mocked(apiJson).mock.calls.filter(([url]) => String(url).endsWith("/settings-review")).length;
  fireEvent.click(await screen.findByRole("button", { name: messages.settings_canvas.review_saved }));
  await waitFor(() => expect(screen.getAllByTestId("lite-propose")).toHaveLength(2));
  expect(vi.mocked(apiJson).mock.calls.filter(([url]) => String(url).endsWith("/settings-review"))).toHaveLength(readCount + 1);
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
  expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  vi.mocked(apiJson).mockImplementation(async (url) => ({ ...proposal, owner_user_id: "u_1", status: String(url).endsWith("/apply") ? "applied" : "proposed" }));
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "sunday" });
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.apply }).at(-1)!);
  await waitFor(() => expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1));
  await waitFor(() => expect(getWorkspaceSettings).toHaveBeenCalledTimes(3));
  await waitFor(() => expect(segmentedValue(week)).toBe("sunday"));
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it.each(["retained", "reset", "pending"])("recovers an applied preference after closing and reopening with %s status cache, then undoes through the original service", async (cache) => {
  let finishHydration: ((value: unknown) => void) | undefined;
  await open();
  vi.mocked(apiJson).mockImplementation(async (url) => ({ ...proposal, owner_user_id: "u_1", status: String(url).endsWith("/apply") ? "applied" : "proposed" }));
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "sunday" });
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.apply }).at(-1)!);
  await waitFor(() => expect(segmentedValue(screen.getByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label }))).toBe("sunday"));
  vi.mocked(apiJson).mockResolvedValue({ ...proposal, owner_user_id: "u_1", status: "applied" });
  fireEvent.click(screen.getByRole("button", { name: "Close tab" }));
  await waitFor(() => expect(screen.queryByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label })).toBeNull());
  if (cache !== "retained") _dropLiteStateCacheForTests();
  if (cache === "pending") vi.mocked(apiJson).mockImplementation(async (url) => String(url).endsWith("/settings-review")
    ? { ...proposal, owner_user_id: "u_1", status: "applied" } : new Promise((resolve) => { finishHydration = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "Reopen preferences" }));
  const week = await screen.findByRole("radiogroup", { name: messages.settings.workspace_i18n.hosted_week_label });
  // Before the reopened page reviews the saved suggestion, Undo is hidden rather than shown disabled (D11).
  if (cache !== "pending") await waitFor(() => expect(screen.getAllByTestId("lite-applied").length).toBeGreaterThan(0));
  expect(screen.queryAllByRole("button", { name: messages.settings.config_copilot_lite.undo })).toHaveLength(0);
  const writes = vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST").length;
  fireEvent.click(await screen.findByRole("button", { name: messages.settings_canvas.review_saved }));
  await waitFor(() => expect(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.undo }).every((button) => !(button as HTMLButtonElement).disabled)).toBe(true));
  expect(screen.queryAllByRole("button", { name: messages.settings.config_copilot_lite.apply })).toHaveLength(0);
  expect(vi.mocked(apiJson).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(writes);
  vi.mocked(apiJson).mockResolvedValue({ ...proposal, owner_user_id: "u_1", status: "reverted" });
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  fireEvent.click(screen.getAllByRole("button", { name: messages.settings.config_copilot_lite.undo }).at(-1)!);
  await waitFor(() => expect(segmentedValue(week)).toBe("monday"));
  if (finishHydration) await act(async () => { finishHydration!({ ...proposal, owner_user_id: "u_1", status: "applied" }); });
  expect(screen.queryAllByTestId("lite-applied")).toHaveLength(0);
  expect(screen.getAllByText(messages.settings.config_copilot_lite.reverted)).toHaveLength(2);
  expect(apiJson).toHaveBeenCalledWith("/api/v1/config-proposals/ccd_1/undo", expect.objectContaining({ method: "POST" }));
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});
