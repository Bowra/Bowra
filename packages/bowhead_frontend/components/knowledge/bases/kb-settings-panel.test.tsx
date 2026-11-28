/**
 * The Settings tab of one knowledge base (FEATURE_KNOWLEDGE_BASES, PRD §4.2, technical design §3.1 C6 / Transfer).
 *
 * Contract:
 *   1. Saving sends only the changed fields with the revision the form was read from; a revision conflict explains
 *      itself and offers a reload; other failures show the mapped, actionable message.
 *   2. Default knowledge bases show their fixed name with the reason instead of inputs, and no transfer or delete.
 *   3. Transfer is offered only with `can.transfer`; candidates are people who already manage it (automatic managers
 *      and listed people at Can manage), never the owner, groups, Spaces or lower levels. My knowledge bases have none.
 *   4. The danger zone deletes only with `can.delete` and names the file count.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { KnowledgeBaseDTO, KnowledgeBaseMembers } from "@/lib/knowledge-bases-api";
import messagesZh from "@/messages/zh-CN.json";

const zhSettings = messagesZh.knowledge_bases.settings;
const zhCommon = messagesZh.knowledge_bases.common;

const mocks = vi.hoisted(() => ({
  update: vi.fn(),
  transfer: vi.fn(),
  members: vi.fn(),
  notifyChanged: vi.fn(),
  notify: vi.fn(),
}));

vi.mock("@/lib/auth/auth-session-context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/auth/auth-session-context")>();
  return {
    ...actual,
    useOptionalAuthSession: () => ({ status: "authenticated", me: { id: "u_me", capabilities: { knowledge_bases: true } } }),
  };
});

vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [{ id: "ws_main", name: "商超零售场景", desc: "" }],
      activeId: "ws_main",
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    }),
  };
});

vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return {
    ...actual,
    updateKnowledgeBase: (...args: unknown[]) => mocks.update(...args),
    transferKnowledgeBase: (...args: unknown[]) => mocks.transfer(...args),
    getKnowledgeBaseMembers: (...args: unknown[]) => mocks.members(...args),
    notifyKnowledgeBasesChanged: () => mocks.notifyChanged(),
  };
});

vi.mock("@/lib/use-toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-toast")>();
  return { ...actual, notify: (...args: unknown[]) => mocks.notify(...args) };
});

import { KbSettingsPanel, transferCandidates } from "./kb-settings-panel";

function knowledgeBase(overrides: Partial<KnowledgeBaseDTO> = {}): KnowledgeBaseDTO {
  return {
    id: "kbase_ops",
    is_default: false,
    home: "space",
    home_id: "ws_main",
    name: "门店运营手册",
    description: "开店、巡检、陈列标准。",
    audience: "space",
    audience_level: "use",
    level: "manage",
    owner: { user_id: "u_me", name: "陈思远" },
    can: { add_files: true, manage: true, transfer: true, delete: true },
    file_count: 21,
    failed_count: 0,
    updated_at: "2026-11-24T05:10:00Z",
    revision: 4,
    ...overrides,
  };
}

function members(overrides: Partial<KnowledgeBaseMembers> = {}): KnowledgeBaseMembers {
  return {
    knowledge_base_id: "kbase_ops",
    audience: "space",
    audience_level: "use",
    owner: { user_id: "u_me", name: "陈思远" },
    automatic_managers: [
      { user_id: "u_admin", name: "周明", reason: "space_admin" },
      { user_id: "u_me", name: "陈思远", reason: "space_admin" },
    ],
    entries: [
      { principal_type: "user", principal_id: "u_lin", label: "林雨桐", level: "manage", state: "active", added_by: "u_me", created_at: "" },
      { principal_type: "user", principal_id: "u_wang", label: "王蕾", level: "edit", state: "active", added_by: "u_me", created_at: "" },
      { principal_type: "user", principal_id: "u_gone", label: "离开的人", level: "manage", state: "not_in_space", added_by: "u_me", created_at: "" },
      { principal_type: "group", principal_id: "g_ops", label: "运营组", level: "manage", state: "active", added_by: "u_me", created_at: "" },
      { principal_type: "user", principal_id: "u_admin", label: "周明", level: "manage", state: "active", added_by: "u_me", created_at: "" },
    ],
    summary: { use: 3, edit: 1, manage: 3, people: 7 },
    can_manage: true,
    ...overrides,
  };
}

const handlers = {
  onUpdated: vi.fn(),
  onReload: vi.fn(),
  onRequestDelete: vi.fn(),
  onOpenMembers: vi.fn(),
};

function renderPanel(kb: KnowledgeBaseDTO) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbSettingsPanel kb={kb} {...handlers} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.members.mockResolvedValue(members());
});

afterEach(cleanup);

describe("KbSettingsPanel · name and description", () => {
  it("saves only the changed fields with the revision the form was read from", async () => {
    const saved = knowledgeBase({ name: "门店运营手册 2026", revision: 5 });
    mocks.update.mockResolvedValue(saved);
    renderPanel(knowledgeBase());
    const save = screen.getByTestId("kb-settings-save");
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText(zhSettings.name_label), { target: { value: "  门店运营手册 2026 " } });
    fireEvent.click(save);
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith("kbase_ops", { revision: 4, name: "门店运营手册 2026" }));
    await waitFor(() => expect(handlers.onUpdated).toHaveBeenCalledWith(saved));
    expect(mocks.notifyChanged).toHaveBeenCalled();
    expect(mocks.notify).toHaveBeenCalledWith("success", zhSettings.saved);
  });

  it("clears the description with null", async () => {
    mocks.update.mockResolvedValue(knowledgeBase({ description: null, revision: 5 }));
    renderPanel(knowledgeBase());
    fireEvent.change(screen.getByLabelText(zhSettings.description_label), { target: { value: "  " } });
    fireEvent.click(screen.getByTestId("kb-settings-save"));
    await waitFor(() => expect(mocks.update).toHaveBeenCalledWith("kbase_ops", { revision: 4, description: null }));
  });

  it("asks for a name instead of sending an empty one", async () => {
    renderPanel(knowledgeBase());
    fireEvent.change(screen.getByLabelText(zhSettings.name_label), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("kb-settings-save"));
    expect(await screen.findByText(zhSettings.name_required)).toBeInTheDocument();
    expect(mocks.update).not.toHaveBeenCalled();
  });

  it("explains a revision conflict and offers a reload that resets the form", async () => {
    mocks.update.mockRejectedValue(new ApiError(409, "conflict", { code: "knowledge_base_revision_conflict" }));
    const view = renderPanel(knowledgeBase());
    fireEvent.change(screen.getByLabelText(zhSettings.name_label), { target: { value: "新名字" } });
    fireEvent.click(screen.getByTestId("kb-settings-save"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(messagesZh.knowledge_base.errors.knowledge_base_revision_conflict);
    fireEvent.click(within(alert).getByTestId("kb-settings-reload"));
    expect(handlers.onReload).toHaveBeenCalled();
    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <KbSettingsPanel kb={knowledgeBase({ name: "别人改的名字", revision: 6 })} {...handlers} />
      </NextIntlClientProvider>,
    );
    await waitFor(() => expect(screen.getByLabelText(zhSettings.name_label)).toHaveValue("别人改的名字"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows other failures with their mapped message, never a raw server text", async () => {
    mocks.update.mockRejectedValue(new ApiError(422, "raw backend detail", { code: "knowledge_base_name_required" }));
    renderPanel(knowledgeBase());
    fireEvent.change(screen.getByLabelText(zhSettings.description_label), { target: { value: "新的说明" } });
    fireEvent.click(screen.getByTestId("kb-settings-save"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(messagesZh.knowledge_base.errors.knowledge_base_name_required);
    expect(alert).not.toHaveTextContent("raw backend detail");
    expect(screen.queryByTestId("kb-settings-reload")).not.toBeInTheDocument();
  });

  it("shows a default's fixed name with the reason, and no inputs, transfer or delete", () => {
    renderPanel(knowledgeBase({
      id: "kbase_space_default", is_default: true, name: null, description: null, owner: null,
      can: { add_files: true, manage: true, transfer: false, delete: false },
    }));
    const fixed = screen.getByTestId("kb-settings-fixed");
    expect(fixed).toHaveTextContent("商超零售场景");
    expect(fixed).toHaveTextContent(zhSettings.default_fixed_reason);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-settings-owner-default")).toHaveTextContent("由空间管理员管理");
    expect(screen.queryByTestId("kb-settings-transfer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-settings-delete")).not.toBeInTheDocument();
    expect(screen.getByText(zhSettings.delete_default)).toBeInTheDocument();
  });
});

describe("KbSettingsPanel · owner and transfer", () => {
  it("lists only people who already manage it, other than the owner", () => {
    expect(transferCandidates(members(), "u_me")).toEqual([
      { user_id: "u_admin", name: "周明", reason: "space_admin" },
      { user_id: "u_lin", name: "林雨桐", reason: "member" },
    ]);
  });

  it("transfers to the chosen manager with the revision", async () => {
    const next = knowledgeBase({ owner: { user_id: "u_lin", name: "林雨桐" }, revision: 5, can: { add_files: true, manage: true, transfer: false, delete: false } });
    mocks.transfer.mockResolvedValue(next);
    renderPanel(knowledgeBase());
    expect(screen.getByTestId("kb-settings-owner-name")).toHaveTextContent("陈思远（你）");
    fireEvent.click(screen.getByTestId("kb-settings-transfer"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(zhSettings.transfer_rule);
    expect(dialog).toHaveTextContent(zhSettings.transfer_keep);
    const options = await within(dialog).findAllByRole("radio");
    expect(options.map((option) => option.getAttribute("aria-label"))).toEqual(["周明", "林雨桐"]);
    const confirm = within(dialog).getByTestId("kb-transfer-confirm");
    expect(confirm).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("radio", { name: "林雨桐" }));
    fireEvent.click(confirm);
    await waitFor(() => expect(mocks.transfer).toHaveBeenCalledWith("kbase_ops", { user_id: "u_lin", revision: 4 }));
    await waitFor(() => expect(handlers.onUpdated).toHaveBeenCalledWith(next));
    expect(mocks.notify).toHaveBeenCalledWith("success", "已把所有权转给林雨桐");
  });

  it("points to Members when nobody else manages it yet", async () => {
    mocks.members.mockResolvedValue(members({ automatic_managers: [], entries: [] }));
    renderPanel(knowledgeBase());
    fireEvent.click(screen.getByTestId("kb-settings-transfer"));
    const empty = await screen.findByTestId("kb-transfer-empty");
    fireEvent.click(within(empty).getByRole("button", { name: zhSettings.open_members }));
    expect(handlers.onOpenMembers).toHaveBeenCalled();
  });

  it("shows the transfer refusal with its actionable message", async () => {
    mocks.transfer.mockRejectedValue(new ApiError(422, "x", { code: "knowledge_base_transfer_target_invalid" }));
    renderPanel(knowledgeBase());
    fireEvent.click(screen.getByTestId("kb-settings-transfer"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(await within(dialog).findByRole("radio", { name: "林雨桐" }));
    fireEvent.click(within(dialog).getByTestId("kb-transfer-confirm"));
    expect(await within(dialog).findByText(messagesZh.knowledge_base.errors.knowledge_base_transfer_target_invalid)).toBeInTheDocument();
    expect(handlers.onUpdated).not.toHaveBeenCalled();
  });

  it("explains why a manager who is not the owner cannot transfer or delete", () => {
    renderPanel(knowledgeBase({ owner: { user_id: "u_owner", name: "王蕾" }, can: { add_files: true, manage: true, transfer: false, delete: false } }));
    expect(screen.getByTestId("kb-settings-owner-name")).toHaveTextContent("王蕾");
    expect(screen.queryByTestId("kb-settings-transfer")).not.toBeInTheDocument();
    expect(screen.getByText(zhSettings.transfer_not_allowed)).toBeInTheDocument();
    expect(screen.queryByTestId("kb-settings-delete")).not.toBeInTheDocument();
    expect(screen.getByText(zhSettings.delete_not_allowed)).toBeInTheDocument();
  });

  it("has no transfer on My knowledge bases", () => {
    renderPanel(knowledgeBase({ home: "personal", home_id: "u_me", audience: "owner", can: { add_files: true, manage: true, transfer: false, delete: true } }));
    expect(screen.getByTestId("kb-settings-owner-personal")).toHaveTextContent(zhSettings.owner_personal);
    expect(screen.queryByTestId("kb-settings-transfer")).not.toBeInTheDocument();
  });
});

describe("KbSettingsPanel · danger zone", () => {
  it("names the file count and asks the page to confirm the deletion", () => {
    renderPanel(knowledgeBase());
    expect(screen.getByText("里面的 21 个文件会移出知识库，原文件仍保留在「文件」中。")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("kb-settings-delete"));
    expect(handlers.onRequestDelete).toHaveBeenCalled();
    expect(screen.getByTestId("kb-settings-delete")).toHaveTextContent(zhSettings.delete_label);
    // The shared Save label is the form's action.
    expect(screen.getByTestId("kb-settings-save")).toHaveTextContent(zhCommon.save);
  });
});
