import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentBlock } from "./AgentBlock";
import { AgentTrace } from "./AgentTrace";
import { Step } from "./Step";
import { TurnRunningProvider } from "./turn-running";

afterEach(() => cleanup());

/**
 * chat-ux-2026-08 — while the running band is present, it alone owns live progress.
 *
 * Observed on 116 (1.0.97 plan turn, same DOM frame): band at y=9877 says “正在想下一步 · 第 3 步 · 22 秒”,
 * peer ledger at y=9597 repeats **the same task text**, with a separate `⟳` spinning in each place.
 * User's exact words: “之前的卡片也有，现在的卡片也有，我以为是一起的”.
 *
 * ★ Root cause: invariant inventory §2.5's decision was applied too broadly; it preserves peer **terminal-state** disclosure
 * (fail / bail / outcomeReason / source footer), not running-state task rows and spinners.
 * These gates must therefore be **paired**: lock both what is suppressed and what survives; testing only half lets the next person
 * suppress the entire block (reintroducing the bug §2.5 fixed).
 * User decision: **retain prose**.
 */
describe("AgentBlock · 运行带接管活进度时的取舍", () => {
  const LONG_TASK = "查询2025年天津各门店的营业额与退款率数据，按门店维度汇总，输出门店经营基础指标表";

  function renderPeer(bandOwnsLiveStatus: boolean, status: "run" | "fail" = "run") {
    return renderIntl(
      <TurnRunningProvider running bandOwnsLiveStatus={bandOwnsLiveStatus}>
        <AgentBlock
          agent="data-analyst"
          task={LONG_TASK}
          status={status}
          outcomeReason={status === "fail" ? "上游超时" : undefined}
          open
        >
          <div>peer 散文</div>
        </AgentBlock>
      </TurnRunningProvider>,
    );
  }

  it("★ 带在场：任务行与转圈都不再自渲（消掉与带的重复）", () => {
    const { container } = renderPeer(true);
    expect(container.querySelector(".agb-task")).toBeNull();
    expect(screen.queryByText("当前任务")).toBeNull();
    expect(container.querySelector(".agb-head .glyph, .agb-head [class*='spin']")).toBeNull();
  });

  it("★ 带在场：散文与终态事实照渲（这半边丢了就是退回 §2.5 的 bug）", () => {
    const { container } = renderPeer(true);
    expect(screen.getByText("peer 散文")).toBeTruthy();      // Retention explicitly approved by the user
    cleanup();
    const fail = renderPeer(true, "fail");
    expect(screen.getByText("上游超时")).toBeTruthy();        // Terminal-state reason
    expect(fail.container.querySelector(".fail-note")).toBeTruthy();
    expect(container).toBeTruthy();
  });

  it("正对照：带不在场（归档/重放）时任务行与转圈必须回来", () => {
    const { container } = renderPeer(false);
    expect(container.querySelector(".agb-task")).toBeTruthy();
    expect(screen.getByText("当前任务")).toBeTruthy();
  });

  it("正对照：带在场但该 peer 已终态 → 不属于「活进度」，任务行照渲", () => {
    const { container } = renderPeer(true, "fail");
    expect(container.querySelector(".agb-task")).toBeTruthy();
  });
});

