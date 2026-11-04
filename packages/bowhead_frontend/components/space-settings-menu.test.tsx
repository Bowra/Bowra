import "@testing-library/jest-dom/vitest";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderIntl } from "@/__tests__/intl-render";
import { apiJson, apiVoid, ApiError } from "@/lib/api-client";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { useSpace, readSpaces, switchActiveSpace, restoreActiveSpace, invalidateSpaces } from "@/lib/use-space";
import { toast } from "@/lib/use-toast";
import { SpacePicker } from "./space-picker";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/use-app-router", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/use-app-router")>(), useAppRouter: () => ({ push, replace }),
}));
vi.mock("@/lib/api-client", async importOriginal => ({
  ...await importOriginal<typeof import("@/lib/api-client")>(), apiJson: vi.fn(), apiVoid: vi.fn(),
}));
vi.mock("@/lib/use-space", () => ({
  useSpace: vi.fn(), readSpaces: vi.fn(), switchActiveSpace: vi.fn(), restoreActiveSpace: vi.fn(), invalidateSpaces: vi.fn(),
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useSpace).mockReturnValue({
    items: [{ id: "ws_current", name: "Sales", desc: "", role: "member" }], activeId: "ws_current", activeName: "Sales",
    loading: false, error: null, errorCause: null, setActiveId: vi.fn(), refresh: vi.fn(),
  });
  vi.mocked(apiJson).mockResolvedValue({ can_leave: true });
  vi.mocked(apiVoid).mockResolvedValue();
  vi.mocked(readSpaces).mockResolvedValue({ items: [], active_id: null });
});

async function openSettings() {
  fireEvent.click(screen.getByRole("button", { name: "切换空间，当前：Sales" }));
  expect(screen.queryByRole("menuitem", { name: "退出当前空间…" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("menuitem", { name: "更多空间设置" }));
  return screen.findByRole("menuitem", { name: "退出当前空间…" });
}
async function openLeave() {
  const leave = await openSettings();
  await waitFor(() => expect(leave).not.toHaveAttribute("aria-disabled", "true"));
  fireEvent.click(leave);
  return screen.findByRole("dialog", { name: "退出「Sales」？" });
}

describe("Space settings submenu and self-exit", () => {
  it.each(["member", "viewer"] as const)("lets a %s leave without exposing administrator settings", async role => {
    vi.mocked(useSpace).mockReturnValue({ ...useSpace(), items: [{ id: "ws_current", name: "Sales", desc: "", role }] });
    renderIntl(<SpacePicker />);
    const leave = await openSettings();
    for (const name of ["常规", "AI 工作说明", "空间访问"]) expect(screen.queryByRole("menuitem", { name })).not.toBeInTheDocument();
    await waitFor(() => expect(leave).not.toHaveAttribute("aria-disabled", "true"));
    fireEvent.click(leave);
    await screen.findByRole("dialog", { name: "退出「Sales」？" });
    expect(apiVoid).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(apiVoid).not.toHaveBeenCalled();
  });
  it.each([ ["常规", "general"], ["AI 工作说明", "instructions"], ["空间访问", "members"] ])("links %s to its existing page for space administrators", async (label, page) => {
    vi.mocked(useSpace).mockReturnValue({ ...useSpace(), items: [{ id: "ws_current", name: "Sales", desc: "", role: "admin" }] });
    renderIntl(<SpacePicker />);
    await openSettings();
    const link = screen.getByRole("menuitem", { name: label });
    expect(link).toHaveAttribute("href", `/space/settings/${page}/`);
    fireEvent.click(link);
    expect(push).toHaveBeenCalledWith(`/space/settings/${page}/`);
  });
  it("keeps the last administrator's exit disabled with a permanent reason", async () => {
    vi.mocked(apiJson).mockResolvedValue({ can_leave: false });
    renderIntl(<SpacePicker />);
    const leave = await openSettings();
    const reason = "你是该空间的最后一位有效管理员，请先指定另一位管理员，再退出。";
    await screen.findByText(reason);
    expect(leave).toHaveAttribute("aria-disabled", "true");
    expect(leave).toHaveAccessibleDescription(reason);
    fireEvent.click(leave);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("retries a failed eligibility check without granting access", async () => {
    vi.mocked(apiJson).mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ can_leave: true });
    renderIntl(<SpacePicker />);
    const leave = await openSettings();
    fireEvent.click(await screen.findByRole("menuitem", { name: "重试检查" }));
    await waitFor(() => expect(leave).not.toHaveAttribute("aria-disabled", "true"));
    expect(apiVoid).not.toHaveBeenCalled();
  });
  it("retains access when an unsaved editor cancels leaving", async () => {
    const unregister = registerSubagentDraftNavigationGuard({ confirmLeave: async () => false });
    try {
      renderIntl(<SpacePicker />);
      await openLeave();
      fireEvent.click(screen.getByRole("button", { name: "退出当前空间" }));
      await waitFor(() => expect(screen.getByRole("button", { name: "退出当前空间" })).not.toBeDisabled());
      expect(apiVoid).not.toHaveBeenCalled();
      expect(replace).not.toHaveBeenCalled();
    } finally { unregister(); }
  });
  it.each([false, true])("refreshes selection after exit (another space: %s)", async another => {
    vi.mocked(readSpaces).mockResolvedValue({ items: another ? [{ id: "ws_next", name: "Next", desc: "" }] : [], active_id: null });
    localStorage.setItem("bowhead.space_id", "ws_current");
    renderIntl(<SpacePicker />);
    await openLeave();
    fireEvent.click(screen.getByRole("button", { name: "退出当前空间" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(apiVoid).toHaveBeenCalledWith("/api/v1/workspaces/ws_current/leave", { method: "POST" });
    expect(invalidateSpaces).toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith({ kind: "success", text: "已退出「Sales」" });
    expect(restoreActiveSpace).toHaveBeenCalledWith("");
    expect(localStorage.getItem("bowhead.space_id")).toBeNull();
    if (another) expect(switchActiveSpace).toHaveBeenCalledWith("ws_next");
    else expect(switchActiveSpace).not.toHaveBeenCalled();
  });
  it("retains the dialog and context when the final check rejects exit", async () => {
    vi.mocked(apiVoid).mockRejectedValue(new ApiError(422, "blocked", { code: "last_workspace_admin" }));
    renderIntl(<SpacePicker />);
    await openLeave();
    fireEvent.click(screen.getByRole("button", { name: "退出当前空间" }));
    await screen.findByText("你是该空间的最后一位有效管理员，请先指定另一位管理员，再退出。");
    expect(replace).not.toHaveBeenCalled();
    expect(invalidateSpaces).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});
