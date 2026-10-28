import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { getReferenceRetention, setReferenceRetention } from "@/lib/reference-retention-api";
import { ReferenceRetentionSettings } from "./reference-retention";

const auth = vi.hoisted(() => ({ me: { id: "user", team_id: "team-a", team_role: "owner" } }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useAuthSession: () => ({ status: "authenticated", me: auth.me }) }));
vi.mock("@/lib/reference-retention-api", () => ({ getReferenceRetention: vi.fn(), setReferenceRetention: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("Reference retention settings", () => {
  beforeEach(() => {
    auth.me = { id: "user", team_id: "team-a", team_role: "owner" };
    vi.mocked(getReferenceRetention).mockResolvedValue({ days: 180, version: 2 });
  });
  afterEach(() => { cleanup(); vi.resetAllMocks(); });

  it("confirms shortening, prevents duplicate saves, and displays only the acknowledged result", async () => {
    const saved = deferred<{ days: number; version: number }>();
    vi.mocked(setReferenceRetention).mockReturnValue(saved.promise);
    renderIntl(<ReferenceRetentionSettings />);
    const input = await screen.findByLabelText("最多保留天数");
    fireEvent.change(input, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(setReferenceRetention).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(setReferenceRetention).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent.click(screen.getByRole("button", { name: "确认缩短" }));
    fireEvent.click(screen.getByRole("button", { name: "确认缩短" }));
    expect(setReferenceRetention).toHaveBeenCalledOnce();
    expect(setReferenceRetention).toHaveBeenCalledWith(7, 2, expect.any(AbortSignal));
    expect(screen.queryByText("保留期限已保存。")).toBeNull();
    await act(async () => saved.resolve({ days: 7, version: 3 }));
    expect(await screen.findByText("当前上限：7 天")).toBeTruthy();
    expect(screen.getByText("保留期限已保存。")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each([new ApiError(409, "secret"), new Error("secret")])("requires a fresh read after a failed or unconfirmed save", async (error) => {
    vi.mocked(getReferenceRetention).mockResolvedValueOnce({ days: 7, version: 1 }).mockResolvedValue({ days: 14, version: 2 });
    vi.mocked(setReferenceRetention).mockRejectedValue(error);
    renderIntl(<ReferenceRetentionSettings />);
    fireEvent.change(await screen.findByLabelText("最多保留天数"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("alert");
    expect(screen.queryByText("secret")).toBeNull();
    // An unconfirmed save hides the save bar until the settings are read again.
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
    expect(screen.queryByText("保留期限已保存。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新加载设置" }));
    expect(await screen.findByText("当前上限：14 天")).toBeTruthy();
    expect((screen.getByLabelText("最多保留天数") as HTMLInputElement).value).toBe("14");
    expect(setReferenceRetention).toHaveBeenCalledOnce();
  });

  it("drops an old team's late load response", async () => {
    const pending = deferred<{ days: number; version: number }>();
    vi.mocked(getReferenceRetention).mockReturnValueOnce(pending.promise).mockResolvedValue({ days: 30, version: 0 });
    const view = renderIntl(<ReferenceRetentionSettings />);
    auth.me = { ...auth.me, team_id: "team-b" };
    view.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}><ReferenceRetentionSettings /></NextIntlClientProvider>);
    await screen.findByText("当前上限：30 天");
    await act(async () => pending.resolve({ days: 7, version: 9 }));
    expect(screen.queryByText("当前上限：7 天")).toBeNull();
    expect((screen.getByLabelText("最多保留天数") as HTMLInputElement).value).toBe("30");
  });

  it("does not start a policy request for a member", () => {
    auth.me = { ...auth.me, team_role: "member" };
    renderIntl(<ReferenceRetentionSettings />);
    expect(getReferenceRetention).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it.each(["", "0", "181", "1.5", "abc"])("rejects an invalid number: %s", async (value) => {
    renderIntl(<ReferenceRetentionSettings />);
    fireEvent.change(await screen.findByLabelText("最多保留天数"), { target: { value } });
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect(setReferenceRetention).not.toHaveBeenCalled();
  });

  it("shows a safe load error and allows recovery", async () => {
    vi.mocked(getReferenceRetention).mockRejectedValueOnce(new Error("private")).mockResolvedValue({ days: 14, version: 1 });
    renderIntl(<ReferenceRetentionSettings />);
    await screen.findByText("无法读取设置，请重新加载。");
    expect(screen.queryByText("private")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新加载设置" }));
    await waitFor(() => expect((screen.getByLabelText("最多保留天数") as HTMLInputElement).value).toBe("14"));
  });
});
