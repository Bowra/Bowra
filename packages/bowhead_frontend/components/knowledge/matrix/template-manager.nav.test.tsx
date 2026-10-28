/**
 * Knowledge-base UX cleanup P1 (interaction design 05 · C): left-navigation Templates view = TemplateManager.
 *
 * Contract:
 *   1. Extraction tables (kind=manual) have a separate Extraction Tables group, listing all fields (required marked *) and record count, no longer mixed into My Templates.
 *   2. Edit/delete appear only for people admitted by the server's admin-or-creator gate (creator, space admin, team admin).
 *   3. New Template first asks the purpose: use in a matrix → new-matrix flow; automatic upload extraction → extraction-table wizard.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";

const mocks = vi.hoisted(() => ({
  listDocTypes: vi.fn(),
  listTeam: vi.fn(),
}));

vi.mock("@/lib/kb-matrix/team-template-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/team-template-api")>();
  return { ...actual, listTeamTemplates: (...args: unknown[]) => mocks.listTeam(...args) };
});
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_1", loading: false, items: [], error: null, refresh: vi.fn() }),
}));
vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return { ...actual, listDocTypesWithStats: (...args: unknown[]) => mocks.listDocTypes(...args) };
});
vi.mock("@/lib/kb-matrix/snapshot-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/snapshot-api")>();
  return { ...actual, listMatrices: vi.fn().mockResolvedValue([]) };
});

import { templatesListedCount } from "@/lib/kb-matrix/template-view";
import type { TeamTemplateDTO } from "@/lib/kb-matrix/team-template-api";

import { TemplateManager } from "./template-manager";

function dt(id: string, kind: string, createdBy: string, fields: Array<[string, boolean]> = [["金额", false]]): KbDocTypeDTO {
  return {
    doc_type_id: id,
    workspace_id: "ws_1",
    name: `名称_${id}`,
    description: "",
    fields: fields.map(([name, required]) => ({
      name,
      type: "string",
      description: "",
      required,
      cross_verify: false,
      extraction_hint: "",
    })),
    record_count: 0,
    extractor_model: "qwen-plus",
    cross_verify_enabled: true,
    created_by: createdBy,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    kind,
  } as KbDocTypeDTO;
}

const mineManual = dt("dt_mine", "manual", "u_me", [["报告期", true], ["营业收入", false]]);
const otherManual = dt("dt_other", "manual", "u_other");
const columnGroup = dt("dt_group", "template", "u_me");

function renderNav(opts: { isAdmin?: boolean; onNewMatrix?: () => void } = {}) {
  return renderIntl(
    <TemplateManager
      workspaceId="ws_1"
      canEdit
      uid="u_me"
      isAdmin={opts.isAdmin ?? false}
      onApplyTemplate={() => undefined}
      onOpenMatrix={() => undefined}
      toast={() => undefined}
      onNewMatrix={opts.onNewMatrix ?? (() => undefined)}
      onViewRecords={() => undefined}
    />,
  );
}

beforeEach(() => {
  mocks.listTeam.mockResolvedValue([]);
  mocks.listDocTypes.mockResolvedValue({
    items: [columnGroup, mineManual, otherManual],
    stats: { dt_mine: { total: 4, last_extracted_at: null } },
  });
});
afterEach(cleanup);

describe("模板视图 · 提取表分组", () => {
  it("manual 单独成组：字段全列、必填带 *、记录数；不再出现在「我的模板」", async () => {
    renderNav();
    const group = await screen.findByTestId("kb-tpl-group-extraction");
    const card = within(group).getByTestId("kb-tpl-extraction-dt_mine");
    expect(card).toHaveTextContent("报告期* · 营业收入");
    expect(card).toHaveTextContent("2 字段 · 4 条记录");
    expect(within(group).getByTestId("kb-tpl-extraction-dt_other")).toBeInTheDocument();
    // My Templates contains only column-group templates now.
    expect(screen.getByTestId("kb-matrix-tpl-dt_group")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-matrix-tpl-dt_mine")).not.toBeInTheDocument();
  });

  it("编辑/删除只给创建者；管理员对所有提取表可见", async () => {
    renderNav();
    await screen.findByTestId("kb-tpl-group-extraction");
    expect(menuItemNames(await openRowMenu("名称_dt_mine 的更多操作"))).toEqual(["查看提取结果", "编辑", "删除…"]);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(menuItemNames(await openRowMenu("名称_dt_other 的更多操作"))).toEqual(["查看提取结果"]);

    cleanup();
    renderNav({ isAdmin: true });
    await screen.findByTestId("kb-tpl-group-extraction");
    expect(menuItemNames(await openRowMenu("名称_dt_other 的更多操作"))).toEqual(["查看提取结果", "编辑", "删除…"]);
  });

  it("新建模板先问用途：矩阵列组走新建矩阵，自动抽取打开向导", async () => {
    const onNewMatrix = vi.fn();
    renderNav({ onNewMatrix });
    await screen.findByTestId("kb-tpl-group-extraction");

    fireEvent.click(screen.getByTestId("kb-tpl-new"));
    fireEvent.click(screen.getByTestId("kb-tpl-purpose-matrix"));
    expect(onNewMatrix).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("kt-wizard")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("kb-tpl-new"));
    fireEvent.click(screen.getByTestId("kb-tpl-purpose-extraction"));
    expect(screen.getByTestId("kt-wizard")).toBeInTheDocument();
    expect(screen.getByTestId("kt-describe")).toBeInTheDocument();
  });
});

const seed = dt("dt_seed", "seed", "system");

function teamEntry(id: string): TeamTemplateDTO {
  return {
    entry_id: id,
    source_doc_type_id: `src_${id}`,
    name: `团队_${id}`,
    description: "",
    field_count: 1,
    field_preview: [{ name: "金额", type: "currency" }],
    source_workspace_id: "ws_src",
    source_workspace_name: "法务",
    published_by: "u_pub",
    published_by_name: "发布者",
    published_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    is_own_space: false,
    already_copied: false,
  } as TeamTemplateDTO;
}

describe("模板视图 · 平铺分组（左栏导航）", () => {
  it("不再有「我的模板 / 团队模板库」分段；分组依次为 内置 → 我的模板 → 团队模板库 → 提取表", async () => {
    mocks.listDocTypes.mockResolvedValue({ items: [seed, columnGroup, mineManual, otherManual], stats: {} });
    mocks.listTeam.mockResolvedValue([teamEntry("ot_1"), teamEntry("ot_2")]);
    renderNav();
    const flat = await screen.findByTestId("kb-tpl-flat");
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    const order = Array.from(flat.querySelectorAll("section[data-testid^='kb-tpl-group-']")).map((el) =>
      el.getAttribute("data-testid"),
    );
    expect(order).toEqual(["kb-tpl-group-seed", "kb-tpl-group-mine", "kb-tpl-group-team", "kb-tpl-group-extraction"]);
    // Each group header = name + count + one explanatory sentence
    for (const head of flat.querySelectorAll(".tgh")) {
      expect(head.querySelector(".t")?.textContent).toBeTruthy();
      expect(head.querySelector(".n")?.textContent).toMatch(/^\d+$/);
      expect(head.querySelector(".d")?.textContent).toBeTruthy();
    }
    await waitFor(() => expect(within(screen.getByTestId("kb-tpl-group-team")).getByText("团队_ot_1")).toBeInTheDocument());
  });

  it("左栏「模板」计数 = 视图各分组列出的对象之和", async () => {
    const items = [seed, columnGroup, mineManual, otherManual];
    const team = [teamEntry("ot_1"), teamEntry("ot_2"), teamEntry("ot_3")];
    mocks.listDocTypes.mockResolvedValue({ items, stats: {} });
    mocks.listTeam.mockResolvedValue(team);
    renderNav();
    const flat = await screen.findByTestId("kb-tpl-flat");
    await waitFor(() => expect(within(screen.getByTestId("kb-tpl-group-team")).getByText("团队_ot_3")).toBeInTheDocument());
    const listed = Array.from(flat.querySelectorAll(".tgh .n")).reduce((sum, n) => sum + Number(n.textContent), 0);
    expect(listed).toBe(7);
    expect(templatesListedCount(items, team)).toBe(listed);
  });

  it("空分组用一行虚线说明，不用大插画", async () => {
    mocks.listDocTypes.mockResolvedValue({ items: [seed, mineManual], stats: {} });
    renderNav();
    const mine = await screen.findByTestId("kb-tpl-group-mine");
    expect(mine.querySelector(".emptyline")).toHaveTextContent("还没有。打开一个矩阵，在列设置里选「存为模板」。");
    expect(screen.queryByTestId("kb-matrix-tpl-empty")).not.toBeInTheDocument();
    const team = screen.getByTestId("kb-tpl-group-team");
    await waitFor(() => expect(team.querySelector(".emptyline")).toBeInTheDocument());
  });
});
