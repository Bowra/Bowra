// Knowledge bases (FEATURE_KNOWLEDGE_BASES): the Files page "加入知识库" dialog picks a knowledge base (PRD §4.7) and the
// Knowledge column opens the knowledge base page with the file selected. Switch off: today's scope cards and links.
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SPACES_FIXTURE, directoryFixture } from "@/components/knowledge/bases/kb-target-picker.fixtures";
import type { FileDTO, FileKbStatus } from "@/lib/api";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  knowledgeBases: true,
  readiness: "ready" as "ready" | "missing",
  directory: null as ReturnType<typeof directoryFixture> | null,
}));

vi.mock("@/lib/kb-ingestion-readiness", () => ({
  useKbIngestionReadiness: () => ({ state: mocks.readiness, retry: vi.fn() }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { knowledge_bases: mocks.knowledgeBases } } }),
}));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: SPACES_FIXTURE, activeId: "ws_1", loading: false, error: null, setActiveId: () => {}, refresh: () => {} }),
}));
vi.mock("@/components/knowledge/bases/use-knowledge-bases", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/knowledge/bases/use-knowledge-bases")>()),
  useKnowledgeBaseDirectory: () => ({ directory: mocks.directory, loading: false, error: null, refresh: vi.fn() }),
}));

import { FilesDialogs } from "./file-dialogs";
import { KbColumn } from "./kb-column";

function renderZh(node: React.ReactNode) {
  return render(<NextIntlClientProvider locale="zh-CN" messages={messagesZh}>{node}</NextIntlClientProvider>);
}

function file(over: Partial<FileDTO> = {}): FileDTO {
  return {
    file_id: "art_1", filename: "供应商合同-华东区.pdf", mime: "application/pdf", mime_family: "pdf", size: 1024,
    source: "upload", origin: "upload", kind: "user_upload", trust: "trusted", created_at: "2026-09-20T10:00:00Z",
    last_used_at: null, deleted_at: null, category: null, category_source: null, usage_count: 0, in_kb: false, kb: null,
    kb_promotable: true, session_id: null, session_title: null, has_canvas_ref: false, retention_hint: null, url: "/api/v1/files/art_1",
    ...over,
  };
}

function kb(over: Partial<FileKbStatus> = {}): FileKbStatus {
  return { file_id: "kbf_1", scope: "space", parse_status: "done", parse_progress_pct: null, parse_stuck: false, parse_error_code: null, ...over };
}

const DLG = { kind: "kb-add" as const, name: "供应商合同-华东区.pdf" };
const checked = (id: string) => screen.getByTestId(`kb-target-${id}`).getAttribute("aria-checked") === "true";

beforeEach(() => {
  mocks.knowledgeBases = true;
  mocks.readiness = "ready";
  mocks.directory = directoryFixture();
});
afterEach(cleanup);

describe("加入知识库 · knowledge bases on", () => {
  it("picks a knowledge base instead of a scope, defaulting to the current Space's default", () => {
    const onConfirm = vi.fn();
    renderZh(<FilesDialogs dlg={{ ...DLG, onConfirm }} onClose={vi.fn()} canPromoteToTeam={false} spaceName="商超零售场景" />);
    expect(screen.getByRole("radiogroup", { name: "加入到" })).toBeInTheDocument();
    expect(screen.queryByTestId("kb-scope-personal")).toBeNull();
    expect(checked("kb_space")).toBe(true);
    expect(screen.getByTestId("kb-target-kb_policy")).toBeDisabled();
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    expect(onConfirm).toHaveBeenCalledWith("space", "kb_space");
  });

  it("confirms the chosen knowledge base with its home as the scope", () => {
    const onConfirm = vi.fn();
    renderZh(<FilesDialogs dlg={{ ...DLG, onConfirm }} onClose={vi.fn()} canPromoteToTeam={false} spaceName="商超零售场景" />);
    fireEvent.click(screen.getByTestId("kb-target-kb_comp"));
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    expect(onConfirm).toHaveBeenCalledWith("personal", "kb_comp");
  });

  it("preselects ?add_to_kb when you can add to it, else falls back", () => {
    renderZh(<FilesDialogs dlg={{ ...DLG, onConfirm: vi.fn() }} onClose={vi.fn()} canPromoteToTeam={false} spaceName="商超零售场景" knowledgeBaseTarget="kb_ops" />);
    expect(checked("kb_ops")).toBe(true);
    cleanup();
    renderZh(<FilesDialogs dlg={{ ...DLG, onConfirm: vi.fn() }} onClose={vi.fn()} canPromoteToTeam={false} spaceName="商超零售场景" knowledgeBaseTarget="kb_policy" />);
    expect(checked("kb_space")).toBe(true);
  });

  it("keeps the embedding model check", () => {
    mocks.readiness = "missing";
    const onConfirm = vi.fn();
    renderZh(<FilesDialogs dlg={{ ...DLG, onConfirm }} onClose={vi.fn()} canPromoteToTeam spaceName="商超零售场景" />);
    expect(screen.getByTestId("kb-ingestion-readiness")).toHaveTextContent("团队尚未配置并启用嵌入模型");
    expect(screen.getByTestId("kb-add-confirm")).toBeDisabled();
  });
});

