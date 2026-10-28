import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import type { PublicEntryDTO } from "@/lib/api";

const state = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchEntries: vi.fn(),
  createEntry: vi.fn(),
  reissueChallenge: vi.fn(),
  setDefault: vi.fn(),
  disableEntry: vi.fn(),
  deleteEntry: vi.fn(),
}));

vi.mock("@/lib/auth-api", () => ({
  fetchMe: () => state.fetchMe(),
}));

vi.mock("@/lib/api", () => ({
  fetchPublicEntries: () => state.fetchEntries(),
  createPublicEntry: (baseUrl: string) => state.createEntry(baseUrl),
  reissuePublicEntryChallenge: (entryId: string) =>
    state.reissueChallenge(entryId),
  setDefaultPublicEntry: (entryId: string) => state.setDefault(entryId),
  disablePublicEntry: (entryId: string, options: unknown) =>
    state.disableEntry(entryId, options),
  deletePublicEntry: (entryId: string) => state.deleteEntry(entryId),
}));

import { SettingsPublicEntries } from "./public-entries";

function makeEntry(
  id: string,
  overrides: Partial<PublicEntryDTO> = {},
): PublicEntryDTO {
  const baseUrl = overrides.base_url ?? `https://${id}.example.com/bowhead`;
  return {
    id,
    team_id: "team_a",
    base_url: baseUrl,
    base_path: "/bowhead",
    is_default: false,
    is_current: false,
    match_source: null,
    status: "active",
    verification_method: "trusted_proxy_bootstrap",
    verification_expires_at: null,
    verified_at: "2026-07-28T00:00:00Z",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    ...overrides,
  };
}

