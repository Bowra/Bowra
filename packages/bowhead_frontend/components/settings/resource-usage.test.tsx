import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ResourceUsageResponse } from "@/lib/resource-usage";

import { ResourceUsageDetails, UsageSummaryInline } from "./resource-usage";

const USAGE: ResourceUsageResponse = {
  kind: "resource_usage",
  schema_version: 1,
  resource: {
    kind: "skill",
    id: "skill_1",
    display_name: "渠道核对",
    revision: "sha256:abc",
  },
  summary: {
    current_agent_count: 1,
    running_task_count: 1,
    draft_agent_count: 0,
    historical_agent_count: 0,
    scheduled_binding_count: 0,
    restricted_count: 2,
    completeness: "partial",
    reason_codes: ["tool_revision_unrecoverable"],
  },
  actual_usage_availability: "unavailable",
  completeness: "partial",
  reason_codes: ["tool_revision_unrecoverable"],
  groups: {
    current: [
      {
        kind: "agent",
        id: "agent_1",
        agent_id: "agent_1",
        display_name: "渠道分析师",
        relation: "current",
        readiness: "ready",
        subject_ref: "rev_1",
        evidence_level: "exact",
        resource_revision: "sha256:abc",
        href: "/agents/detail/?id=agent_1",
        status: "enabled",
      },
    ],
    running: [],
    draft: [],
    history: [],
  },
  next_cursors: {},
};

afterEach(cleanup);

describe("resource usage visibility", () => {
  it("renders a compact list summary and opens the detail action", () => {
    const onOpen = vi.fn();
    renderIntl(<UsageSummaryInline summary={USAGE.summary} onOpen={onOpen} />);

    fireEvent.click(screen.getByRole("button", { name: /1 个当前智能体/ }));

    expect(onOpen).toHaveBeenCalledOnce();
    expect(screen.getByText(/1 个运行中任务/)).toBeTruthy();
  });

  it("does not present an incomplete zero-count summary as unused", () => {
    renderIntl(
      <UsageSummaryInline
        summary={{
          current_agent_count: 0,
          running_task_count: 0,
          draft_agent_count: 0,
          historical_agent_count: 0,
          scheduled_binding_count: 0,
          restricted_count: 1,
          completeness: "partial",
          reason_codes: ["tool_revision_unrecoverable"],
        }}
      />,
    );

    expect(screen.getByText("应用位置可能不完整")).toBeTruthy();
    expect(screen.queryByText("当前未被智能体使用")).toBeNull();
  });

  it("discloses partial and restricted results and deep-links only visible items", () => {
    const onNavigate = vi.fn();
    renderIntl(
      <ResourceUsageDetails
        data={USAGE}
        loading={false}
        error={null}
        onNavigate={onNavigate}
      />,
    );

    expect(screen.getByText(/结果可能不完整/)).toBeTruthy();
    expect(screen.getByText(/另有 2 个受限对象/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /渠道分析师/ }));
    expect(onNavigate).toHaveBeenCalledWith("/agents/detail/?id=agent_1");
  });

  it("does not present a failed projection query as unused", () => {
    renderIntl(<ResourceUsageDetails data={null} loading={false} error="boom" />);

    expect(screen.getByRole("alert").textContent).toContain("依赖信息暂不可用");
    expect(screen.queryByText("当前可见范围内没有依赖关系。")).toBeNull();
  });

  it("does not repeat four empty groups below the overall empty state", () => {
    const empty: ResourceUsageResponse = {
      ...USAGE,
      summary: {
        ...USAGE.summary,
        current_agent_count: 0,
        running_task_count: 0,
        restricted_count: 0,
        completeness: "complete",
        reason_codes: [],
      },
      completeness: "complete",
      reason_codes: [],
      groups: { current: [], running: [], draft: [], history: [] },
    };
    renderIntl(<ResourceUsageDetails data={empty} loading={false} error={null} />);

    expect(screen.getByText("当前可见范围内没有依赖关系。")).toBeTruthy();
    expect(screen.queryByText("当前使用")).toBeNull();
    expect(screen.queryByText("无")).toBeNull();
  });
});
