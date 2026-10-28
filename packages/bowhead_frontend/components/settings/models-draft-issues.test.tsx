/**
 * Draft issues must appear on the **edit page**, before users enter the review dialog.
 *
 * A configuration without a primary model must show actionable guidance on the edit page;
 * clicking Review and Apply showed “请选择一个仍然存在的主模型” only on the next page, which had no control
 * to fix it, forcing users back.
 *
 * `localIssues` is calculated live by `useMemo` (recomputed on config changes); results always sat in memory but previously
 * rendered only inside review. The checks existed, but never spoke where needed.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it } from "vitest";

import { DraftIssueList } from "@/components/settings/models";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

function renderZh(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      {node}
    </NextIntlClientProvider>,
  );
}

const ISSUES = [
  { path: "providers", message: "至少需要一个模型来源。" },
  { path: "tiers.primary", message: "请选择一个仍然存在的主模型。" },
  { path: "tiers.primary_fallback.0", message: "备用模型 1 已失效。" },
];

describe("编辑页内联问题条", () => {
  it("模型分配区块只显示 tiers 相关的问题", () => {
    renderZh(<DraftIssueList issues={ISSUES} prefixes={["tiers"]} />);

    expect(screen.getByText("请选择一个仍然存在的主模型。")).toBeTruthy();
    expect(screen.getByText("备用模型 1 已失效。")).toBeTruthy();
    // Negative control: provider issues must not appear in assignments, separating guidance further from its control.
    expect(screen.queryByText("至少需要一个模型来源。")).toBeNull();
  });

  it("来源区块只显示 providers/kb/media/search 相关的问题", () => {
    renderZh(<DraftIssueList issues={ISSUES} prefixes={["providers", "kb", "media", "search"]} />);

    expect(screen.getByText("至少需要一个模型来源。")).toBeTruthy();
    expect(screen.queryByText("请选择一个仍然存在的主模型。")).toBeNull();
  });

  it("前缀匹配按路径分段，不能用裸字符串前缀", () => {
    // `tiers_other` is not inside `tiers`; match only `tiers` or the `tiers.` prefix.
    renderZh(<DraftIssueList issues={[{ path: "tiers_other", message: "不该出现" }]} prefixes={["tiers"]} />);

    expect(screen.queryByText("不该出现")).toBeNull();
  });

  it("没有命中的问题时不渲染任何东西（不留空框）", () => {
    const { container } = renderZh(<DraftIssueList issues={[]} prefixes={["tiers"]} />);

    expect(container.querySelector(".draft-issues")).toBeNull();
  });

  it("渲染成 role=alert，读屏能播报", () => {
    renderZh(<DraftIssueList issues={ISSUES} prefixes={["tiers"]} />);

    expect(screen.getByRole("alert")).toBeTruthy();
  });
});

describe("落位：编辑页两个区块都得真的挂上它", () => {
  /**
   * Component tests prove filtering, not presence on the edit page, precisely what users could not see.
   * Lock source mount sites here: removing either must fail.
   *
   * Limitation: proves a JSX call exists, not runtime visibility (conditional rendering may still hide it).
   * Verify conditional visibility in a browser in addition to this source contract.
   */
  const source = readFileSync(
    path.resolve(__dirname, "models.tsx"),
    "utf-8",
  );

  it("模型分配区块（②）挂了 tiers 那份", () => {
    expect(source).toContain('<DraftIssueList issues={localIssues} prefixes={["tiers"]} />');
  });

  it("模型来源区块（①）挂了 providers 那份", () => {
    expect(source).toContain('<DraftIssueList issues={localIssues} prefixes={scoped ? ["providers"] : ["providers", "kb", "media", "search"]} />');
  });

  it("positive control：扫描确实读到了 models.tsx", () => {
    // Without this, a wrong path makes the two checks above always fail on an empty string—or always pass after reversing toContain.
    expect(source.length).toBeGreaterThan(50_000);
    expect(source).toContain("function validateDraftLocally(");
  });
});
