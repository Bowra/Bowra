/**
 * Active-filter chip row (appearance decision 20): show with at least 2 active conditions; remove individually, with Clear All at the end.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { ActiveFilterChips } from "./active-filter-chips";

afterEach(cleanup);

describe("ActiveFilterChips", () => {
  it("只有 1 个条件时不出现（筛选按钮上已写出值）", () => {
    renderIntl(<ActiveFilterChips onClearAll={vi.fn()} filters={[{ key: "q", label: "关键词：星舰", onRemove: vi.fn() }]} />);
    expect(screen.queryByRole("list", { name: "已选筛选" })).toBeNull();
  });

  it("满 2 个出现；× 去掉对应条件，「清除全部」清空", () => {
    const removeQ = vi.fn();
    const removeRole = vi.fn();
    const clearAll = vi.fn();
    renderIntl(
      <ActiveFilterChips
        onClearAll={clearAll}
        filters={[{ key: "q", label: "关键词：星舰", onRemove: removeQ }, { key: "role", label: "角色：管理员", onRemove: removeRole }]}
      />,
    );
    const list = screen.getByRole("list", { name: "已选筛选" });
    expect(list.className).toContain("ui-filter-chips");
    expect(screen.getByText("关键词：星舰")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "移除筛选：角色：管理员" }));
    expect(removeRole).toHaveBeenCalledTimes(1);
    expect(removeQ).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "清除全部" }));
    expect(clearAll).toHaveBeenCalledTimes(1);
  });
});
