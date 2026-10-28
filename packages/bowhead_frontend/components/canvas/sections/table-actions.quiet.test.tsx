/**
 * Quiet-by-default integration contract (chart-config-ux ⑥, user decision 2026-09-02).
 *
 * Decision: four actions collapse into one `⋯` + **chart-kind switcher stays visible**, rather than hiding all five—
 * user's exact words: “可以默认安静（但是 chart 切换还是需要单独的）”.
 * Lock both halves: testing only collapsed actions would let someone tuck children into
 * the overlay later with all tests still passing.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChartMenuRootContext } from "./chart-menu-root";
import { TableActions } from "./table-actions";

afterEach(() => cleanup());

const ACTIONS = [
  { key: "open-data", label: "打开数据", onClick: vi.fn() },
  { key: "download-csv", label: "下载 CSV", onClick: vi.fn() },
];

function renderQuiet(extra?: { surface?: "canvas" | "chat" | "export" }) {
  return render(
    <TableActions variant="quiet" moreLabel="更多操作" actions={ACTIONS} {...extra}>
      <button type="button">柱状图 ▾</button>
    </TableActions>,
  );
}

describe("TableActions variant=quiet", () => {
  it("★ 默认只出 `⋯` + 图种切换器，动作都收起来", () => {
    renderQuiet();
    expect(screen.getByRole("button", { name: "更多操作" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "柱状图 ▾" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "打开数据" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "下载 CSV" })).toBeNull();
  });

  it("★★ 图种切换器不在 `⋯` 弹层里——user 点名要它单独", () => {
    renderQuiet();
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    const pop = document.querySelector(".v2-chart-actions-pop")!;
    expect(pop).not.toBeNull();
    // The overlay contains those two actions, **excluding** the switcher.
    expect(pop.textContent).toContain("打开数据");
    expect(pop.textContent).not.toContain("柱状图");
    // Switcher remains outside the overlay, a sibling of `⋯`.
    expect(pop.contains(screen.getByRole("button", { name: "柱状图 ▾" }))).toBe(false);
  });

  it("`⋯` 弹层挂进上层指定的容器（设置页预览区带着自己的配色变量）", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    try {
      render(
        <ChartMenuRootContext.Provider value={host}>
          <TableActions variant="quiet" moreLabel="更多操作" actions={ACTIONS} />
        </ChartMenuRootContext.Provider>,
      );
      fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
      expect(host.querySelector(".v2-chart-actions-pop")).not.toBeNull();
    } finally {
      cleanup();
      host.remove();
    }
  });

  it("点开 `⋯` 后动作可点，点完菜单关掉", () => {
    renderQuiet();
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "打开数据" }));
    expect(ACTIONS[0]!.onClick).toHaveBeenCalled();
    expect(document.querySelector(".v2-chart-actions-pop")).toBeNull();
  });

  it("★ `⋯` 必须有可读名——只有一个 `⋯` 字符的话读屏软件念不出东西", () => {
    renderQuiet();
    const more = screen.getByRole("button", { name: "更多操作" });
    expect(more).toHaveAttribute("aria-haspopup", "menu");
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
  });

  it("★ 菜单开着时挂 is-open —— CSS 靠它保证「点开了 `⋯` 不因鼠标移出而淡出」", () => {
    // Avoid `:has()`: 116 is an old plain-HTTP environment; avoid new selectors where possible.
    const { container } = renderQuiet();
    const wrap = container.querySelector(".v2-chart-actions.is-quiet")!;
    expect(wrap.classList.contains("is-open")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(wrap.classList.contains("is-open")).toBe(true);
  });

  it("★ 导出面一个都不出——连 `⋯` 和切换器都不许有（纸上点不了）", () => {
    const { container } = renderQuiet({ surface: "export" });
    expect(container.innerHTML).toBe("");
  });

  it("hidden 的动作不进弹层", () => {
    render(
      <TableActions
        variant="quiet"
        moreLabel="更多操作"
        actions={[{ key: "open-data", label: "打开数据", onClick: vi.fn(), hidden: true }, ACTIONS[1]!]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.queryByRole("menuitem", { name: "打开数据" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "下载 CSV" })).toBeInTheDocument();
  });

  it("键盘打开焦点进第一项，方向键移动，Esc 关闭并回到 `⋯`", async () => {
    renderQuiet();
    const more = screen.getByRole("button", { name: "更多操作" });
    fireEvent.click(more, { detail: 0 });
    const [first, second] = await screen.findAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(first));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(second);

    fireEvent.keyDown(second!, { key: "Escape" });

    await waitFor(() => expect(document.querySelector(".v2-chart-actions-pop")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(more));
  });

  it("点菜单外面关闭", async () => {
    render(
      <>
        <TableActions variant="quiet" moreLabel="更多操作" actions={ACTIONS} />
        <p>图外</p>
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.pointerDown(screen.getByText("图外"));

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});
