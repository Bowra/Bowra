/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.1 Q1): with the knowledge base layout the Extraction tables tab
 * (`records`) lists the extraction-table definitions (create, edit, delete) together with today's extraction
 * results, and the Templates tab keeps only matrix templates, whose New template goes straight to a new matrix.
 * Without the layout both tabs stay exactly as today.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { openRowMenu } from "@/__tests__/row-actions";
import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";

const mocks = vi.hoisted(() => ({
  search: "tab=records",
  push: vi.fn(),
  replace: vi.fn(),
  listDocTypes: vi.fn(),
  listTeam: vi.fn(),
  deleteDocType: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(mocks.search) }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: mocks.push, replace: mocks.replace }) }));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    activeId: "ws_1",
    items: [{ id: "ws_1", name: "商超零售场景", desc: "", role: "member" }],
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ status: "authenticated", me: { id: "u_me", team_id: "team_1" } }),
}));
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return { ...actual, listDocTypesWithStats: (...args: unknown[]) => mocks.listDocTypes(...args) };
});
vi.mock("@/lib/kb-matrix/snapshot-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/snapshot-api")>();
  return { ...actual, listMatrices: vi.fn().mockResolvedValue([]) };
});
vi.mock("@/lib/kb-matrix/team-template-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/team-template-api")>();
  return { ...actual, listTeamTemplates: (...args: unknown[]) => mocks.listTeam(...args) };
});
vi.mock("@/lib/kb-matrix/graduate-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/graduate-api")>();
  return { ...actual, deleteDocTypeTyped: (...args: unknown[]) => mocks.deleteDocType(...args) };
});

import { MatrixTab } from "./matrix-tab";

function dt(id: string, kind: string, createdBy = "u_me"): KbDocTypeDTO {
  return {
    doc_type_id: id,
    workspace_id: "ws_1",
    name: `名称_${id}`,
    description: "",
    fields: [{ name: "金额", type: "string", description: "", required: false, cross_verify: false, extraction_hint: "" }],
    record_count: 0,
    extractor_model: "qwen-plus",
    cross_verify_enabled: true,
    created_by: createdBy,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    kind,
  } as KbDocTypeDTO;
}

const seed = dt("dt_seed", "seed", "system");
const group = dt("dt_group", "template");
const invoices = dt("dt_invoice", "manual");
const contracts = dt("dt_contract", "manual");

beforeEach(() => {
  mocks.search = "tab=records";
  mocks.push.mockReset();
  mocks.replace.mockReset();
  mocks.deleteDocType.mockReset();
  mocks.listTeam.mockResolvedValue([]);
  mocks.listDocTypes.mockResolvedValue({
    items: [seed, group, invoices, contracts],
    stats: { dt_invoice: { total: 4, last_extracted_at: "2026-11-20T00:00:00Z" } },
  });
});
afterEach(cleanup);

