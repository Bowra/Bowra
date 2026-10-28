import { useState } from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import messages from "@/messages/en-US.json";

vi.mock("@/lib/api", () => ({
  getWorkspaceSettings: vi.fn(),
  setWorkspaceSetting: vi.fn(),
}));
vi.mock("@/lib/team-storage", () => ({ getActiveTeamId: () => "team_original" }));

import { getWorkspaceSettings, setWorkspaceSetting } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { notifyWorkspaceSettingsSaved } from "@/lib/workspace-settings-notifications";
import { SettingsWorkspaceI18n, WorkspaceI18nFields, type WorkspaceI18nAgentInterface } from "./workspace-i18n";

const labels = messages.settings.workspace_i18n;

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it("removes inaccessible settings and informs the host when canonical readback loses access", async () => {
  const denied = vi.fn();
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  renderIntl(<SettingsWorkspaceI18n workspaceId="ws_original" onAccessDenied={denied} />, { locale: "en-US" });
  await screen.findByRole("combobox", { name: labels.field_week_label });
  vi.mocked(getWorkspaceSettings).mockRejectedValue(new ApiError(403, "Access denied"));
  notifyWorkspaceSettingsSaved({ teamId: "team_original", workspaceId: "ws_original" });
  await waitFor(() => expect(denied).toHaveBeenCalledOnce());
  expect(screen.queryByRole("combobox", { name: labels.field_week_label })).toBeNull();
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("hydrates supported candidate fields without reading or writing settings", () => {
  const changed = vi.fn();
  function Candidate() {
    const [week, setWeek] = useState("monday");
    return (
      <>
        <button onClick={() => setWeek("sunday")}>Stage suggestion</button>
        <WorkspaceI18nFields settings={{ week_start_day: week }} fields={["week_start_day"]} onChange={changed} />
      </>
    );
  }
  renderIntl(<Candidate />, { locale: "en-US" });
  fireEvent.click(screen.getByRole("button", { name: "Stage suggestion" }));
  expect(
    (
      screen.getByRole("combobox", {
        name: labels.field_week_label,
      }) as HTMLSelectElement
    ).value,
  ).toBe("sunday");
  expect(screen.getAllByRole("combobox")).toHaveLength(1);
  expect(changed).not.toHaveBeenCalled();
  expect(getWorkspaceSettings).not.toHaveBeenCalled();
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("retains the settings page's immediate-save policy", async () => {
  vi.mocked(getWorkspaceSettings).mockResolvedValue({
    week_start_day: "monday",
  });
  vi.mocked(setWorkspaceSetting).mockResolvedValue(undefined);
  renderIntl(<SettingsWorkspaceI18n workspaceId="ws_original" />, {
    locale: "en-US",
  });
  const select = await screen.findByRole("combobox", {
    name: labels.field_week_label,
  });
  fireEvent.change(select, { target: { value: "sunday" } });
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledWith("ws_original", "week_start_day", "sunday"));
});

it("keeps a late original-Space load out of the new target", async () => {
  let resolveOriginal!: (value: { week_start_day: string }) => void;
  vi.mocked(getWorkspaceSettings).mockImplementation((id) =>
    id === "ws_original"
      ? new Promise((resolve) => {
          resolveOriginal = resolve;
        })
      : Promise.resolve({ week_start_day: "sunday" }),
  );
  function Page() {
    const [target, setTarget] = useState("ws_original");
    return (
      <>
        <button onClick={() => setTarget("ws_new")}>Switch Space</button>
        <SettingsWorkspaceI18n workspaceId={target} />
      </>
    );
  }
  renderIntl(<Page />, { locale: "en-US" });
  fireEvent.click(screen.getByRole("button", { name: "Switch Space" }));
  await screen.findByRole("combobox", { name: labels.field_week_label });
  resolveOriginal({ week_start_day: "monday" });
  await waitFor(() =>
    expect(
      (
        screen.getByRole("combobox", {
          name: labels.field_week_label,
        }) as HTMLSelectElement
      ).value,
    ).toBe("sunday"),
  );
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("defers confirmed readback while a manual save is pending and exposes both values", async () => {
  let saved!: () => void;
  vi.mocked(getWorkspaceSettings).mockResolvedValueOnce({ week_start_day: "monday" }).mockResolvedValueOnce({ week_start_day: "sunday" });
  vi.mocked(setWorkspaceSetting).mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        saved = resolve;
      }),
  );
  const lifecycle = vi.fn();
  renderIntl(<SettingsWorkspaceI18n workspaceId="ws_original" onLifecycleChange={lifecycle} />, { locale: "en-US" });
  const select = await screen.findByRole("combobox", { name: labels.field_week_label });
  fireEvent.change(select, { target: { value: "sunday" } });
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledTimes(1));
  expect(lifecycle).toHaveBeenLastCalledWith(
    expect.objectContaining({
      savedValues: { week_start_day: "monday" },
      localValues: { week_start_day: "sunday" },
      pendingWrites: 1,
      editGeneration: 1,
    }),
  );
  notifyWorkspaceSettingsSaved({ teamId: "team_original", workspaceId: "ws_original" });
  expect(getWorkspaceSettings).toHaveBeenCalledTimes(1);
  saved();
  await waitFor(() => expect(getWorkspaceSettings).toHaveBeenCalledTimes(2));
  expect((select as HTMLSelectElement).value).toBe("sunday");
  await waitFor(() => expect(lifecycle).toHaveBeenLastCalledWith(expect.objectContaining({ pendingWrites: 0, unconfirmedFields: [] })));
});

it("discards a refresh that predates newer input and reads the canonical value again", async () => {
  let staleRead!: (value: { week_start_day: string }) => void;
  vi.mocked(getWorkspaceSettings)
    .mockResolvedValueOnce({ week_start_day: "monday" })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          staleRead = resolve;
        }),
    )
    .mockResolvedValueOnce({ week_start_day: "sunday" });
  vi.mocked(setWorkspaceSetting).mockResolvedValue(undefined);
  renderIntl(<SettingsWorkspaceI18n workspaceId="ws_original" />, { locale: "en-US" });
  const select = await screen.findByRole("combobox", { name: labels.field_week_label });
  notifyWorkspaceSettingsSaved({ teamId: "team_original", workspaceId: "ws_original" });
  fireEvent.change(select, { target: { value: "sunday" } });
  await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledTimes(1));
  staleRead({ week_start_day: "monday" });
  await waitFor(() => expect(getWorkspaceSettings).toHaveBeenCalledTimes(3));
  expect((select as HTMLSelectElement).value).toBe("sunday");
});

