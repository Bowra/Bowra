/**
 * One knowledge base's page (FEATURE_KNOWLEDGE_BASES, PRD §4.2 / §4.11, mockups ② ⑩ ⑫).
 *
 * Contract:
 *   1. An unknown or inaccessible ID (404 or 403) shows exactly the same not-found state, keeping the page title.
 *   2. Header actions follow the level: managers get ⋯ (Settings, Delete), Share and Add files; editors Who has access
 *      and Add files; people who can only use it Who has access and a notice naming whom to contact; My knowledge
 *      bases have neither Share nor Who has access.
 *   3. Tabs keep the tab in the address (replace, other parameters kept); Settings exists for managers only.
 *   4. File-only links switch to a listed Space, read the file and replace the address with its knowledge base (its
 *      `knowledge_base_id`, else the scope's default from the directory); an unreadable file shows not-found.
 *   5. Knowledge base writes anywhere (`KNOWLEDGE_BASES_CHANGED_EVENT`) refresh the page.
 */

import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { KbFileDTO } from "@/lib/kb-api";
import type { KnowledgeBaseDirectory, KnowledgeBaseDTO } from "@/lib/knowledge-bases-api";
import messagesZh from "@/messages/zh-CN.json";

const zhCommon = messagesZh.knowledge_bases.common;
const zhPage = messagesZh.knowledge_bases.page;

const mocks = vi.hoisted(() => ({
  getKnowledgeBase: vi.fn(),
  getKbFile: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  setActiveId: vi.fn(),
  session: { status: "authenticated", me: { id: "u_me", capabilities: { knowledge_bases: true } } } as unknown,
  space: { activeId: "ws_main" as string | null },
  directory: { directory: null as unknown, loading: false, error: null as Error | null },
  managerProps: [] as Array<Record<string, unknown>>,
  deleteDialog: { onDeleted: null as null | (() => void) },
}));

vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push, replace: mocks.replace, back: vi.fn() }),
}));

vi.mock("@/lib/auth/auth-session-context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/auth/auth-session-context")>();
  return { ...actual, useOptionalAuthSession: () => mocks.session };
});

vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  const React = await import("react");
  return {
    ...actual,
    useSpaceSelection: () => {
      const [activeId, setActive] = React.useState<string | null>(mocks.space.activeId);
      return {
        items: [
          { id: "ws_main", name: "商超零售场景", desc: "" },
          { id: "ws_other", name: "华东区", desc: "" },
        ],
        activeId,
        loading: false,
        error: null,
        setActiveId: (id: string) => {
          mocks.setActiveId(id);
          setActive(id);
        },
        refresh: vi.fn(),
      };
    },
  };
});

vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return { ...actual, getKnowledgeBase: (...args: unknown[]) => mocks.getKnowledgeBase(...args) };
});

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return { ...actual, getKbFile: (...args: unknown[]) => mocks.getKbFile(...args) };
});

vi.mock("./use-knowledge-bases", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./use-knowledge-bases")>();
  return {
    ...actual,
    useKnowledgeBaseDirectory: ({ enabled = true }: { enabled?: boolean } = {}) =>
      enabled
        ? { ...mocks.directory, refresh: vi.fn() }
        : { directory: null, loading: true, error: null, refresh: vi.fn() },
  };
});

vi.mock("@/components/knowledge/kb-files-manager", () => ({
  KbFilesManager: (props: Record<string, unknown>) => {
    mocks.managerProps.push(props);
    const kb = props.knowledgeBase as KnowledgeBaseDTO | undefined;
    return (
      <div
        data-testid="files-manager"
        data-kb={kb?.id ?? ""}
        data-file={String(props.initialFileId ?? "")}
        data-chunk={String(props.initialChunkId ?? "")}
        data-upload={String(props.uploadRequest ?? "")}
      />
    );
  },
}));
vi.mock("./kb-members-panel", () => ({
  KbMembersPanel: ({ kb }: { kb: KnowledgeBaseDTO }) => <div data-testid="members-panel" data-kb={kb.id} />,
}));
vi.mock("./kb-share-dialog", () => ({
  KbShareDialog: ({ kb }: { kb: KnowledgeBaseDTO }) => <div data-testid="share-dialog" data-kb={kb.id} />,
}));
vi.mock("./kb-delete-dialog", () => ({
  KbDeleteDialog: ({ kb, onDeleted }: { kb: KnowledgeBaseDTO; onDeleted: () => void }) => {
    mocks.deleteDialog.onDeleted = onDeleted;
    return <div data-testid="delete-dialog" data-kb={kb.id} />;
  },
}));
vi.mock("./kb-settings-panel", () => ({
  KbSettingsPanel: ({ kb }: { kb: KnowledgeBaseDTO }) => <div data-testid="settings-panel" data-kb={kb.id} />,
}));

