/**
 * Test entry contract: vitest.setup.ts runs Testing Library cleanup after every case.
 *
 * With `globals: false`, Testing Library cannot detect a global
 * afterEach and does not attach automatic cleanup. This file deliberately has no afterEach / cleanup: two cases run
 * in order; the first renders and starts a timer, and the second asserts that the previous case's DOM is removed
 * and the component is unmounted (the effect cleanup has run and the timer no longer fires).
 */
import { useEffect } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const lifecycle = { mounted: 0, unmounted: 0 };

function LeakProbe() {
  useEffect(() => {
    lifecycle.mounted += 1;
    const timer = setInterval(() => undefined, 1_000);
    return () => {
      clearInterval(timer);
      lifecycle.unmounted += 1;
    };
  }, []);
  return <div data-testid="auto-cleanup-probe">probe</div>;
}

describe("vitest.setup 全局 cleanup", () => {
  it("第一个用例 render 出组件", () => {
    render(<LeakProbe />);
    expect(screen.getByTestId("auto-cleanup-probe")).toBeTruthy();
    expect(lifecycle).toEqual({ mounted: 1, unmounted: 0 });
  });

  it("第二个用例开始时上一个用例的 DOM 已清空、组件已卸载", () => {
    expect(screen.queryByTestId("auto-cleanup-probe")).toBeNull();
    expect(document.body.innerHTML).toBe("");
    expect(lifecycle).toEqual({ mounted: 1, unmounted: 1 });
  });
});