function arrange(entries: PublicEntryDTO[]) {
  state.fetchMe.mockResolvedValue({
    team_role: "owner",
    is_team_admin: true,
  });
  state.fetchEntries.mockResolvedValue(entries);
  state.disableEntry.mockImplementation(async (entryId: string) => ({
    ...entries.find((entry) => entry.id === entryId)!,
    is_default: false,
    status: "disabled",
  }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SettingsPublicEntries", () => {
  it("blocks malformed public URLs before submit and keeps HTTP risk visible", async () => {
    arrange([
      makeEntry("pe_current", {
        base_url: "http://192.0.2.10",
        base_path: "",
        is_default: true,
        is_current: true,
        match_source: "cookie",
      }),
    ]);
    renderIntl(<SettingsPublicEntries />);

    expect(await screen.findByText("当前请求入口")).toBeInTheDocument();
    expect(screen.getAllByText("http://192.0.2.10")).toHaveLength(2);
    expect(screen.getByText(/此入口使用 HTTP，传输未加密/)).toBeInTheDocument();

    const input = screen.getByLabelText("公开入口地址");
    const submit = screen.getByRole("button", { name: "添加入口" });
    expect(screen.getByTestId("public-entry-add-controls")).toContainElement(input);
    expect(screen.getByTestId("public-entry-add-controls")).toContainElement(submit);
    fireEvent.change(input, { target: { value: "not-a-url" } });
    expect(submit).toBeDisabled();
    expect(
      screen.getByText(/请输入完整的 HTTP 或 HTTPS 地址/),
    ).toBeInTheDocument();

    fireEvent.change(input, {
      target: { value: "https://public.example.com/bowhead" },
    });
    expect(submit).toBeEnabled();
  });

  it("refreshes a verification result after the one-time link is opened", async () => {
    const pending = makeEntry("pe_intranet", {
      base_url: "http://10.10.0.5:10010",
      base_path: "",
      status: "pending_verification",
      verification_expires_at: "2026-07-30T10:15:00Z",
    });
    const active = { ...pending, status: "active" as const, is_default: true };
    arrange([pending]);
    state.fetchEntries.mockResolvedValueOnce([pending]).mockResolvedValueOnce([active]);
    state.reissueChallenge.mockResolvedValue({
      entry: pending,
      expires_at: pending.verification_expires_at,
      verification_url:
        "http://10.10.0.5:10010/.well-known/bowhead-public-entry/pe_intranet#token",
    });
    renderIntl(<SettingsPublicEntries />);

    await chooseRowAction("http://10.10.0.5:10010 的更多操作", "重新签发验证链接");
    expect(await screen.findByText("验证链接")).toBeInTheDocument();
    expect(screen.getByText("验证完成后返回本页会自动刷新，也可手动刷新状态。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "刷新验证状态" }));
    await waitFor(() => {
      expect(state.fetchEntries).toHaveBeenCalledTimes(2);
      expect(screen.getByText("已启用")).toBeInTheDocument();
    });
    expect(screen.queryByText("验证链接")).not.toBeInTheDocument();
  });

  it("requires an explicit replacement before disabling a default entry", async () => {
    const current = makeEntry("pe_current", {
      is_default: true,
      is_current: true,
    });
    const replacementA = makeEntry("pe_replacement_a");
    const replacementB = makeEntry("pe_replacement_b");
    arrange([current, replacementA, replacementB]);
    renderIntl(<SettingsPublicEntries />);

    await chooseRowAction(`${current.base_url} 的更多操作`, "停用…", { danger: true });

    const dialog = await screen.findByRole("dialog", { name: "停用公开入口" });
    const confirm = within(dialog).getByRole("button", { name: "确认停用" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("新的默认入口"), {
      target: { value: replacementB.id },
    });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(state.disableEntry).toHaveBeenCalledWith(current.id, {
        new_default_entry_id: replacementB.id,
        disable_last: false,
      });
    });
  });

  // A deployment can be served at a different host from its registered entry.
  // The page must show the actual address when no entry matches, so
  // the one-character difference was invisible and every public page stayed
  // unbranded. See docs/sprints/brand-appearance-repair-2026-09-09/spike.md.
  it("shows the browser address when no entry matches, even if the injected origin is internal", async () => {
    const served = `${window.location.origin}/bowhead`;
    const previous = window.__BOWHEAD_PUBLIC_CONTEXT__;
    window.__BOWHEAD_PUBLIC_CONTEXT__ = {
      entryId: null,
      teamId: null,
      baseUrl: "http://internal.example:8000/bowhead",
      basePath: "/bowhead",
      matchSource: "browser_hint",
    };
    arrange([
      makeEntry("pe_registered", {
        base_url: "https://other.customer.example/bowhead",
        is_default: true,
        is_current: false,
      }),
    ]);
    renderIntl(<SettingsPublicEntries />);

    expect(
      await screen.findByText(/当前请求没有匹配到有效公开入口/),
    ).toBeInTheDocument();
    // Positive control: before the fix, only the sentence above appeared, with none of the address.
    expect(screen.getByText(served)).toBeInTheDocument();

    window.__BOWHEAD_PUBLIC_CONTEXT__ = previous;
  });
});

describe("SettingsPublicEntries 卡片「⋯」（外观段第四批）", () => {
  const ACTIVE = "https://a.example.com/bowhead";
  const DEFAULT = "https://d.example.com/bowhead";
  const PENDING = "https://p.example.com/bowhead";
  const entries = () => [
    makeEntry("pe_default", { base_url: DEFAULT, is_default: true }),
    makeEntry("pe_active", { base_url: ACTIVE }),
    makeEntry("pe_pending", { base_url: PENDING, status: "pending_verification", verified_at: null }),
  ];

  it("keeps actions out of the card and orders the menu with danger items last", async () => {
    arrange(entries());
    renderIntl(<SettingsPublicEntries />);
    await screen.findByText(ACTIVE);
    for (const name of ["停用", "删除", "设为默认", "重新签发验证链接"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(menuItemNames(await openRowMenu(`${ACTIVE} 的更多操作`))).toEqual(["设为默认", "停用…"]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(menuItemNames(await openRowMenu(`${DEFAULT} 的更多操作`))).toEqual(["停用…"]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(menuItemNames(await openRowMenu(`${PENDING} 的更多操作`))).toEqual(["重新签发验证链接", "删除…"]);
  });

  it("calls the matching endpoints for Make default and Reissue", async () => {
    arrange(entries());
    state.setDefault.mockResolvedValue(makeEntry("pe_active", { base_url: ACTIVE, is_default: true }));
    state.reissueChallenge.mockResolvedValue({ entry: makeEntry("pe_pending", { base_url: PENDING, status: "pending_verification" }), verification_url: "https://p.example.com/verify", expires_at: null });
    renderIntl(<SettingsPublicEntries />);
    await screen.findByText(ACTIVE);
    await chooseRowAction(`${ACTIVE} 的更多操作`, "设为默认", { danger: false });
    await waitFor(() => expect(state.setDefault).toHaveBeenCalledWith("pe_active"));
    await chooseRowAction(`${PENDING} 的更多操作`, "重新签发验证链接", { danger: false });
    await waitFor(() => expect(state.reissueChallenge).toHaveBeenCalledWith("pe_pending"));
    expect(state.disableEntry).not.toHaveBeenCalled();
    expect(state.deleteEntry).not.toHaveBeenCalled();
  });

  it("confirms before disabling; cancel keeps the entry active", async () => {
    arrange(entries());
    renderIntl(<SettingsPublicEntries />);
    await screen.findByText(ACTIVE);
    await chooseRowAction(`${ACTIVE} 的更多操作`, "停用…", { danger: true });
    await expectConfirmGate({
      action: state.disableEntry,
      dialogName: "停用公开入口",
      confirmName: "确认停用",
      reopen: () => chooseRowAction(`${ACTIVE} 的更多操作`, "停用…"),
    });
    await waitFor(() => expect(state.disableEntry).toHaveBeenCalledWith("pe_active", expect.anything()));
  });

  it("confirms before deleting; cancel keeps the entry", async () => {
    arrange(entries());
    state.deleteEntry.mockResolvedValue(undefined);
    renderIntl(<SettingsPublicEntries />);
    await screen.findByText(PENDING);
    await chooseRowAction(`${PENDING} 的更多操作`, "删除…", { danger: true });
    await expectConfirmGate({
      action: state.deleteEntry,
      dialogName: "删除公开入口",
      confirmName: "确认删除",
      reopen: () => chooseRowAction(`${PENDING} 的更多操作`, "删除…"),
    });
    await waitFor(() => expect(state.deleteEntry).toHaveBeenCalledWith("pe_pending"));
  });
});
