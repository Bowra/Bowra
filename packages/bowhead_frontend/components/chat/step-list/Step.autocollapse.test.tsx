/**
 * UX-1 (chat-solidify 2026-07-03) — automatically collapse sandbox code after completion.
 *
 * User observation: finished Python sandbox code kept occupying the screen. Cause: Step's
 * useState initializer evaluates only on mount: mounted while run → open=true, then done
 * never reevaluates it, leaving it open forever. Fix: state-transition effect (with autoCollapseOnDone,
 * collapse completed code and failed steps; preserve partial results and manual disclosure).
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Step } from "./Step";

afterEach(cleanup);

describe("Step · UX-1 autoCollapseOnDone", () => {
  it("run→done + autoCollapseOnDone → detail 自动收起(仍可点开)", () => {
    const { rerender } = render(
      <Step toolName="python_sandbox" status="run" autoCollapseOnDone>
        <pre>print(1)</pre>
      </Step>,
    );
    expect(screen.getByText("print(1)")).toBeTruthy();

    rerender(
      <Step toolName="python_sandbox" status="done" autoCollapseOnDone>
        <pre>print(1)</pre>
      </Step>,
    );
    expect(screen.queryByText("print(1)")).toBeNull();

    // Can still be opened manually.
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("print(1)")).toBeTruthy();
  });

  it("非代码类(无 autoCollapseOnDone) run→done 保持展开(结果型 detail)", () => {
    const { rerender } = render(
      <Step toolName="bi_query" status="run">
        <div>234 rows</div>
      </Step>,
    );
    rerender(
      <Step toolName="bi_query" status="done">
        <div>234 rows</div>
      </Step>,
    );
    expect(screen.getByText("234 rows")).toBeTruthy();
  });

  it("用户手动收起后, 后续状态迁移不再自动动它", () => {
    const { rerender } = render(
      <Step toolName="python_sandbox" status="run" autoCollapseOnDone>
        <pre>code</pre>
      </Step>,
    );
    // User manually collapses while streaming.
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText("code")).toBeNull();
    // User opens it again.
    fireEvent.click(screen.getByRole("button"));
    // Done transition must not override user intent (retain manually opened state).
    rerender(
      <Step toolName="python_sandbox" status="done" autoCollapseOnDone>
        <pre>code</pre>
      </Step>,
    );
    expect(screen.getByText("code")).toBeTruthy();
  });

  it.each(["fail", "part"] as const)("collapses failed running details with the %s glyph", (status) => {
    const { rerender } = render(
      <Step toolName="python_sandbox" status="run" autoCollapseOnDone>
        <pre>running code</pre>
      </Step>,
    );
    expect(screen.getByText("running code")).toBeTruthy();
    rerender(
      <Step toolName="python_sandbox" status={status} failed>
        <pre>ValueError: test failure</pre>
      </Step>,
    );
    expect(screen.queryByText("ValueError: test failure")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("ValueError: test failure")).toBeTruthy();
  });

  it("run→part(部分批失败)同样强制展开(失败/成功 lane 分解必须可见)", () => {
    const { rerender } = render(
      <Step toolName="bi_query" status="run">
        <div>lane breakdown</div>
      </Step>,
    );
    fireEvent.click(screen.getByRole("button")); // User collapses.
    expect(screen.queryByText("lane breakdown")).toBeNull();
    rerender(
      <Step toolName="bi_query" status="part">
        <div>lane breakdown</div>
      </Step>,
    );
    expect(screen.getByText("lane breakdown")).toBeTruthy();
  });
});
