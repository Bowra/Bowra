import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import type { ResultItemDTO } from "@/lib/results-api";
import zhMessages from "@/messages/zh-CN.json";

import {
  buildResultsExplorerRows,
  ResultsExplorer,
} from "./results-explorer";

function item(
  key: string,
  position: number,
  overrides: Partial<ResultItemDTO> = {},
): ResultItemDTO {
  const artifactId = key.replace(/^artifact:/, "");
  return {
    key,
    object_type: "artifact",
    title: `${artifactId}.csv`,
    primary_group: "evidence",
    secondary_group: "data",
    viewer_profile: "table",
    status: "ready",
    route: { type: "artifact", artifact_id: artifactId, default_mode: "preview" },
    capabilities: ["download"],
    relation_roles: [],
    execution: null,
    display_context: {
      source_role: "data_query",
      output_role: null,
      execution_label: null,
      occurred_at: "2026-08-25T06:03:21Z",
      duplicate_ordinal: null,
      duplicate_count: null,
    },
    position,
    version: null,
    artifact_metadata: {
      mime: "text/csv",
      size: 10,
      source: "bi_fetch_rows",
      kind: "tool_data",
      origin: "query",
      trust: "trusted",
      created_at: "2026-08-25T06:03:21Z",
      producer_kind: null,
    },
    ...overrides,
  };
}

const report = item("report:cv_1", 1, {
  object_type: "report",
  title: "尽调结论与报价建议",
  primary_group: "deliverables",
  secondary_group: null,
  viewer_profile: "report",
  route: { type: "report" },
  capabilities: ["read_report"],
  display_context: {
    source_role: "report",
    output_role: "main_deliverable",
    execution_label: null,
    occurred_at: null,
    duplicate_ordinal: null,
    duplicate_count: null,
  },
  artifact_metadata: null,
});

const defaultFilters = {
  lens: "all" as const,
  primary: "all" as const,
  secondary: "all" as const,
  technical_only: false,
};

function props(overrides: Partial<Parameters<typeof ResultsExplorer>[0]> = {}) {
  return {
    anchor: null,
    authorizedTotal: 3,
    checkingChanges: false,
    completedView: { query: overrides.query ?? "", filters: overrides.filters ?? defaultFilters },
    entryId: "results_entry_1",
    error: null,
    errorDirection: null,
    errorAnchorKey: null,
    expandedGroups: ["deliverables", "evidence"],
    filters: defaultFilters,
    focusKey: null,
    groupCounts: { deliverables: 1, evidence: 2, data: 1, code: 0, input: 1 },
    hasMore: false,
    hasPrevious: false,
    items: [
      report,
      item("artifact:data", 2),
      item("artifact:input", 3, { secondary_group: "input" }),
    ],
    loadedCount: 3,
    loading: false,
    loadingMore: false,
    loadingPrevious: false,
    matchedTotal: 3,
    newResultsCount: null,
    projectionChanged: false,
    projectionStatus: {
      report_relations: "ready" as const,
      execution_relations: "unavailable" as const,
      export_relations: "degraded" as const,
    },
    query: "",
    selectedKey: null,
    onAcceptChanges: vi.fn(),
    onAnchorChange: vi.fn(),
    onExpandedGroupsChange: vi.fn(),
    onFiltersChange: vi.fn(),
    onFocusKeyChange: vi.fn(),
    onLoadNext: vi.fn(),
    onLoadPrevious: vi.fn(),
    onQueryChange: vi.fn(),
    onRetry: vi.fn(),
    onSelect: vi.fn(),
    ...overrides,
  };
}

afterEach(cleanup);

