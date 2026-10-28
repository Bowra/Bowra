/**
 * UX-1 (chat-solidify 2026-07-03) — automatically collapse sandbox code after completion.
 *
 * User observation: finished Python sandbox code kept occupying the screen. Cause: Step's
 * useState initializer evaluates only on mount: mounted while run → open=true, then done
 * never reevaluates it, leaving it open forever. Fix: state-transition effect (with autoCollapseOnDone,
 * collapse run→done; force open fail/part; respect the user after a manual toggle).
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

  it("run→fail 强制展开, 即使用户手动收起过(FailureAckCard 是唯一错误面)", () => {
    // code-review 2026-07-04 fix: forced failure expansion must override user-toggle.
    // After O5 removed retry/skip buttons + toast, FailureAckCard in the body is the sole error
    // explanation: users collapsed running code; failure is unexpected new information and must open.
    const { rerender } = render(
      <Step toolName="python_sandbox" status="run" autoCollapseOnDone>
        <pre>err detail</pre>
      </Step>,
    );
    // User manually collapses while streaming.
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText("err detail")).toBeNull();
    // Transition to fail → force open (override user-toggle).
    rerender(
      <Step toolName="python_sandbox" status="fail" autoCollapseOnDone>
        <pre>err detail</pre>
      </Step>,
    );
    expect(screen.getByText("err detail")).toBeTruthy();

    // Also force open without manual intervention (run → fail).
    cleanup();
    const r2 = render(
      <Step toolName="python_sandbox" status="run" autoCollapseOnDone>
        <pre>err2</pre>
      </Step>,
    );
    r2.rerender(
      <Step toolName="python_sandbox" status="fail" autoCollapseOnDone>
        <pre>err2</pre>
      </Step>,
    );
    expect(screen.getByText("err2")).toBeTruthy();
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
