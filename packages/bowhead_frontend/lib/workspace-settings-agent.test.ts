import { beforeEach, expect, it, vi } from "vitest";
import { apiJson } from "@/lib/api-client";
import { performLiteAction } from "@/lib/config-copilot-lite";
import { performWorkspaceSettingsSuggestionAction, stageWorkspaceSettingsSuggestion, workspaceSettingsClientInfo } from "./workspace-settings-agent";
import type { WorkspaceI18nLifecycle } from "@/components/settings/workspace-i18n";

vi.mock("@/lib/api-client", () => ({ apiJson: vi.fn(), ApiError: class extends Error {} }));
vi.mock("@/lib/config-copilot-lite", () => ({ performLiteAction: vi.fn() }));
const target = { page: "workspace_i18n" as const, userId: "u_1", teamId: "team_1", workspaceId: "ws_1" };
const initial: WorkspaceI18nLifecycle = {
  workspaceId: "ws_1",
  savedValues: { week_start_day: "monday" },
  localValues: { week_start_day: "monday" },
  editGeneration: 3,
  pendingWrites: 0,
  unconfirmedFields: [],
  error: null,
};
const review = {
  proposal_id: "ccd_1",
  form_id: "workspace_i18n",
  owner_user_id: "u_1",
  team_id: "team_1",
  workspace_id: "ws_1",
  workspace_name: "Original Space",
  turn_id: "t_1",
  status: "proposed",
  rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }],
  notes: {},
  settings_target: { form_id: "workspace_i18n", user_id: "u_1", team_id: "team_1", workspace_id: "ws_1", edit_generation: 3 },
};
beforeEach(() => vi.resetAllMocks());

it("copies the exact send selection and blocks pending or unconfirmed manual input", () => {
  const info = workspaceSettingsClientInfo(target, initial);
  expect(JSON.parse(info.configuration_settings_target)).toMatchObject({ workspace_id: "ws_1", edit_generation: 3, pending_writes: 0 });
  expect(() => workspaceSettingsClientInfo(target, { ...initial, pendingWrites: 1 })).toThrow("local_changes");
  expect(() => workspaceSettingsClientInfo(target, { ...initial, unconfirmedFields: ["week_start_day"] })).toThrow("local_changes");
});

it("stages the authorized original suggestion through GET without changing the page values", async () => {
  vi.mocked(apiJson).mockResolvedValue(review);
  const result = await stageWorkspaceSettingsSuggestion("ccd_1", target, () => initial);
  expect(apiJson).toHaveBeenCalledExactlyOnceWith("/api/v1/config-proposals/ccd_1/settings-review");
  expect(result.proposal.rows).toEqual(review.rows);
  expect(initial.localValues.week_start_day).toBe("monday");
  expect(result.target).toEqual(target);
});

it("checks the latest lifecycle after an asynchronous read instead of overwriting newer input", async () => {
  let resolve!: (value: typeof review) => void;
  vi.mocked(apiJson).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  let current = initial;
  const staging = stageWorkspaceSettingsSuggestion("ccd_1", target, () => current);
  current = { ...initial, editGeneration: 4, localValues: { week_start_day: "sunday" } };
  resolve(review);
  await expect(staging).rejects.toMatchObject({ code: "local_changes" });
  expect(current.localValues.week_start_day).toBe("sunday");
});

it("rejects another original target and a closed page", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ...review, workspace_id: "ws_other" });
  await expect(stageWorkspaceSettingsSuggestion("ccd_1", target, () => initial)).rejects.toMatchObject({ code: "target_mismatch" });
  await expect(stageWorkspaceSettingsSuggestion("ccd_1", target, () => null)).rejects.toMatchObject({ code: "page_not_ready" });
});

it("rejects stale affected values while retaining other manual fields", async () => {
  vi.mocked(apiJson).mockResolvedValue(review);
  await expect(
    stageWorkspaceSettingsSuggestion("ccd_1", target, () => ({ ...initial, savedValues: { week_start_day: "sunday" } })),
  ).rejects.toMatchObject({ code: "baseline_changed" });
  const result = await stageWorkspaceSettingsSuggestion("ccd_1", target, () => ({
    ...initial,
    localValues: { ...initial.localValues, default_ui_language: "fr-FR" },
  }));
  expect(result.proposal.rows).toHaveLength(1);
});

it("blocks manual changes made after staging before attempting confirmation", async () => {
  vi.mocked(apiJson).mockResolvedValue(review);
  const staged = await stageWorkspaceSettingsSuggestion("ccd_1", target, () => initial);
  await expect(performWorkspaceSettingsSuggestionAction(staged, () => ({ ...initial, editGeneration: 4 }), "apply")).rejects.toMatchObject({
    code: "local_changes",
  });
  expect(apiJson).toHaveBeenCalledTimes(1);
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("stages a Tool-opened ordinary chat suggestion and preserves input during its read", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ...review, settings_target: null });
  const staged = await stageWorkspaceSettingsSuggestion("ccd_1", target, () => initial);
  expect(staged.proposal.settings_target).toBeUndefined();
  let resolve!: (value: unknown) => void;
  vi.mocked(apiJson).mockImplementation(() => new Promise((done) => { resolve = done; }));
  let latest = initial;
  const pending = stageWorkspaceSettingsSuggestion("ccd_1", target, () => latest);
  latest = { ...initial, editGeneration: initial.editGeneration + 1 };
  resolve({ ...review, settings_target: null });
  await expect(pending).rejects.toMatchObject({ code: "local_changes" });
});

it("reviews an applied preference for undo against its applied value and preserves an unset original", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ...review, status: "applied", rows: [{ ...review.rows[0], from_unset: true }] });
  const applied = { ...initial, editGeneration: 8, savedValues: { week_start_day: "sunday" }, localValues: { week_start_day: "sunday" } };
  const staged = await stageWorkspaceSettingsSuggestion("ccd_1", target, () => applied, "saved_review");
  expect(staged.status).toBe("applied");
  expect(staged.proposal.rows[0].from_unset).toBe(true);
  expect(performLiteAction).not.toHaveBeenCalled();
  await expect(performWorkspaceSettingsSuggestionAction(staged, () => applied, "apply")).rejects.toMatchObject({ code: "proposal_not_pending" });
  await performWorkspaceSettingsSuggestionAction(staged, () => applied, "undo");
  expect(performLiteAction).toHaveBeenCalledExactlyOnceWith(staged.proposal, "undo", expect.any(Function));
});

it("rejects applied recovery without explicit review or after its affected value changes", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ...review, status: "applied" });
  await expect(stageWorkspaceSettingsSuggestion("ccd_1", target, () => initial)).rejects.toMatchObject({ code: "proposal_not_pending" });
  await expect(stageWorkspaceSettingsSuggestion("ccd_1", target, () => initial, "saved_review")).rejects.toMatchObject({ code: "baseline_changed" });
  expect(performLiteAction).not.toHaveBeenCalled();
});

it("does not restore undo for applied metadata", async () => {
  vi.mocked(apiJson).mockResolvedValue({ ...review, form_id: "workspace_metadata", status: "applied", settings_target: null });
  await expect(stageWorkspaceSettingsSuggestion("ccd_1", { ...target, page: "space_general" }, () => initial, "saved_review")).rejects.toMatchObject({ code: "proposal_not_pending" });
  expect(performLiteAction).not.toHaveBeenCalled();
});
