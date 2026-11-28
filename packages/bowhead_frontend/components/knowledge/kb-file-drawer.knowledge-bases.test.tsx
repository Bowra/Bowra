// Knowledge bases (FEATURE_KNOWLEDGE_BASES): file details rows "所属知识库" / "谁能访问" and "移到其他知识库…" (PRD §4.5,
// mockup ⑥), plus M7 (chunk text without HTML tags or Markdown code fences). Switch off: today's drawer.
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KbFileDTO } from "@/lib/kb-api";
import messagesZh from "@/messages/zh-CN.json";

import { SPACES_FIXTURE, directoryFixture, knowledgeBase } from "./bases/kb-target-picker.fixtures";

const mocks = vi.hoisted(() => ({
  knowledgeBases: true,
  directory: null as ReturnType<typeof directoryFixture> | null,
  members: vi.fn(),
  getKnowledgeBase: vi.fn(),
  chunks: [] as Array<{ chunk_id: string; content: string; page: number | null; section_title: string | null; chunk_index: number }>,
}));

vi.mock("@/lib/kb-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/kb-api")>()),
  fetchKbFileChunks: vi.fn(() => Promise.resolve({ chunks: mocks.chunks })),
  downloadKbFileBlob: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("mammoth", () => ({ convertToHtml: () => Promise.reject(new Error("not a DOCX archive")) }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { knowledge_bases: mocks.knowledgeBases } } }),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES_FIXTURE, activeId: "ws_1", loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));
vi.mock("./bases/use-knowledge-bases", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./bases/use-knowledge-bases")>()),
  useKnowledgeBaseDirectory: () => ({ directory: mocks.directory, loading: false, error: null, refresh: vi.fn() }),
}));
vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/knowledge-bases-api")>()),
  getKnowledgeBaseMembers: mocks.members,
  getKnowledgeBase: mocks.getKnowledgeBase,
}));

import { KbFileDrawer } from "./kb-file-drawer";

function file(overrides: Partial<KbFileDTO> = {}): KbFileDTO {
  return {
    file_id: "kbf_1",
    artifact_id: "art_1",
    filename: "开店检查清单 2026.pdf",
    mime_type: "application/pdf",
    size_bytes: 2_100_000,
    scope: "space",
    scope_id: "ws_1",
    workspace_id: "ws_1",
    team_id: "team_1",
    uploaded_by: "u_2",
    uploaded_at: "2026-11-24T06:20:00Z",
    parse_status: "done",
    parse_progress_pct: null,
    parse_layer: "L1",
    parse_error: null,
    parse_duration_ms: null,
    chunks_count: 18,
    cite_count: 9,
    last_cited_at: null,
    use_llm_marker: false,
    doc_type_id: null,
    summary_json: null,
    version: 1,
    soft_deleted_at: null,
    can_delete: true,
    can_change_scope: true,
    knowledge_base_id: "kb_ops",
    ...overrides,
  } as KbFileDTO;
}

function renderDrawer(f: KbFileDTO, props: { onMove?: () => void; onChangeScope?: () => void } = {}) {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <KbFileDrawer file={f} onClose={vi.fn()} onDeleted={() => undefined} {...props} />
    </NextIntlClientProvider>,
  );
}

function summary(people: number, edit: number) {
  return { summary: { use: people - edit, edit, manage: 0, people } };
}

beforeEach(() => {
  mocks.knowledgeBases = true;
  mocks.directory = directoryFixture();
  mocks.members.mockReset();
  mocks.members.mockResolvedValue(summary(12, 3));
  mocks.getKnowledgeBase.mockReset();
  mocks.chunks = [];
});
afterEach(cleanup);