describe("AgentBlock", () => {
  it("shows the full long task in the expanded body", () => {
    const task = "调研飞书反馈里表面问题背后的源头，区分真实根因、影响路径、正确修复方案和验证闭环";

    renderIntl(
      <AgentBlock agent="data-analyst" task={task} status="run" open>
        <div>child trace</div>
      </AgentBlock>,
    );

    expect(screen.getByText("当前任务")).toBeTruthy();
    expect(screen.getAllByText(task).length).toBeGreaterThanOrEqual(1);
  });

  it("uses the configured name in both the block header and provenance", () => {
    renderIntl(
      <AgentTrace
        steps={[
          {
            stepId: "step_1",
            parentStepId: null,
            concurrencyGroupId: null,
            stepType: "tool_run",
            toolName: "bi_query",
            headerZh: "查询数据",
            state: "done",
            subagentId: "sub_sales",
          },
        ]}
        peerNames={{ sub_sales: "sales-expert" }}
        peerDisplayNames={{ sub_sales: "销售专家" }}
        peerProse={{}}
      />,
    );

    expect(screen.getAllByText("销售专家")).toHaveLength(2);
    expect(screen.queryByText("专家")).toBeNull();
    expect(screen.queryByText("sales-expert")).toBeNull();
  });

  it("falls back to the raw custom slug and escapes an HTML-like configured name", () => {
    const { container } = renderIntl(
      <>
        <AgentBlock agent="sales-expert" status="done" open />
        <AgentBlock
          agent="sales-expert"
          displayName={'<img src="x" onerror="alert(1)">'}
          status="done"
          open
        />
      </>,
    );
    expect(screen.getByText("sales-expert")).toBeTruthy();
    expect(screen.getByText('<img src="x" onerror="alert(1)">')).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });

  it("keeps a peer terminal reason visible when its block is folded or expanded", () => {
    renderIntl(
      <AgentBlock
        agent="financial-analyst"
        task="检查利润率"
        status="fail"
        outcomeReason="上游数据源超时"
      />,
    );

    const agentButton = screen.getByRole("button", { name: /财务.*检查利润率/ });
    expect(screen.getByText("上游数据源超时")).toBeTruthy();

    fireEvent.click(agentButton);

    expect(screen.getByText("结果说明")).toBeTruthy();
    expect(screen.getByText("上游数据源超时")).toBeTruthy();
  });

  it.each([
    [
      "zh-CN" as const,
      "这个分析智能体执行超时，请缩小分析范围后重试。",
      "查看技术详情",
    ],
    [
      "en-US" as const,
      "This analysis agent timed out. Narrow the analysis scope and try again.",
      "View technical details",
    ],
  ])(
    "localizes peer failures and discloses only typed diagnostics in %s",
    (locale, friendlyMessage, disclosureLabel) => {
      const hostileRaw = "Authorization: Bearer provider-raw-secret";
      const { container } = renderIntl(
        <AgentTrace
          steps={[]}
          peerNames={{ peer_1: "compute" }}
          peerProse={{}}
          peerTerminalById={{
            peer_1: {
              status: "failed",
              failureReason: "peer_execution_timeout",
              failureDiagnostics: {
                schemaVersion: 1,
                disclosure: "authenticated",
                code: "peer_execution_timeout",
                diagnosticCode: "chat_peer_failed",
                occurrenceId: "0123456789abcdef0123456789abcdef",
                turnId: "t_peer_1",
                safeDetail: "phase=peer; failure_class=timeout",
                retryable: true,
              },
            },
          }}
        />,
        { locale },
      );

      expect(container.textContent).toContain(friendlyMessage);
      expect(container.textContent).not.toContain(hostileRaw);
      fireEvent.click(screen.getByRole("button"));
      expect(container.textContent).toContain(disclosureLabel);
      expect(container.textContent).toContain(
        "0123456789abcdef0123456789abcdef",
      );
      expect(container.textContent).toContain(
        "phase=peer; failure_class=timeout",
      );
    },
  );

  it.each([
    [
      "zh-CN" as const,
      "Power BI 返回的结果不完整，系统已停止使用这份数据以避免得出错误结论；请缩小查询范围后重试。",
      "查看技术详情",
    ],
    [
      "en-US" as const,
      "Power BI returned an incomplete result. Bowhead stopped using it to avoid an incorrect conclusion. Narrow the query and try again.",
      "View technical details",
    ],
  ])(
    "localizes a Power BI step failure in %s and preserves safe diagnostics",
    (locale, friendlyMessage, disclosureLabel) => {
      const hostileRaw =
        "Authorization: Bearer raw-secret from provider response";
      const { container } = renderIntl(
        <AgentTrace
          steps={[
            {
              stepId: "step_pbi",
              parentStepId: null,
              concurrencyGroupId: null,
              stepType: "tool_run",
              toolName: "bi_query",
              headerZh: "查询 Power BI",
              state: "failed",
              error: {
                kind: "transient",
                message: hostileRaw,
                code: "pbi_query_result_incomplete",
                diagnostics: {
                  schemaVersion: 1,
                  disclosure: "authenticated",
                  code: "pbi_query_result_incomplete",
                  diagnosticCode: "powerbi_query_failure",
                  safeDetail: "phase=parse; failure_class=limit",
                  retryable: false,
                },
              },
            },
          ]}
          peerNames={{}}
          peerDisplayNames={{}}
          peerProse={{}}
        />,
        { locale },
      );

      expect(screen.getByText(friendlyMessage)).toBeTruthy();
      expect(screen.getByText(disclosureLabel)).toBeTruthy();
      expect(
        screen.getByText("pbi_query_result_incomplete"),
      ).toBeTruthy();
      expect(container.textContent).not.toContain(hostileRaw);
      expect(container.textContent).not.toContain("raw-secret");
    },
  );

  it.each([2, 3])(
    "renders %i peers as neutral participants without inferred topology",
    (count) => {
      const peerIds = Array.from({ length: count }, (_, index) => `sub_${index + 1}`);
      const peerNames = Object.fromEntries(
        peerIds.map((peerId, index) => [peerId, `agent-${index + 1}`]),
      );
      const peerDisplayNames = Object.fromEntries(
        peerIds.map((peerId, index) => [peerId, `Agent ${index + 1}`]),
      );
      const { container } = renderIntl(
        <AgentTrace
          steps={peerIds.map((peerId, index) => ({
            stepId: `step_${index + 1}`,
            parentStepId: null,
            concurrencyGroupId: null,
            stepType: "tool_run",
            toolName: "bi_query",
            headerZh: "查询数据",
            state: "done",
            subagentId: peerId,
          }))}
          peerNames={peerNames}
          peerDisplayNames={peerDisplayNames}
          peerProse={{}}
        />,
      );

      expect(screen.getByText(`${count} 个智能体参与`)).toBeTruthy();
      expect(container.textContent).not.toMatch(/Debate|Judge|辩论|评议收口|并行|同时分析|→|‖/);
    },
  );

  it("renders a historical retired debate profile as a neutral Agent", () => {
    const { container } = renderIntl(
      <AgentBlock agent="debate" status="done" task="历史执行记录" />,
    );

    expect(container.textContent).not.toMatch(/Debate|辩论|评议/);
    expect(container.textContent).toContain("专家");
    expect(container.querySelector(".ag-debate")).toBeNull();
  });

  it("keeps Agent and Step disclosures independent and unmounts closed detail", () => {
    renderIntl(
      <AgentBlock
        agent="financial-analyst"
        task="盈利质量"
        status="done"
      >
        <Step toolName="bi_query" label="毛利桥" status="done" open>
          <div>毛利桥明细</div>
        </Step>
      </AgentBlock>,
    );

    const agentButton = screen.getByRole("button", { name: /财务.*盈利质量/ });
    expect(agentButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("毛利桥明细")).toBeNull();

    fireEvent.click(agentButton);

    const stepButton = screen.getByRole("button", { name: /查询数据.*毛利桥/ });
    expect(agentButton.getAttribute("aria-expanded")).toBe("true");
    expect(stepButton.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("毛利桥明细")).toBeTruthy();

    fireEvent.click(stepButton);
    expect(stepButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("毛利桥明细")).toBeNull();

    fireEvent.click(agentButton);
    expect(agentButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: /查询数据.*毛利桥/ })).toBeNull();
  });
});