describe("MatrixTab · knowledge base layout", () => {
  it("Extraction tables lists the definitions, then their results; no matrix templates", async () => {
    renderIntl(<MatrixTab section="records" knowledgeBasesLayout />);
    const definitions = await screen.findByTestId("kb-tpl-group-extraction");
    expect(within(definitions).getByTestId("kb-tpl-extraction-dt_invoice")).toBeInTheDocument();
    expect(within(definitions).getByTestId("kb-tpl-extraction-dt_contract")).toBeInTheDocument();
    const results = await screen.findByTestId("kb-records-section");
    expect(results.querySelector(".tgh .t")).toHaveTextContent("提取结果");
    await waitFor(() => expect(within(results).getByTestId("kb-matrix-view-dt_invoice")).toBeInTheDocument());
    // Results follow the definitions.
    expect(definitions.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByTestId("kb-tpl-group-seed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-tpl-group-mine")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-tpl-group-team")).not.toBeInTheDocument();
    // The Team library is not read for this tab.
    expect(mocks.listTeam).not.toHaveBeenCalled();

    // A result opens its view in the same tab.
    fireEvent.click(within(results).getByTestId("kb-matrix-view-dt_invoice"));
    expect(mocks.push).toHaveBeenCalledWith("/knowledge?tab=records&view=tpl_dt_invoice");
  });

  it("New extraction table opens the extraction-table wizard directly", async () => {
    renderIntl(<MatrixTab section="records" knowledgeBasesLayout />);
    await screen.findByTestId("kb-tpl-group-extraction");
    const create = await screen.findByTestId("kb-tpl-new");
    expect(create).toHaveTextContent("新建提取表");
    fireEvent.click(create);
    expect(screen.queryByTestId("kb-tpl-purpose")).not.toBeInTheDocument();
    expect(screen.getByTestId("kt-wizard")).toBeInTheDocument();
  });

  it("deleting an extraction table reloads the results listed next to it", async () => {
    mocks.deleteDocType.mockResolvedValue(undefined);
    renderIntl(<MatrixTab section="records" knowledgeBasesLayout />);
    const results = await screen.findByTestId("kb-records-section");
    await waitFor(() => expect(within(results).getByTestId("kb-matrix-view-dt_invoice")).toBeInTheDocument());
    mocks.listDocTypes.mockResolvedValue({ items: [seed, group, contracts], stats: {} });
    fireEvent.click(within(await openRowMenu("名称_dt_invoice 的更多操作")).getByRole("menuitem", { name: "删除…" }));
    fireEvent.click(await screen.findByTestId("kb-matrix-tpl-delete-confirm"));
    await waitFor(() => expect(mocks.deleteDocType).toHaveBeenCalledWith("dt_invoice"));
    await waitFor(() => expect(screen.queryByTestId("kb-matrix-view-dt_invoice")).not.toBeInTheDocument());
    expect(screen.getByTestId("kb-records-empty")).toBeInTheDocument();
  });

  it("Templates keeps only matrix templates; New template goes straight to a new matrix", async () => {
    mocks.search = "tab=templates";
    renderIntl(<MatrixTab section="templates" knowledgeBasesLayout />);
    const flat = await screen.findByTestId("kb-tpl-flat");
    const order = Array.from(flat.querySelectorAll("section[data-testid^='kb-tpl-group-']")).map((el) => el.getAttribute("data-testid"));
    expect(order).toEqual(["kb-tpl-group-seed", "kb-tpl-group-mine", "kb-tpl-group-team"]);
    expect(screen.getByText("模板是矩阵里可复用的一组问题列，套用到新矩阵即可一次带出整组列。")).toBeInTheDocument();
    const create = await screen.findByTestId("kb-tpl-new");
    expect(create).toHaveTextContent("新建模板");
    fireEvent.click(create);
    expect(screen.queryByTestId("kb-tpl-purpose")).not.toBeInTheDocument();
    expect(mocks.push).toHaveBeenCalledWith("/knowledge?tab=matrix&new=1");
  });
});

describe("MatrixTab · switch off (no layout)", () => {
  it("Extracted results stays today's results list only", async () => {
    renderIntl(<MatrixTab section="records" />);
    const results = await screen.findByTestId("kb-records-section");
    expect(results.querySelector(".viewhd")).toHaveTextContent("按提取表从文件中提取出的结果，可以在对话里直接查询。");
    await waitFor(() => expect(within(results).getByTestId("kb-matrix-view-dt_invoice")).toBeInTheDocument());
    expect(screen.queryByTestId("kb-tpl-group-extraction")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kb-tpl-new")).not.toBeInTheDocument();
  });

  it("Templates keeps every group and asks the purpose first", async () => {
    mocks.search = "tab=templates";
    renderIntl(<MatrixTab section="templates" />);
    const flat = await screen.findByTestId("kb-tpl-flat");
    const order = Array.from(flat.querySelectorAll("section[data-testid^='kb-tpl-group-']")).map((el) => el.getAttribute("data-testid"));
    expect(order).toEqual(["kb-tpl-group-seed", "kb-tpl-group-mine", "kb-tpl-group-team", "kb-tpl-group-extraction"]);
    fireEvent.click(await screen.findByTestId("kb-tpl-new"));
    expect(screen.getByTestId("kb-tpl-purpose")).toBeInTheDocument();
  });
});