import { KbPage, type KbPageProps } from "./kb-page";

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
    failed_count: 1,
    updated_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
    revision: 2,
    ...overrides,
  };
}

const EDIT = { level: "edit" as const, owner: { user_id: "u_owner", name: "王蕾" }, can: { add_files: true, manage: false, transfer: false, delete: false } };
const USE = { level: "use" as const, owner: { user_id: "u_owner", name: "王蕾" }, can: { add_files: false, manage: false, transfer: false, delete: false } };

function kbFile(overrides: Partial<KbFileDTO> = {}): KbFileDTO {
  return {
    file_id: "kbf_1",
    filename: "开店检查清单 2026.pdf",
    scope: "space",
    scope_id: "ws_main",
    workspace_id: "ws_main",
    knowledge_base_id: "kbase_ops",
    ...overrides,
  } as KbFileDTO;
}

function renderPage(props: Partial<KbPageProps> = {}) {
  const all: KbPageProps = {
    knowledgeBaseId: "kbase_ops",
    tab: "files",
    fileId: null,
    chunkId: null,
    spaceId: null,
    viewQuery: "id=kbase_ops",
    ...props,
  };
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh} timeZone="Asia/Shanghai">
      <KbPage {...all} />
    </NextIntlClientProvider>,
  );
}

function notFound(status: number) {
  return new ApiError(status, "not found", { code: status === 404 ? "knowledge_base_not_found" : "knowledge_base_level_required" });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.session = { status: "authenticated", me: { id: "u_me", capabilities: { knowledge_bases: true } } };
  mocks.space.activeId = "ws_main";
  mocks.directory = { directory: null, loading: false, error: null };
  mocks.managerProps = [];
  mocks.deleteDialog.onDeleted = null;
  mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase());
  window.history.replaceState(null, "", "/knowledge/base/?id=kbase_ops");
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

describe("KbPage · not found without disclosure", () => {
  it("answers an unknown and an inaccessible knowledge base with the same not-found state", async () => {
    mocks.getKnowledgeBase.mockRejectedValueOnce(notFound(404));
    const first = renderPage();
    const unknown = await screen.findByTestId("kb-page-not-found");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(messagesZh.knowledge_base.title);
    expect(unknown).toHaveTextContent(zhCommon.not_found_title);
    expect(unknown).toHaveTextContent(zhCommon.not_found_body);
    const unknownHtml = unknown.outerHTML;
    first.unmount();

    mocks.getKnowledgeBase.mockRejectedValueOnce(notFound(403));
    renderPage();
    const denied = await screen.findByTestId("kb-page-not-found");
    expect(denied.outerHTML).toBe(unknownHtml);
    fireEvent.click(within(denied).getByRole("button", { name: zhCommon.back_to_directory }));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/");
  });

  it("shows not-found without asking the server while the switch is off", async () => {
    mocks.session = { status: "authenticated", me: { id: "u_me", capabilities: { knowledge_bases: false } } };
    renderPage();
    expect(await screen.findByTestId("kb-page-not-found")).toBeInTheDocument();
    expect(mocks.getKnowledgeBase).not.toHaveBeenCalled();
  });

  it("offers a retry with an actionable message for other failures", async () => {
    mocks.getKnowledgeBase.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderPage();
    const error = await screen.findByTestId("kb-page-error");
    expect(error).toHaveTextContent(zhPage.load_failed_title);
    expect(error).toHaveTextContent(messagesZh.knowledge_base.errors.network);
    fireEvent.click(within(error).getByRole("button", { name: zhCommon.retry }));
    expect(await screen.findByTestId("kb-page")).toBeInTheDocument();
  });
});

