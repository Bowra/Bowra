import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import { ChartSourceLine } from "./chart-source-line";
import { SourcesSection } from "./sources";

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("viewer-safe source renderers", () => {
  it("SourcesSection renders safe detail and keeps unavailable slots generic", () => {
    const data = {
      id: "sources",
      kind: "sources",
      label: "数据来源",
      entries: [
        {
          source_alias: ALIAS,
          access_state: "linked_accessible",
          generic_type: "query_result",
          ordinal: 1,
          safe_label: "Approved label",
          label: "legacy label must not win",
          artifact_id: "art_raw_must_not_render",
        },
        {
          source_alias: "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
          access_state: "linked_unavailable",
          generic_type: "document",
          ordinal: 2,
          safe_label: "stale label",
          safe_query: "stale query",
        },
      ],
    } as Extract<ReportSection, { kind: "sources" }>;

    const { container } = renderIntl(
      <SourceAccessRegistryProvider>
        <SourcesSection data={data} />
      </SourceAccessRegistryProvider>,
    );

    expect(container.textContent).toContain("Approved label");
    expect(container.textContent).toContain("来源当前不可查看");
    expect(container.textContent).not.toContain("legacy label must not win");
    expect(container.textContent).not.toContain("art_raw_must_not_render");
    expect(container.textContent).not.toContain("stale label");
    expect(container.textContent).not.toContain("stale query");
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("SourcesSection renders descriptive (identity-less) entries as plain author text", () => {
    // 116 dogfood 1.0.95 P3: descriptive = peer-reported sources without access-gated objects (batch labels),
    // shown as plain author-text rows; ⚠ the stale unavailable+safe_label case above must still be stripped—
    // access_state distinguishes the two meanings; this case pairs with the previous one.
    const data = {
      id: "sources",
      kind: "sources",
      label: "数据来源",
      entries: [
        {
          source_alias: "source-ref-v1:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
          access_state: "descriptive",
          generic_type: "query_result",
          ordinal: 1,
          safe_label: "2026年7月各品牌退款额/营业额/退款率",
          safe_query: "2026年7月 各品牌 退款额 / 营业额 / 退款率",
        },
      ],
    } as Extract<ReportSection, { kind: "sources" }>;

    const { container } = renderIntl(
      <SourceAccessRegistryProvider>
        <SourcesSection data={data} />
      </SourceAccessRegistryProvider>,
    );

    expect(container.textContent).toContain("2026年7月各品牌退款额/营业额/退款率");
    expect(container.textContent).not.toContain("来源当前不可查看");
    expect(container.querySelector("[data-source-access='descriptive']")).not.toBeNull();
  });

  it("ChartSourceLine supports projected data_source and rechecks before opening an action", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ family: "text", mode: "text", text: "fresh" }),
    }));
    renderIntl(
      <SourceAccessRegistryProvider>
        <ChartSourceLine
          source={null}
          dataSource={{
            id: ALIAS,
            source_alias: ALIAS,
            access_state: "linked_accessible",
            generic_type: "generated_artifact",
            ordinal: 5,
            safe_label: "Approved chart input",
            safe_url: "/api/v1/report-artifacts/art-safe",
            safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
          }}
        />
      </SourceAccessRegistryProvider>,
    );

    expect(screen.getByText("Approved chart input")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getAllByText(/正在确认来源/).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getByRole("link", { name: /打开来源/ })).toBeInTheDocument());
  });

  it("ChartSourceLine never renders legacy raw SourceRef fields", () => {
    const { container } = renderIntl(
      <SourceAccessRegistryProvider>
        <ChartSourceLine
          source={{
            id: "art_secret",
            tool: "bi_query",
            query: "secret query",
            artifact_id: "art_secret",
          }}
        />
      </SourceAccessRegistryProvider>,
    );
    expect(container.textContent).toContain("来源当前不可查看");
    expect(container.textContent).not.toContain("art_secret");
    expect(container.textContent).not.toContain("secret query");
  });
});