describe("加入知识库 · knowledge bases off", () => {
  it("keeps today's scope cards and ignores ?add_to_kb", () => {
    mocks.knowledgeBases = false;
    const onConfirm = vi.fn();
    renderZh(<FilesDialogs dlg={{ ...DLG, onConfirm }} onClose={vi.fn()} canPromoteToTeam spaceName="商超零售场景" knowledgeBaseTarget="kb_ops" />);
    expect(screen.getByText(messagesZh.files_page.kb.kb_scope_title)).toBeInTheDocument();
    expect(screen.queryByTestId("kb-target-picker")).toBeNull();
    fireEvent.click(screen.getByTestId("kb-scope-team"));
    fireEvent.click(screen.getByTestId("kb-add-confirm"));
    expect(onConfirm).toHaveBeenCalledWith("team");
  });
});

describe("Knowledge column links", () => {
  it("opens the knowledge base page with the file selected; Space files carry their Space", () => {
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ workspace_id: "ws_retail" }) })} onAdd={vi.fn()} />);
    expect(screen.getByRole("link", { name: "在知识库中查看" })).toHaveAttribute("href", "/knowledge/base/?file_id=kbf_1&space=ws_retail");
    cleanup();
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ scope: "personal", parse_status: "failed", parse_error_code: "x" }) })} onAdd={vi.fn()} />);
    expect(screen.getByRole("link", { name: "去知识库重试" })).toHaveAttribute("href", "/knowledge/base/?file_id=kbf_1");
  });

  it("keeps today's address with the switch off", () => {
    mocks.knowledgeBases = false;
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ workspace_id: "ws_retail", parse_status: "parsing" }) })} onAdd={vi.fn()} />);
    expect(screen.getByRole("link", { name: "查看处理进度" })).toHaveAttribute("href", "/knowledge/?scope=space&space=ws_retail&file_id=kbf_1");
  });
});

describe("Knowledge column knowledge base name", () => {
  it("names a named knowledge base and a default one from the directory", () => {
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ knowledge_base_id: "kb_contracts" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入「供应商合同」 · 可用")).toBeInTheDocument();
    cleanup();
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ knowledge_base_id: "kb_space" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入「商超零售场景」 · 可用")).toBeInTheDocument();
    cleanup();
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ scope: "personal", knowledge_base_id: "kb_my" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入「我的知识」 · 可用")).toBeInTheDocument();
  });

  it("keeps today's label for a knowledge base the directory does not list, or before it loads", () => {
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ knowledge_base_id: "kb_other_space" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入 · 可用")).toBeInTheDocument();
    cleanup();
    mocks.directory = null;
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ knowledge_base_id: "kb_contracts" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入 · 可用")).toBeInTheDocument();
  });

  it("ignores the field with the switch off", () => {
    mocks.knowledgeBases = false;
    renderZh(<KbColumn file={file({ in_kb: true, kb: kb({ knowledge_base_id: "kb_contracts" }) })} onAdd={vi.fn()} />);
    expect(screen.getByText("已加入 · 可用")).toBeInTheDocument();
    expect(screen.queryByText(/供应商合同/)).toBeNull();
  });
});
