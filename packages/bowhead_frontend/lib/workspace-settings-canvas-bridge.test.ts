import { beforeEach, expect, it, vi } from "vitest";
import { apiJson, ApiError } from "@/lib/api-client";
import { performLiteAction } from "@/lib/config-copilot-lite";
import { WorkspaceSettingsCanvasBridge } from "./workspace-settings-canvas-bridge";
import type { WorkspaceI18nLifecycle } from "@/components/settings/workspace-i18n";

vi.mock("@/lib/api-client", () => ({ apiJson: vi.fn(), ApiError: class extends Error { constructor(public status: number) { super("Access denied"); } } }));
vi.mock("@/lib/config-copilot-lite", () => ({ performLiteAction: vi.fn() }));

const target = { page: "workspace_i18n" as const, userId: "u_1", teamId: "team_1", workspaceId: "ws_1" };
const clean: WorkspaceI18nLifecycle = { workspaceId: "ws_1", savedValues: { week_start_day: "monday" }, localValues: { week_start_day: "monday" }, editGeneration: 0, pendingWrites: 0, unconfirmedFields: [], error: null };
const proposal = { proposal_id: "ccd_1", turn_id: "t_1", form_id: "workspace_i18n", workspace_id: "ws_1", workspace_name: "Original Space", team_id: "team_1", notes: {}, rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }], settings_target: { form_id: "workspace_i18n" as const, user_id: "u_1", team_id: "team_1", workspace_id: "ws_1", edit_generation: 0 } };
const review = { ...proposal, owner_user_id: "u_1", status: "proposed" };
function fixture() {
  const bridge = new WorkspaceSettingsCanvasBridge();
  let current = clean;
  const deny = vi.fn();
  const close = bridge.register({ controllerId: "original", target, readLifecycle: () => current, onAccessDenied: deny });
  const captured = bridge.capture(target);
  bridge.acceptTurn("t_1", captured);
  return { bridge, close, captured, deny, change: (value: WorkspaceI18nLifecycle) => { current = value; } };
}
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiJson).mockResolvedValue(review); });