it("preserves unconfirmed local input when a save fails and another receipt triggers readback", async () => {
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  vi.mocked(setWorkspaceSetting).mockRejectedValue(new Error("Save outcome unavailable"));
  const lifecycle = vi.fn();
  renderIntl(<SettingsWorkspaceI18n workspaceId="ws_original" onLifecycleChange={lifecycle} />, { locale: "en-US" });
  const select = await screen.findByRole("combobox", { name: labels.field_week_label });
  fireEvent.change(select, { target: { value: "sunday" } });
  await screen.findByText("Save outcome unavailable");
  notifyWorkspaceSettingsSaved({ teamId: "team_original", workspaceId: "ws_original" });
  await waitFor(() => expect(getWorkspaceSettings).toHaveBeenCalledTimes(2));
  expect((select as HTMLSelectElement).value).toBe("sunday");
  expect(lifecycle).toHaveBeenLastCalledWith(
    expect.objectContaining({
      savedValues: { week_start_day: "monday" },
      localValues: { week_start_day: "sunday" },
      unconfirmedFields: ["week_start_day"],
    }),
  );
});

it("ignores foreign targets and keeps a closed page closed after a save receipt", async () => {
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  const page = renderIntl(<SettingsWorkspaceI18n workspaceId="ws_original" />, { locale: "en-US" });
  await screen.findByRole("combobox", { name: labels.field_week_label });
  notifyWorkspaceSettingsSaved({ teamId: "team_other", workspaceId: "ws_original" });
  notifyWorkspaceSettingsSaved({ teamId: "team_original", workspaceId: "ws_other" });
  expect(getWorkspaceSettings).toHaveBeenCalledTimes(1);
  page.unmount();
  notifyWorkspaceSettingsSaved({ teamId: "team_original", workspaceId: "ws_original" });
  expect(getWorkspaceSettings).toHaveBeenCalledTimes(1);
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});

it("exposes protected receipt readback and releases the interface when the page closes", async () => {
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ week_start_day: "monday" });
  let handle: WorkspaceI18nAgentInterface | null = null;
  const page = renderIntl(
    <SettingsWorkspaceI18n
      workspaceId="ws_original"
      onAgentInterfaceChange={(value) => {
        handle = value;
      }}
    />,
    { locale: "en-US" },
  );
  await screen.findByRole("combobox", { name: labels.field_week_label });
  const original = handle!;
  const receipt = {
    proposal_id: "ccd_1",
    form_id: "workspace_i18n",
    status: "applied",
    workspace_id: "ws_original",
    team_id: "team_original",
    owner_user_id: "u_original",
  };
  expect(await original.requestReadback({ ...receipt, status: "proposed" })).toBe(false);
  expect(await original.requestReadback({ ...receipt, workspace_id: "ws_other" })).toBe(false);
  expect(getWorkspaceSettings).toHaveBeenCalledTimes(1);
  expect(await original.requestReadback(receipt)).toBe(true);
  await waitFor(() => expect(getWorkspaceSettings).toHaveBeenCalledTimes(2));
  page.unmount();
  expect(handle).toBeNull();
  expect(await original.requestReadback(receipt)).toBe(false);
  expect(setWorkspaceSetting).not.toHaveBeenCalled();
});
