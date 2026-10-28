/**
 * Knowledge base polish (1.5.0): filter buttons name their values, and a selected-filters row appears at two or
 * more selected values (keyword, each status, each type, date; scope never counts).
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";
import deMessages from "@/messages/de-DE.json";

import { KbFilesControls, type KbFilesControlsProps, type StatusBucket } from "./kb-files-controls";

const zh = zhMessages.knowledge_base;
const de = deMessages.knowledge_base;

function props(over: Partial<KbFilesControlsProps> = {}): KbFilesControlsProps {
  return {
    query: "",
    deep: false,
    fStatus: new Set<StatusBucket>(),
    fTypes: new Set<string>(),
    fDays: null,
    sortKey: "uploadedAt",
    sortDir: "desc",
    onQuery: vi.fn(),
    onClearQuery: vi.fn(),
    onToggleDeep: vi.fn(),
    onSubmitDeep: vi.fn(),
    onToggleStatus: vi.fn(),
    onToggleType: vi.fn(),
    onSetDays: vi.fn(),
    onClearStatus: vi.fn(),
    onClearType: vi.fn(),
    onClearSheet: vi.fn(),
    onSetSort: vi.fn(),
    onClear: vi.fn(),
    scopeFilter: <button type="button">scope</button>,
    ...over,
  };
}

afterEach(() => {
  cleanup();
});

describe("filter buttons", () => {
  it("name up to two values, summarize more, and keep the sort button neutral with field and order", () => {
    renderIntl(<KbFilesControls {...props({ fStatus: new Set<StatusBucket>(["failed", "done"]), fTypes: new Set(["pdf", "doc", "sheet"]), fDays: 30 })} />);
    // Values follow menu order, not click order.
    expect(document.querySelector('[data-kb-filter="status"]')).toHaveTextContent(`${zh.filter.status_label}：${zh.filter.status.done}、${zh.filter.status.failed}`);
    expect(document.querySelector('[data-kb-filter="type"]')).toHaveTextContent(`${zh.filter.type_label}：PDF等 3 项`);
    expect(document.querySelector('[data-kb-filter="date"]')).toHaveTextContent(`${zh.filter.date_label}：近 30 天`);
    const sort = screen.getByRole("button", { name: "排序：上传时间 · 新→旧" });
    expect(sort).not.toHaveClass("active");
    for (const id of ["status", "type", "date", "sort"]) expect(document.querySelector(`[data-kb-filter="${id}"] .kb-chevron`)).not.toBeNull();
  });

  it("uses the locale's own separators (de-DE)", () => {
    renderIntl(<KbFilesControls {...props({ fStatus: new Set<StatusBucket>(["failed", "done"]) })} />, { locale: "de-DE" });
    expect(document.querySelector('[data-kb-filter="status"]')).toHaveTextContent(`Status: ${de.filter.status.done}, ${de.filter.status.failed}`);
  });
});

describe("selected-filters row", () => {
  it("does not appear for a single value (the button already names it)", () => {
    renderIntl(<KbFilesControls {...props({ fStatus: new Set<StatusBucket>(["failed"]) })} />);
    expect(screen.queryByRole("list", { name: zh.filter.selected_label })).toBeNull();
  });

  it("counts each multi-select value: two statuses show two tokens; removing one clears only that value", () => {
    const p = props({ fStatus: new Set<StatusBucket>(["failed", "done"]) });
    renderIntl(<KbFilesControls {...p} />);
    const row = screen.getByRole("list", { name: zh.filter.selected_label });
    expect(within(row).getAllByRole("listitem")).toHaveLength(3); // two tokens + clear all
    fireEvent.click(within(row).getByRole("button", { name: `移除筛选：${zh.filter.status_label}：${zh.filter.status.failed}` }));
    expect(p.onToggleStatus).toHaveBeenCalledWith("failed");
    expect(p.onToggleStatus).toHaveBeenCalledTimes(1);
  });

  it("lists keyword, type and date; removes the keyword at once; clear all resets", () => {
    const p = props({ query: "合同", fTypes: new Set(["pdf"]), fDays: 7 });
    renderIntl(<KbFilesControls {...p} />);
    const row = screen.getByRole("list", { name: zh.filter.selected_label });
    expect(row).toHaveTextContent(`${zh.filter.keyword}：合同`);
    expect(row).toHaveTextContent(`${zh.filter.type_label}：PDF`);
    expect(row).toHaveTextContent(`${zh.filter.date_label}：近 7 天`);
    expect(row).not.toHaveTextContent("scope");
    fireEvent.click(within(row).getByRole("button", { name: `移除筛选：${zh.filter.keyword}：合同` }));
    expect(p.onClearQuery).toHaveBeenCalledTimes(1);
    fireEvent.click(within(row).getByRole("button", { name: "清除全部" }));
    expect(p.onClear).toHaveBeenCalledTimes(1);
  });

  it("moves focus to the next token after a removal, and back to the filter button when the row goes away", () => {
    function Harness({ statuses }: { statuses: StatusBucket[] }) {
      return <NextIntlClientProvider locale="zh-CN" messages={zhMessages}><KbFilesControls {...props({ fStatus: new Set(statuses) })} /></NextIntlClientProvider>;
    }
    const { rerender } = render(<Harness statuses={["processing", "done", "failed"]} />);
    const removes = () => document.querySelectorAll<HTMLButtonElement>(".ui-filter-chip-remove");
    removes()[0].focus();
    fireEvent.click(removes()[0]);
    rerender(<Harness statuses={["done", "failed"]} />);
    expect(document.activeElement).toBe(removes()[0]);
    expect(document.activeElement).toHaveAccessibleName(`移除筛选：${zh.filter.status_label}：${zh.filter.status.done}`);
    fireEvent.click(removes()[0]);
    rerender(<Harness statuses={["failed"]} />);
    expect(screen.queryByRole("list", { name: zh.filter.selected_label })).toBeNull();
    expect(document.activeElement).toBe(document.querySelector('[data-kb-filter="status"]'));
  });
});

describe("filter menus", () => {
  it("use two-line items instead of parenthesized notes and offer a clear item only when the group has a value", async () => {
    const p = props({ fStatus: new Set<StatusBucket>(["failed"]) });
    renderIntl(<KbFilesControls {...p} />);
    fireEvent.click(document.querySelector<HTMLElement>('[data-kb-filter="status"]')!);
    const processing = await screen.findByRole("menuitemcheckbox", { name: zh.filter.status.processing });
    expect(processing).toHaveAccessibleDescription(zh.filter.status_desc.processing);
    expect(processing.textContent).not.toMatch(/[()]/);
    fireEvent.click(screen.getByRole("menuitem", { name: zh.filter.clear_status }));
    expect(p.onClearStatus).toHaveBeenCalledTimes(1);

    cleanup();
    renderIntl(<KbFilesControls {...props()} />);
    fireEvent.click(document.querySelector<HTMLElement>('[data-kb-filter="type"]')!);
    expect(await screen.findByRole("menuitemcheckbox", { name: zh.filter.type.doc })).toHaveAccessibleDescription(zh.filter.type_desc.doc);
    expect(screen.queryByRole("menuitem", { name: zh.filter.clear_type })).toBeNull();
  });

  it("sort menu has labelled groups and field-specific order wording", async () => {
    renderIntl(<KbFilesControls {...props({ sortKey: "sizeMB" })} />);
    fireEvent.click(document.querySelector<HTMLElement>('[data-kb-filter="sort"]')!);
    expect(await screen.findByRole("group", { name: zh.filter.sort_field })).toBeInTheDocument();
    const order = screen.getByRole("group", { name: zh.filter.sort_order_label });
    expect(within(order).getByRole("menuitemradio", { name: "大的在前" })).toHaveAttribute("aria-checked", "true");
    expect(within(order).getByRole("menuitemradio", { name: "小的在前" })).toBeInTheDocument();
  });
});

describe("phone filter sheet", () => {
  it("collapses filters behind 'Filters · N'; the sheet lists scope, status, type and date and applies at once", async () => {
    const onScope = vi.fn();
    const p = props({
      narrow: true,
      fStatus: new Set<StatusBucket>(["failed", "done"]),
      fDays: 30,
      resultCount: 4,
      scopeSheet: { scope: "space", onSelect: onScope, options: [
        { id: "all", label: "全部", hint: "h", count: 9 },
        { id: "space", label: "空间 · 零售", hint: "h", count: 4 },
      ] },
    });
    renderIntl(<KbFilesControls {...p} />);
    // Phone toolbar: no inline status/type/date chips; sort stays reachable.
    expect(document.querySelector('[data-kb-filter="status"]')).toBeNull();
    expect(document.querySelector('[data-kb-filter="sort"]')).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: `${zh.files_view.filters} · 3` }));
    const sheet = await screen.findByTestId("kb-filter-sheet");
    for (const label of [zh.filter.scope_label, zh.filter.status_label, zh.filter.type_label, zh.filter.date_label]) {
      expect(within(sheet).getByRole("heading", { name: label })).toBeInTheDocument();
    }
    expect(within(sheet).getByTestId("kb-sheet-scope-space")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(within(sheet).getByTestId("kb-sheet-scope-all"));
    expect(onScope).toHaveBeenCalledWith("all");
    fireEvent.click(within(sheet).getByRole("checkbox", { name: zh.filter.type.doc }));
    expect(p.onToggleType).toHaveBeenCalledWith("doc");
    expect(within(sheet).getByRole("radio", { name: "近 30 天" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("kb-filter-sheet-done")).toHaveTextContent("显示 4 个文件");
    // The sheet clears only what it shows; keyword and deep search are outside it.
    fireEvent.click(screen.getByRole("button", { name: "清除全部" }));
    expect(p.onClearSheet).toHaveBeenCalledTimes(1);
    expect(p.onClear).not.toHaveBeenCalled();
  });
});

describe("phone focus return", () => {
  it("falls back to the Filters sheet trigger when the selected-filters row disappears", () => {
    function Harness({ statuses }: { statuses: StatusBucket[] }) {
      return <NextIntlClientProvider locale="zh-CN" messages={zhMessages}><KbFilesControls {...props({ narrow: true, fStatus: new Set(statuses) })} /></NextIntlClientProvider>;
    }
    const { rerender } = render(<Harness statuses={["done", "failed"]} />);
    const remove = document.querySelector<HTMLButtonElement>(".ui-filter-chip-remove")!;
    remove.focus();
    fireEvent.click(remove);
    rerender(<Harness statuses={["failed"]} />);
    expect(screen.queryByRole("list", { name: zh.filter.selected_label })).toBeNull();
    expect(document.activeElement).toBe(document.querySelector(".kb-filter-toggle"));
  });
});