it("queues a Tool open for the original page and stages its owned unbound proposal without writing", async () => {
  const bridge = new WorkspaceSettingsCanvasBridge();
  const unbound = { ...proposal, settings_target: undefined };
  bridge.acceptToolOpen("t_1", target);
  bridge.register({ controllerId: "tool-page", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  vi.mocked(apiJson).mockResolvedValue({ ...review, settings_target: null });
  expect(bridge.routesTo(unbound, "tool-page")).toBe(true);
  await expect(bridge.stage(unbound)).resolves.toMatchObject({ editGeneration: 0 });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("does not transfer a Tool operation to a reopened page on event replay", async () => {
  const bridge = new WorkspaceSettingsCanvasBridge();
  const unbound = { ...proposal, settings_target: undefined };
  bridge.acceptToolOpen("t_1", target);
  const close = bridge.register({ controllerId: "original", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  close();
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  bridge.acceptToolOpen("t_1", target);
  await expect(bridge.perform(unbound, "apply")).rejects.toMatchObject({ code: "page_not_ready" });
  expect(bridge.routesTo(unbound, "reopened")).toBe(false);
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("retains the same Tool page incarnation during development effect replay", async () => {
  const bridge = new WorkspaceSettingsCanvasBridge();
  bridge.acceptToolOpen("t_1", target);
  const close = bridge.register({ controllerId: "original", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  close();
  bridge.register({ controllerId: "original", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  const unbound = { ...proposal, settings_target: undefined };
  vi.mocked(apiJson).mockResolvedValue({ ...review, settings_target: null });
  await expect(bridge.stage(unbound)).resolves.toMatchObject({ editGeneration: 0 });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("protects manual input made after a Tool opened the original page", async () => {
  const bridge = new WorkspaceSettingsCanvasBridge();
  let current = clean;
  bridge.register({ controllerId: "original", target, readLifecycle: () => current, onAccessDenied: vi.fn() });
  bridge.acceptToolOpen("t_1", target);
  current = { ...clean, editGeneration: 1 };
  await expect(bridge.stage({ ...proposal, settings_target: undefined })).rejects.toMatchObject({ code: "local_changes" });
  expect(apiJson).not.toHaveBeenCalled();
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("waits for a Tool-opened original page's first canonical load", async () => {
  const bridge = new WorkspaceSettingsCanvasBridge();
  let current: WorkspaceI18nLifecycle | null = null;
  bridge.acceptToolOpen("t_1", target);
  bridge.register({ controllerId: "original", target, readLifecycle: () => current, onAccessDenied: vi.fn() });
  const unbound = { ...proposal, settings_target: undefined };
  await expect(bridge.stage(unbound)).rejects.toMatchObject({ code: "page_not_ready" });
  current = clean;
  bridge.pageChanged("original");
  vi.mocked(apiJson).mockResolvedValue({ ...review, settings_target: null });
  await expect(bridge.stage(unbound)).resolves.toMatchObject({ editGeneration: 0 });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("stages an accepted original Turn with one authorized GET and no settings mutation", async () => {
  const { bridge } = fixture();
  await Promise.all([bridge.stage(proposal), bridge.stage(proposal)]);
  expect(apiJson).toHaveBeenCalledExactlyOnceWith("/api/v1/config-proposals/ccd_1/settings-review");
  expect(performLiteAction).not.toHaveBeenCalled();
  expect(bridge.routesTo(proposal, "original")).toBe(true);
});

it("does not transfer an old accepted Turn to a reopened page with the same key and generation", async () => {
  const { bridge, close } = fixture();
  await bridge.stage(proposal);
  close();
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  bridge.acceptTurn("t_1", bridge.capture(target));
  await expect(bridge.perform(proposal, "apply")).rejects.toMatchObject({ code: "page_not_ready" });
  expect(bridge.routesTo(proposal, "reopened")).toBe(false);
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("fences an in-flight review after closing and reopening the original target", async () => {
  let finish!: (value: typeof review) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { bridge, close } = fixture();
  const staged = bridge.stage(proposal);
  close();
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  finish(review);
  await expect(staged).rejects.toMatchObject({ code: "page_not_ready" });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("tracks a confirmation's original controller before its authorized review arrives", async () => {
  let finish!: (value: typeof review) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { bridge, close } = fixture();
  const pending = bridge.perform(proposal, "apply");
  expect(bridge.hasPendingAction("original")).toBe(true);
  close();
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  expect(bridge.hasPendingAction("reopened")).toBe(false);
  finish(review);
  await expect(pending).rejects.toMatchObject({ code: "page_not_ready" });
  expect(bridge.hasPendingAction("original")).toBe(false);
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("checks the latest original manual lifecycle for confirmation from either surface", async () => {
  const { bridge, change } = fixture();
  await bridge.stage(proposal);
  change({ ...clean, editGeneration: 1, pendingWrites: 1, localValues: { week_start_day: "sunday" } });
  await expect(bridge.perform(proposal, "apply")).rejects.toMatchObject({ code: "local_changes" });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("rejects a mismatched authorized Turn instead of trusting the event projection", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ...review, turn_id: "t_foreign" });
  await expect(fixture().bridge.stage(proposal)).rejects.toMatchObject({ code: "target_mismatch" });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("invalidates the original host on authoritative access denial without a write", async () => {
  vi.mocked(apiJson).mockRejectedValue(new ApiError(403, "Forbidden"));
  const { bridge, deny } = fixture();
  await expect(bridge.stage(proposal)).rejects.toMatchObject({ status: 403 });
  expect(deny).toHaveBeenCalledOnce();
  expect(() => bridge.capture(target)).toThrow("page_not_ready");
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("excludes simultaneous Chat and Canvas confirmations of the same proposal", async () => {
  let finish!: (value: null) => void;
  vi.mocked(performLiteAction).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { bridge } = fixture();
  await bridge.stage(proposal);
  const first = bridge.perform(proposal, "apply");
  await expect(bridge.perform(proposal, "apply")).rejects.toThrow("settings_action_pending");
  finish(null);
  expect(await first).toBeNull();
  expect(performLiteAction).toHaveBeenCalledOnce();
});

it("uses actual receipts for apply and undo while preserving the original target", async () => {
  const { bridge, change } = fixture();
  const receipt = { proposal_id: "ccd_1", form_id: "workspace_i18n", status: "applied", owner_user_id: "u_1", team_id: "team_1", workspace_id: "ws_1" };
  vi.mocked(performLiteAction).mockResolvedValue(receipt);
  expect(await bridge.perform(proposal, "apply")).toBe(receipt);
  change({ ...clean, savedValues: { week_start_day: "sunday" }, localValues: { week_start_day: "sunday" } });
  vi.mocked(performLiteAction).mockResolvedValue({ ...receipt, status: "reverted" });
  expect(await bridge.perform(proposal, "undo")).toMatchObject({ status: "reverted", workspace_id: "ws_1" });
});


it("isolates late access denial from a closed original controller and its reopened target", async () => {
  let reject!: (error: ApiError) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
  const { bridge, close, deny } = fixture();
  const pending = bridge.stage(proposal);
  close();
  const reopenedDeny = vi.fn();
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => clean, onAccessDenied: reopenedDeny });
  reject(new ApiError(403, "Forbidden"));
  await expect(pending).rejects.toMatchObject({ status: 403 });
  expect(deny).not.toHaveBeenCalled();
  expect(reopenedDeny).not.toHaveBeenCalled();
  expect(bridge.capture(target).controllerId).toBe("reopened");
  expect(performLiteAction).not.toHaveBeenCalled();
});


it("rechecks each event's original Turn when sharing an in-flight authorized review", async () => {
  let finish!: (value: typeof review) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const { bridge } = fixture();
  bridge.acceptTurn("t_later", bridge.capture(target));
  const original = bridge.stage(proposal);
  const mismatched = bridge.stage({ ...proposal, turn_id: "t_later" });
  finish(review);
  await expect(original).resolves.toMatchObject({ proposal: { turn_id: "t_1" } });
  await expect(mismatched).rejects.toMatchObject({ code: "target_mismatch" });
  expect(apiJson).toHaveBeenCalledOnce();
  expect(performLiteAction).not.toHaveBeenCalled();
});


it("requires explicit review on a reopened page without transferring the old Turn", async () => {
  const { bridge, close } = fixture();
  await bridge.stage(proposal);
  close();
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => ({ ...clean, editGeneration: 4 }), onAccessDenied: vi.fn() });
  await expect(bridge.stage(proposal)).rejects.toMatchObject({ code: "page_not_ready" });
  expect(bridge.canReviewSaved(proposal)).toBe(true);
  await expect(bridge.reviewSaved(proposal)).resolves.toMatchObject({ editGeneration: 4 });
  expect(performLiteAction).not.toHaveBeenCalled();
  expect(bridge.routesTo(proposal, "reopened")).toBe(true);
  await expect(bridge.stage({ ...proposal, proposal_id: "ccd_late" })).rejects.toMatchObject({ code: "page_not_ready" });
  await bridge.perform(proposal, "apply");
  expect(performLiteAction).toHaveBeenCalledOnce();
});

it("admits an applied preference only after explicit review and rechecks later input before undo", async () => {
  const { bridge, close } = fixture();
  close();
  let current = { ...clean, savedValues: { week_start_day: "sunday" }, localValues: { week_start_day: "sunday" }, editGeneration: 4 };
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => current, onAccessDenied: vi.fn() });
  vi.mocked(apiJson).mockResolvedValue({ ...review, status: "applied", rows: [{ ...review.rows[0], from_unset: true }] });
  await expect(bridge.perform(proposal, "undo")).rejects.toMatchObject({ code: "page_not_ready" });
  const staged = await bridge.reviewSaved(proposal);
  expect(staged.status).toBe("applied");
  expect(staged.proposal.rows[0].from_unset).toBe(true);
  expect(performLiteAction).not.toHaveBeenCalled();
  await expect(bridge.perform(proposal, "apply")).rejects.toMatchObject({ code: "proposal_not_pending" });
  current = { ...current, editGeneration: 5 };
  await expect(bridge.perform(proposal, "undo")).rejects.toMatchObject({ code: "local_changes" });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it.each(["local", "baseline", "owner", "turn", "access"])("saved review rejects changed %s without any write", async (failure) => {
  const { bridge, close } = fixture();
  close();
  const deny = vi.fn();
  const current = failure === "local" ? { ...clean, unconfirmedFields: ["week_start_day"] }
    : failure === "baseline" ? { ...clean, savedValues: { week_start_day: "saturday" } } : clean;
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => current, onAccessDenied: deny });
  if (failure === "owner") vi.mocked(apiJson).mockResolvedValue({ ...review, owner_user_id: "u_other" });
  if (failure === "turn") vi.mocked(apiJson).mockResolvedValue({ ...review, turn_id: "t_other" });
  if (failure === "access") vi.mocked(apiJson).mockRejectedValue(new ApiError(403, "Forbidden"));
  await expect(bridge.reviewSaved(proposal)).rejects.toThrow();
  await expect(bridge.perform(proposal, "apply")).rejects.toThrow();
  expect(bridge.routesTo(proposal, "reopened")).toBe(false);
  expect(performLiteAction).not.toHaveBeenCalled();
  expect(deny).toHaveBeenCalledTimes(failure === "access" ? 1 : 0);
});

it("does not attach a late saved review to another reopened instance", async () => {
  const { bridge, close } = fixture();
  close();
  const dispose = bridge.register({ controllerId: "reviewed", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  let finish!: (value: typeof review) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const pending = bridge.reviewSaved(proposal);
  dispose();
  bridge.register({ controllerId: "new-instance", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  finish(review);
  await expect(pending).rejects.toMatchObject({ code: "page_not_ready" });
  expect(bridge.routesTo(proposal, "new-instance")).toBe(false);
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("protects edits made during or after explicit saved review", async () => {
  const { bridge, close } = fixture();
  close();
  let current = clean;
  bridge.register({ controllerId: "reopened", target, readLifecycle: () => current, onAccessDenied: vi.fn() });
  let finish!: (value: typeof review) => void;
  vi.mocked(apiJson).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const pending = bridge.reviewSaved(proposal);
  current = { ...clean, editGeneration: 1 };
  finish(review);
  await expect(pending).rejects.toMatchObject({ code: "local_changes" });
  await bridge.reviewSaved(proposal);
  current = { ...clean, editGeneration: 2 };
  await expect(bridge.perform(proposal, "apply")).rejects.toMatchObject({ code: "local_changes" });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it.each([true, false])("does not admit replayed Tool work regardless of page/event ordering (%s)", async (mountedFirst) => {
  const bridge = new WorkspaceSettingsCanvasBridge();
  const mount = () => bridge.register({ controllerId: "replayed-page", target, readLifecycle: () => clean, onAccessDenied: vi.fn() });
  if (mountedFirst) mount();
  bridge.acceptToolOpen(proposal.turn_id, target, "replay");
  if (!mountedFirst) mount();
  const unbound = { ...proposal, settings_target: undefined };
  await expect(bridge.stage(unbound)).rejects.toMatchObject({ code: "page_not_ready" });
  expect(bridge.canReviewSaved(unbound)).toBe(true);
  expect(apiJson).not.toHaveBeenCalled();
  expect(performLiteAction).not.toHaveBeenCalled();
});
