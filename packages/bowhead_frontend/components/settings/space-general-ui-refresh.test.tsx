/**
 * Space settings · General (page-by-page acceptance A): name and description sit in grouped cards and save through the
 * save bar; the multilingual feature toggle is an immediately effective Switch.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messages from "@/messages/zh-CN.json";

const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push }) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { id: "u_owner", team_id: "team_original", capabilities: { settings_pages_canvas: true } } }),
}));

vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({ items: [{ id: "ws_1", name: "销售" }], activeId: "ws_1", loading: false, error: null }),
  invalidateSpaces: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  getWorkspaceMetadata: vi.fn(),
  updateWorkspaceMetadata: vi.fn(),
  getWorkspaceSettings: vi.fn(),
  setWorkspaceSetting: vi.fn(),
}));

import { getWorkspaceMetadata, getWorkspaceSettings, setWorkspaceSetting, updateWorkspaceMetadata } from "@/lib/api";
import { SpaceGeneral } from "./space-general";

const META = { id: "ws_1", name: "销售", description: "旧描述", revision: 3 };

beforeEach(() => {
  vi.mocked(getWorkspaceMetadata).mockResolvedValue(META as never);
  vi.mocked(updateWorkspaceMetadata).mockImplementation(async (_id, body) => ({ ...META, ...body, revision: 4 }) as never);
  vi.mocked(getWorkspaceSettings).mockResolvedValue({ i18n_v1: "false" } as never);
  vi.mocked(setWorkspaceSetting).mockResolvedValue(undefined as never);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const renderPage = () => render(<NextIntlClientProvider locale="zh-CN" messages={messages}><SpaceGeneral /></NextIntlClientProvider>);

describe("SpaceGeneral", () => {
  it("shows the save bar after a rename, saves through the same endpoint with the revision and discards back to the original", async () => {
    const { container } = renderPage();
    const name = await screen.findByDisplayValue("销售");
    expect(container.querySelector(".ui-space-general-group .ui-group-card #space-general-name")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "保存更改" })).toBeNull();
    expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();

    fireEvent.change(name, { target: { value: "销售二部" } });
    expect((screen.getByRole("button", { name: messages.settings_canvas.open }) as HTMLButtonElement).disabled).toBe(true);
    const bar = screen.getByRole("region", { name: "有未保存的更改" });
    fireEvent.click(screen.getByRole("button", { name: "放弃" }));
    expect(screen.getByDisplayValue("销售")).toBeTruthy();
    expect(bar.isConnected).toBe(false);

    fireEvent.change(screen.getByDisplayValue("销售"), { target: { value: "销售二部" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(updateWorkspaceMetadata).toHaveBeenCalledWith("ws_1", { name: "销售二部", expected_revision: 3 }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: messages.settings_canvas.open }));
    const opened = new URL(push.mock.calls[0][0], window.location.origin);
    expect(opened.searchParams.get("canvas_user")).toBe("u_owner");
    expect(opened.searchParams.get("canvas_team")).toBe("team_original");
    expect(opened.searchParams.get("canvas_space")).toBe("ws_1");
  });

  it("disables Save in the save bar when the name is blank", async () => {
    renderPage();
    fireEvent.change(await screen.findByDisplayValue("销售"), { target: { value: "  " } });
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("writes the multilingual feature Switch immediately", async () => {
    renderPage();
    const toggle = await screen.findByRole("switch", { name: messages.settings.workspace_i18n.field_flag_label });
    expect(screen.queryByRole("checkbox")).toBeNull();
    fireEvent.click(toggle);
    await waitFor(() => expect(setWorkspaceSetting).toHaveBeenCalledWith("ws_1", "i18n_v1", "true"));
  });
});