describe("ResultsExplorer", () => {
  it("keeps the canonical hierarchy and secondary groups collapsed by default", () => {
    const rows = buildResultsExplorerRows({
      expandedGroups: ["deliverables", "evidence"],
      filters: defaultFilters,
      groupCounts: { deliverables: 1, evidence: 2, data: 1, code: 0, input: 1 },
      items: props().items,
      query: "",
    });

    expect(rows.map((row) => row.key)).toEqual([
      "group:deliverables",
      "report:cv_1",
      "group:evidence",
      "group:evidence:data",
      "group:evidence:code",
      "group:evidence:input",
    ]);
  });

  it("force-expands the selected row and hides unavailable technical filtering", () => {
    renderIntl(<ResultsExplorer {...props({ selectedKey: "artifact:data" })} />);

    expect(screen.getByRole("heading", { name: /交付内容/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /data\.csv/i })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByText("运行与技术输出")).not.toBeInTheDocument();
  });

  it("disambiguates duplicate outputs with public execution context", () => {
    renderIntl(<ResultsExplorer {...props({
      expandedGroups: ["deliverables", "evidence", "evidence:data"],
      items: [item("artifact:stdout-19", 19, {
        display_context: {
          source_role: "sandbox",
          output_role: "technical_output",
          execution_label: "最近一次分析",
          occurred_at: "2026-08-25T06:03:21Z",
          duplicate_ordinal: 19,
          duplicate_count: 19,
        },
      })],
    })} />);

    expect(screen.getByText(/最近一次分析 · 同名输出 19 \/ 19/)).toBeInTheDocument();
  });

  it("reports filtered totals truthfully and offers a real locate recovery", () => {
    const onFiltersChange = vi.fn();
    const onQueryChange = vi.fn();
    renderIntl(<ResultsExplorer {...props({
      authorizedTotal: 135,
      error: "results_anchor_not_in_view",
      errorDirection: "base",
      errorAnchorKey: "report:cv_1",
      selectedKey: "report:cv_1",
      filters: { ...defaultFilters, primary: "evidence" },
      matchedTotal: 12,
      onFiltersChange,
      onQueryChange,
    })} />);

    expect(screen.getByText("12 个匹配 · 已加载 3 个")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除条件并显示" }));
    expect(onQueryChange).toHaveBeenCalledWith("");
    expect(onFiltersChange).toHaveBeenCalledWith(defaultFilters);
  });

  it("shows a dedicated loading state instead of empty group shells", () => {
    renderIntl(<ResultsExplorer {...props({
      authorizedTotal: null,
      completedView: null,
      groupCounts: null,
      items: [],
      loadedCount: 0,
      loading: true,
      matchedTotal: null,
    })} />);

    expect(screen.getByRole("status")).toHaveTextContent("正在加载成果");
    expect(screen.queryByRole("heading", { name: /交付内容/ })).not.toBeInTheDocument();
  });

  it("keeps completed filter labels, hierarchy and counts together while a target is pending or failed", () => {
    const original = props({
      completedView: { query: "", filters: defaultFilters },
      filters: { ...defaultFilters, primary: "evidence", secondary: "input" },
      loading: true,
      hasMore: true,
    });
    const view = renderIntl(<ResultsExplorer {...original} />);
    expect(screen.getByRole("radio", { name: "全部" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "输入文件" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "输入文件" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("heading", { name: /交付内容/ })).toBeInTheDocument();
    expect(screen.getByText("共 3 个成果 · 已加载 3 个")).toBeInTheDocument();
    expect(screen.getByText("正在更新为：分析依据 · 输入文件")).toBeInTheDocument();
    expect(screen.getByText("仍显示：全部")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加载更多" })).toBeDisabled();

    view.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
      <ResultsExplorer {...original} loading={false} error="results_request_failed" errorDirection="base" />
    </NextIntlClientProvider>);
    expect(screen.getByText("未能更新为：分析依据 · 输入文件")).toBeInTheDocument();
    expect(screen.getByText("共 3 个成果 · 已加载 3 个")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "全部" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(original.onRetry).toHaveBeenCalledOnce();
  });

  it("explains an unsupported delivery receipt without suggesting regeneration", () => {
    renderIntl(<ResultsExplorer {...props({ items: [], loadedCount: 0, loading: false,
      error: "results_artifact_contract_unsupported", errorDirection: "base" })} />);
    expect(screen.getByText("这些文件的交付记录需要更新版本支持。请联系管理员更新 Bowhead，然后刷新结果。")).toBeInTheDocument();
  });

  it("does not call an unfiltered locate failure a filter mismatch or offer clearing nonexistent filters", () => {
    const onRetry = vi.fn();
    renderIntl(<ResultsExplorer {...props({
      error: "results_anchor_not_in_view", errorDirection: "base",
      errorAnchorKey: "artifact:missing", selectedKey: "artifact:missing", onRetry,
    })} />);
    expect(screen.getByText(/未能在成果列表中定位这一行/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "清除条件并显示" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试列表定位" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("does not infer membership from an unloaded window", () => {
    renderIntl(<ResultsExplorer {...props({
      authorizedTotal: 150, matchedTotal: 120, hasMore: true, hasPrevious: true,
      filters: { ...defaultFilters, primary: "evidence" }, selectedKey: "artifact:deep",
    })} />);
    expect(screen.getByText("120 个匹配 · 已加载 3 个")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "清除条件并显示" })).not.toBeInTheDocument();
    expect(screen.queryByText("当前条件下未能定位此对象")).not.toBeInTheDocument();
  });

  it("does not publish a zero total when the first request failed without a completed response", () => {
    const view = renderIntl(<ResultsExplorer {...props({
      completedView: null, authorizedTotal: null, matchedTotal: null, groupCounts: null,
      items: [], loadedCount: 0, error: "results_request_failed", errorDirection: "base",
    })} />);
    expect(view.container.querySelector(".results-explorer-head")).toHaveTextContent("暂时无法加载成果");
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
  });

  it("normalizes filter intent through callbacks and exposes explicit new-result acceptance", () => {
    const onFiltersChange = vi.fn();
    const onAcceptChanges = vi.fn();
    renderIntl(<ResultsExplorer {...props({
      newResultsCount: 4,
      onAcceptChanges,
      onFiltersChange,
    })} />);

    fireEvent.click(screen.getByRole("radio", { name: "输入文件" }));
    expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({
      primary: "evidence",
      secondary: "input",
    }));
    fireEvent.click(screen.getByRole("button", { name: "查看更新" }));
    expect(onAcceptChanges).toHaveBeenCalledTimes(1);
  });

  it("limits search by Unicode code points without splitting emoji", () => {
    const onQueryChange = vi.fn();
    renderIntl(<ResultsExplorer {...props({ onQueryChange })} />);

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "😀".repeat(121) },
    });
    const submitted = onQueryChange.mock.lastCall?.[0] as string;
    expect(Array.from(submitted)).toHaveLength(120);
    expect(submitted.endsWith("😀")).toBe(true);
  });

  it("uses bounded virtual DOM and force-mounts the selected object", () => {
    const items = Array.from({ length: 100 }, (_, index) => (
      item(`artifact:item-${index}`, index + 1)
    ));
    renderIntl(<ResultsExplorer {...props({
      authorizedTotal: 100,
      groupCounts: { deliverables: 0, evidence: 100, data: 100, code: 0, input: 0 },
      items,
      loadedCount: 100,
      matchedTotal: 100,
      query: "item",
      selectedKey: "artifact:item-99",
    })} />);

    expect(screen.getByRole("button", { name: /item-99\.csv/i })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem").length).toBeLessThan(80);
  });
});