describe("KbPage · header by level", () => {
  it("gives managers ⋯ (Settings, Delete), Share, Add files and the Settings tab", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "门店运营手册" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: zhPage.breadcrumb_label })).toHaveTextContent(`${zhCommon.breadcrumb_root}›商超零售场景`);
    const meta = screen.getByTestId("kb-page-meta");
    expect(meta).toHaveTextContent(zhCommon.audience_space_use);
    expect(meta).toHaveTextContent("你：所有者");
    expect(meta).toHaveTextContent("更新于");
    expect(screen.getByTestId("kb-page-share")).toHaveTextContent(zhPage.share);
    expect(screen.getByTestId("kb-page-add-files")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /文件\s*21/ })).toBeInTheDocument();
    expect(screen.getByTestId("kb-page-tab-settings")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-page-use-only")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "门店运营手册的更多操作" }));
    expect(await screen.findByRole("menuitem", { name: zhPage.menu_settings })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: `${zhPage.menu_delete}…` }));
    expect(await screen.findByTestId("delete-dialog")).toHaveAttribute("data-kb", "kbase_ops");
    act(() => mocks.deleteDialog.onDeleted?.());
    expect(mocks.push).toHaveBeenCalledWith("/knowledge/");

    fireEvent.click(screen.getByTestId("kb-page-share"));
    expect(await screen.findByTestId("share-dialog")).toBeInTheDocument();
  });

  it("gives editors Who has access and Add files, without ⋯ or Settings", async () => {
    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase(EDIT));
    renderPage();
    expect(await screen.findByTestId("kb-page-share")).toHaveTextContent(zhPage.who_has_access);
    expect(screen.getByTestId("kb-page-add-files")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "门店运营手册的更多操作" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-page-tab-settings")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-page-meta")).toHaveTextContent("你：可编辑");
  });

  it("gives people who can only use it Who has access and a notice naming the owner", async () => {
    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase(USE));
    renderPage();
    expect(await screen.findByTestId("kb-page-share")).toHaveTextContent(zhPage.who_has_access);
    expect(screen.queryByTestId("kb-page-add-files")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-page-use-only")).toHaveTextContent(
      "你可以在对话里使用、查看和下载这些文件。需要添加或修改时，请联系所有者王蕾。",
    );
    fireEvent.click(screen.getByTestId("kb-page-share"));
    expect(await screen.findByTestId("share-dialog")).toBeInTheDocument();
  });

  it("names the Team admins in the use-only notice of the Team default", async () => {
    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase({
      ...USE, id: "kbase_team", is_default: true, home: "team", home_id: "team_main", name: null, owner: null, audience: "team",
    }));
    renderPage({ knowledgeBaseId: "kbase_team" });
    expect(await screen.findByRole("heading", { level: 1, name: zhCommon.default_name_team })).toBeInTheDocument();
    expect(screen.getByTestId("kb-page-use-only")).toHaveTextContent("请联系团队管理员");
  });

  it("moves the badges and wrapped actions into the page body on phones", async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query === "(max-width: 767.98px)",
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
    })) as unknown as typeof window.matchMedia;
    try {
      const { container } = renderPage();
      await waitFor(() => expect(container.querySelector(".kbp-phone-head")).not.toBeNull());
      const actions = container.querySelector(".kbp-phone-actions") as HTMLElement;
      expect(within(actions).getByTestId("kb-page-share")).toBeInTheDocument();
      expect(within(actions).getByTestId("kb-page-add-files")).toBeInTheDocument();
      expect(container.querySelector(".kbp-header")).toBeNull();
      expect(screen.getByRole("heading", { level: 1, name: "门店运营手册" })).toBeInTheDocument();
    } finally {
      window.matchMedia = original;
    }
  });

  it("has neither Share nor Who has access on My knowledge bases", async () => {
    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase({
      id: "kbase_mine", home: "personal", home_id: "u_me", audience: "owner", can: { add_files: true, manage: true, transfer: false, delete: true },
    }));
    renderPage({ knowledgeBaseId: "kbase_mine" });
    expect(await screen.findByTestId("kb-page-add-files")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-page-share")).not.toBeInTheDocument();
    expect(screen.getByTestId("kb-page-meta")).toHaveTextContent(zhCommon.audience_owner);
    expect(screen.getByRole("navigation", { name: zhPage.breadcrumb_label })).toHaveTextContent(zhCommon.home_personal);
  });
});

describe("KbPage · tabs in the address", () => {
  it("switches tabs with replace and keeps the other parameters", async () => {
    window.history.replaceState(null, "", "/knowledge/base/?id=kbase_ops&q=%E9%99%88%E5%88%97&file_id=kbf_1");
    renderPage({ fileId: "kbf_1" });
    expect(await screen.findByTestId("files-manager")).toHaveAttribute("data-file", "kbf_1");
    fireEvent.click(screen.getByTestId("kb-page-tab-members"));
    expect(await screen.findByTestId("members-panel")).toHaveAttribute("data-kb", "kbase_ops");
    const url = String(mocks.replace.mock.calls.at(-1)?.[0]);
    const params = new URLSearchParams(url.split("?")[1]);
    expect(url.startsWith("/knowledge/base/?")).toBe(true);
    expect(params.get("id")).toBe("kbase_ops");
    expect(params.get("q")).toBe("陈列");
    expect(params.get("tab")).toBe("members");
    // File details live in the Files tab only.
    expect(params.has("file_id")).toBe(false);

    window.history.replaceState(null, "", url);
    fireEvent.click(screen.getByTestId("kb-page-tab-files"));
    const back = new URLSearchParams(String(mocks.replace.mock.calls.at(-1)?.[0]).split("?")[1]);
    expect(back.has("tab")).toBe(false);
    expect(back.get("id")).toBe("kbase_ops");
    expect(await screen.findByTestId("files-manager")).toHaveAttribute("data-kb", "kbase_ops");
  });

  it("opens the tab named in the address, and Files instead of Settings for non-managers", async () => {
    const managed = renderPage({ tab: "settings" });
    expect(await screen.findByTestId("settings-panel")).toBeInTheDocument();
    managed.unmount();

    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase(EDIT));
    renderPage({ tab: "settings" });
    expect(await screen.findByTestId("files-manager")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-panel")).not.toBeInTheDocument();
  });

  it("opens the upload in the Files tab from the header Add files", async () => {
    renderPage({ tab: "members" });
    await screen.findByTestId("members-panel");
    fireEvent.click(screen.getByTestId("kb-page-add-files"));
    expect(await screen.findByTestId("files-manager")).toHaveAttribute("data-upload", "1");
    expect(new URLSearchParams(String(mocks.replace.mock.calls.at(-1)?.[0]).split("?")[1]).has("tab")).toBe(false);
  });

  it("refreshes after any knowledge base write", async () => {
    renderPage();
    await screen.findByRole("tab", { name: /文件\s*21/ });
    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase({ file_count: 22 }));
    act(() => { window.dispatchEvent(new Event("bowhead:knowledge-bases-changed")); });
    expect(await screen.findByRole("tab", { name: /文件\s*22/ })).toBeInTheDocument();
    expect(mocks.getKnowledgeBase).toHaveBeenCalledTimes(2);
  });
});

