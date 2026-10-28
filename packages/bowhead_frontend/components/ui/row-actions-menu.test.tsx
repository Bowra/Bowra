/**
 * Row-end “⋯” menu interaction contract (appearance batch four: move inline dangerous actions into the menu):
 * danger items red, disabled items explain why, and selection only calls the original handler (caller opens confirmation as before);
 * clicks and key presses do not bubble to clickable rows.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { RowActionsMenu } from "./row-actions-menu";

afterEach(() => cleanup());

describe("RowActionsMenu", () => {
  it("触发按钮默认叫「更多操作」，打开后危险项标红，选中调用原处理函数并关闭菜单", () => {
    const onDelete = vi.fn();
    renderIntl(<RowActionsMenu actions={[{ id: "delete", label: "删除", danger: true, onSelect: onDelete }]} />);
    const trigger = screen.getByRole("button", { name: "更多操作" });
    expect(trigger.className).toContain("ui-row-more");
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu", { name: "更多操作" });
    expect(menu.className).toContain("ui-menu");
    const item = screen.getByRole("menuitem", { name: "删除…" });
    expect(item.className).toContain("is-danger");
    fireEvent.click(item);
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("禁用项不执行，原因经 aria-describedby 读出", () => {
    const onRemove = vi.fn();
    renderIntl(
      <RowActionsMenu
        label="张三 的更多操作"
        actions={[{ id: "remove", label: "移除", danger: true, disabled: true, disabledReason: "负责人不能移除", onSelect: onRemove }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "张三 的更多操作" }));
    const item = screen.getByRole("menuitem", { name: "移除…" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    const reason = document.getElementById(item.getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe("负责人不能移除");
    fireEvent.click(item);
    expect(onRemove).not.toHaveBeenCalled();
  });

  it("点击触发按钮、菜单项与回车都不冒泡到行（行本身常是「点开详情」）", () => {
    const onRowClick = vi.fn();
    const onRowKey = vi.fn();
    const onDelete = vi.fn();
    renderIntl(
      <div role="row" tabIndex={0} onClick={onRowClick} onKeyDown={onRowKey}>
        <RowActionsMenu actions={[{ id: "delete", label: "删除", danger: true, onSelect: onDelete }]} />
      </div>,
    );
    const trigger = screen.getByRole("button", { name: "更多操作" });
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(trigger);
    const item = screen.getByRole("menuitem", { name: "删除…" });
    fireEvent.keyDown(item, { key: "Enter" });
    fireEvent.click(item);
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onRowClick).not.toHaveBeenCalled();
    expect(onRowKey).not.toHaveBeenCalled();
  });

  it("没有动作时不渲染触发按钮", () => {
    renderIntl(<RowActionsMenu actions={[]} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
