import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { getAgentKnowledgeBaseSources, getAgentKnowledgeSources } from "@/lib/agents-api";
import { useKnowledgeSummary } from "./agent-config-v2";

vi.mock("@/lib/agents-api", () => ({ getAgentKnowledgeSources: vi.fn(), getAgentKnowledgeBaseSources: vi.fn() }));

const MANUAL = "kbase_" + "a".repeat(24);
const SPACE_DEFAULT = "kbase_" + "2".repeat(24);

function Summary({ value }: { value: unknown }) {
  const summary = useKnowledgeSummary("osa_1", value);
  return <><p data-testid="line">{summary.line}</p><p data-testid="names">{summary.names.join(" | ")}</p></>;
}

describe("useKnowledgeSummary", () => {
  afterEach(cleanup);
  it("names selected knowledge bases, a Space default by its Space, and unusable ones by position", async () => {
    vi.mocked(getAgentKnowledgeSources).mockResolvedValue({ enabled: true, available: true, items: [], selected_items: [], total: 0, max_selected: 64, top_k_min: 1, top_k_max: 50 });
    vi.mocked(getAgentKnowledgeBaseSources).mockResolvedValue({
      enabled: true,
      state: "ready",
      max_selected: 32,
      selected_items: [],
      groups: [{
        home: "space", home_id: "ws_store", name: "门店", items: [
          { id: SPACE_DEFAULT, is_default: true, home: "space", home_id: "ws_store", name: null, audience: "space", audience_level: "edit" },
          { id: MANUAL, is_default: false, home: "space", home_id: "ws_store", name: "门店运营手册", audience: "space", audience_level: "use" },
        ],
      }],
    });
    renderIntl(<Summary value={{ schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [MANUAL, SPACE_DEFAULT, "kbase_" + "f".repeat(24)] }} />);
    await waitFor(() => expect(screen.getByTestId("names").textContent).toBe("知识库「门店运营手册」 | 知识库「门店」 | 第 3 个知识库现在不可用"));
    expect(screen.getByTestId("line").textContent).toContain("指定了 3 个知识库");
  });
});
