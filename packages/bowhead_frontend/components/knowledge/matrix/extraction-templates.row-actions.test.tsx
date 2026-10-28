/** Extraction-template card “⋯” menu: View Records, Edit and Delete live in the menu. */
import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";

import { ExtractionTemplateGroup } from "./extraction-templates";

const DOC_TYPE = {
  doc_type_id: "dt_1", name: "采购合同", fields: [{ name: "金额", required: true }], created_by: "u_1", record_count: 3,
} as unknown as KbDocTypeDTO;

afterEach(() => cleanup());

function renderGroup() {
  const handlers = { onEdit: vi.fn(), onDelete: vi.fn(), onViewRecords: vi.fn() };
  renderIntl(
    <ExtractionTemplateGroup items={[DOC_TYPE]} recordCounts={{}} uid="u_1" isAdmin busy={false} {...handlers} />,
  );
  return handlers;
}

describe("ExtractionTemplateGroup 卡片操作", () => {
  it("the menu lists View records, Edit, Delete in order and each item calls its own handler", async () => {
    const handlers = renderGroup();
    expect(screen.queryByTestId("kb-tpl-extraction-delete-dt_1")).toBeNull();
    const menu = await openRowMenu("采购合同 的更多操作");
    const [records, edit, del] = menuItemNames(menu);
    expect(del).toBe("删除…");
    screen.getByRole("menuitem", { name: records }).click();
    expect(handlers.onViewRecords).toHaveBeenCalledWith(DOC_TYPE);
    await chooseRowAction("采购合同 的更多操作", edit, { danger: false });
    // The second argument is the focus-return target for the wizard: the row menu trigger.
    expect(handlers.onEdit).toHaveBeenCalledWith(DOC_TYPE, screen.getByRole("button", { name: "采购合同 的更多操作" }));
    await chooseRowAction("采购合同 的更多操作", "删除…", { danger: true });
    expect(handlers.onDelete).toHaveBeenCalledWith(DOC_TYPE);
  });
});