describe("KbPage · file-only links", () => {
  it("replaces the address with the file's knowledge base and keeps the file and chunk", async () => {
    mocks.getKbFile.mockResolvedValue(kbFile());
    renderPage({ knowledgeBaseId: null, fileId: "kbf_1", chunkId: "c_7", viewQuery: "file_id=kbf_1&chunk_id=c_7" });
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/base/?id=kbase_ops&file_id=kbf_1&chunk_id=c_7", { scroll: false }));
    expect(mocks.getKbFile).toHaveBeenCalledWith("kbf_1");
    expect(mocks.getKnowledgeBase).not.toHaveBeenCalled();
  });

  it("uses the scope's default knowledge base when the file names none", async () => {
    mocks.getKbFile.mockResolvedValue(kbFile({ knowledge_base_id: null, scope: "team", scope_id: "team_main" }));
    const teamDefault = knowledgeBase({ ...USE, id: "kbase_team_default", is_default: true, home: "team", home_id: "team_main", name: null, owner: null });
    const directory: KnowledgeBaseDirectory = {
      space_id: "ws_main",
      sections: { personal: [], space: [], team: [teamDefault] },
      can_create: { personal: true, space: true, team: false },
    };
    mocks.directory = { directory, loading: false, error: null };
    renderPage({ knowledgeBaseId: null, fileId: "kbf_1", viewQuery: "file_id=kbf_1" });
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/base/?id=kbase_team_default&file_id=kbf_1", { scroll: false }));
  });

  it("switches to a listed Space before reading the file", async () => {
    mocks.getKbFile.mockImplementation(async () => {
      expect(mocks.setActiveId).toHaveBeenCalledWith("ws_other");
      return kbFile({ scope_id: "ws_other", workspace_id: "ws_other", knowledge_base_id: "kbase_east" });
    });
    renderPage({ knowledgeBaseId: null, fileId: "kbf_1", spaceId: "ws_other", viewQuery: "file_id=kbf_1&space=ws_other" });
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/base/?id=kbase_east&file_id=kbf_1", { scroll: false }));
    expect(mocks.getKbFile).toHaveBeenCalledTimes(1);
  });

  it("ignores a Space that is not listed and reads the file in the current Space", async () => {
    mocks.getKbFile.mockResolvedValue(kbFile());
    renderPage({ knowledgeBaseId: null, fileId: "kbf_1", spaceId: "ws_unknown", viewQuery: "file_id=kbf_1&space=ws_unknown" });
    await waitFor(() => expect(mocks.replace).toHaveBeenCalled());
    expect(mocks.setActiveId).not.toHaveBeenCalled();
  });

  it("shows the not-found state when the file cannot be read", async () => {
    mocks.getKbFile.mockRejectedValue(new ApiError(404, "gone", { code: "kb_file_not_found" }));
    renderPage({ knowledgeBaseId: null, fileId: "kbf_gone", viewQuery: "file_id=kbf_gone" });
    expect(await screen.findByTestId("kb-page-not-found")).toBeInTheDocument();
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("sends file-only links to today's Knowledge page while the switch is off", async () => {
    mocks.session = { status: "authenticated", me: { id: "u_me", capabilities: { knowledge_bases: false } } };
    renderPage({ knowledgeBaseId: null, fileId: "kbf_1", chunkId: "c_7", spaceId: "ws_main", viewQuery: "" });
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/knowledge/?file_id=kbf_1&chunk_id=c_7&space=ws_main", { scroll: false }));
    expect(mocks.getKbFile).not.toHaveBeenCalled();
  });
});