describe("KbFileDrawer · knowledge bases on", () => {
  it("shows the knowledge base as a link and who has access, with View members", async () => {
    renderDrawer(file());
    expect(screen.getByText("所属知识库")).toBeInTheDocument();
    expect(screen.queryByText(messagesZh.knowledge_base.drawer.meta_scope)).toBeNull();
    expect(screen.getByRole("link", { name: "门店运营手册" })).toHaveAttribute("href", "/knowledge/base/?id=kb_ops");
    const access = screen.getByTestId("kb-drawer-access");
    expect(access).toHaveTextContent("空间成员可使用");
    await waitFor(() => expect(access).toHaveTextContent("空间成员可使用 · 12 人可使用，3 人可编辑"));
    expect(mocks.members).toHaveBeenCalledWith("kb_ops");
    expect(screen.getByRole("link", { name: "查看成员" })).toHaveAttribute("href", "/knowledge/base/?id=kb_ops&tab=members");
  });

  it("resolves a file without a named knowledge base to its scope's default", async () => {
    mocks.members.mockResolvedValue(summary(8, 8));
    renderDrawer(file({ knowledge_base_id: null }));
    expect(screen.getByRole("link", { name: "商超零售场景" })).toHaveAttribute("href", "/knowledge/base/?id=kb_space");
    await waitFor(() => expect(screen.getByTestId("kb-drawer-access")).toHaveTextContent("空间成员可编辑 · 8 人可使用"));
    expect(screen.getByTestId("kb-drawer-access")).not.toHaveTextContent("可编辑，");
  });

  it("My knowledge is only yours: no member lookup and no View members", () => {
    renderDrawer(file({ scope: "personal", scope_id: "u_1", knowledge_base_id: null }));
    expect(screen.getByRole("link", { name: "我的知识" })).toBeInTheDocument();
    expect(screen.getByTestId("kb-drawer-access")).toHaveTextContent("仅自己");
    expect(screen.queryByRole("link", { name: "查看成员" })).toBeNull();
    expect(mocks.members).not.toHaveBeenCalled();
  });

  it("reads a named knowledge base outside this Space's directory by its ID", async () => {
    mocks.getKnowledgeBase.mockResolvedValue(knowledgeBase({ id: "kb_other", home_id: "ws_2", name: "华东门店资料" }));
    renderDrawer(file({ scope_id: "ws_2", knowledge_base_id: "kb_other" }));
    expect(await screen.findByRole("link", { name: "华东门店资料" })).toHaveAttribute("href", "/knowledge/base/?id=kb_other");
    expect(mocks.getKnowledgeBase).toHaveBeenCalledWith("kb_other");
  });

  it("offers 移到其他知识库… instead of 更改可见范围", () => {
    const onMove = vi.fn();
    const onChangeScope = vi.fn();
    renderDrawer(file(), { onMove, onChangeScope });
    expect(screen.queryByRole("button", { name: messagesZh.knowledge_base.drawer.change_scope })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "移到其他知识库…" }));
    expect(onMove).toHaveBeenCalledOnce();
    expect(onChangeScope).not.toHaveBeenCalled();
  });

  it("keeps the move entry disabled with the reason when the file may not be moved", () => {
    renderDrawer(file({ can_change_scope: false }), { onMove: vi.fn() });
    const move = screen.getByTestId("kb-drawer-move");
    expect(move).toBeDisabled();
    expect(move).toHaveAccessibleDescription(messagesZh.knowledge_bases.drawer.move_reason);
  });

  it("shows chunk text without HTML tags or Markdown code fences (M7)", async () => {
    mocks.chunks = [
      { chunk_id: "c1", content: "<p>开店前 7 天：完成货架安装。</p>", page: 1, section_title: null, chunk_index: 1 },
      { chunk_id: "c2", content: "```markdown\n# 开业前 1 天\n生鲜区补货\n```", page: 2, section_title: null, chunk_index: 2 },
    ];
    renderDrawer(file());
    const cards = await screen.findAllByTestId("kb-chunk-card");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent("开店前 7 天：完成货架安装。");
    expect(cards[0].textContent).not.toContain("<p>");
    expect(cards[1]).toHaveTextContent("# 开业前 1 天");
    expect(cards[1].textContent).not.toContain("```");
  });
});

describe("KbFileDrawer · knowledge bases off", () => {
  it("keeps 可见范围 and 更改可见范围 and ignores onMove", () => {
    mocks.knowledgeBases = false;
    const onChangeScope = vi.fn();
    renderDrawer(file(), { onMove: vi.fn(), onChangeScope });
    expect(screen.getByText(messagesZh.knowledge_base.drawer.meta_scope)).toBeInTheDocument();
    expect(screen.queryByText("所属知识库")).toBeNull();
    expect(screen.queryByTestId("kb-drawer-move")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: messagesZh.knowledge_base.drawer.change_scope }));
    expect(onChangeScope).toHaveBeenCalledOnce();
    expect(mocks.members).not.toHaveBeenCalled();
  });
});
