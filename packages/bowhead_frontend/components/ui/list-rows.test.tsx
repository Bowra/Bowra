/**
 * Mobile list rows (decision 12): replace management tables when the viewport is narrower than 640.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";

import { ListRow, ListRows, useListRowsLayout } from "./list-rows";

function mockMatchMedia(matches: boolean) {
  const matchMedia = vi.fn().mockReturnValue({ matches, addEventListener: vi.fn(), removeEventListener: vi.fn() });
  vi.stubGlobal("matchMedia", matchMedia);
  return matchMedia;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useListRowsLayout", () => {
  it("uses list rows on narrow screens and keeps the table on wide screens", () => {
    mockMatchMedia(true);
    expect(renderHook(() => useListRowsLayout()).result.current).toBe(true);
    cleanup();
    mockMatchMedia(false);
    expect(renderHook(() => useListRowsLayout()).result.current).toBe(false);
  });
});

describe("ListRow", () => {
  it("按槽位渲染标题、第二行、小字、行尾状态与操作", () => {
    render(
      <ListRows label="成员">
        <ListRow title="a@x.com" subtitle="Alice" meta="3 天前" status={<span>管理员</span>} actions={<button type="button">更多</button>} />
      </ListRows>,
    );
    const list = screen.getByRole("list", { name: "成员" });
    expect(list.querySelector(".ui-list-row-title")?.textContent).toBe("a@x.com");
    expect(list.querySelector(".ui-list-row-sub")?.textContent).toBe("Alice");
    expect(list.querySelector(".ui-list-row-meta")?.textContent).toBe("3 天前");
    expect(list.querySelector(".ui-list-row-status")?.textContent).toBe("管理员");
    expect(screen.getByRole("button", { name: "更多" })).toBeTruthy();
  });

  it("点整行打开：标题是按钮，读出展开状态；已停用的行加 is-muted", () => {
    const onOpen = vi.fn();
    render(
      <ListRows label="审计">
        <ListRow title="member_invited" onOpen={onOpen} openLabel="member_invited" expanded={false} muted />
      </ListRows>,
    );
    const open = screen.getByRole("button", { name: "member_invited" });
    expect(open.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(open.closest("li")?.className).toContain("is-muted");
  });
});
