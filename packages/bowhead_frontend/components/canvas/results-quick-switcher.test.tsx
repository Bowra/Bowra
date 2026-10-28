import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ResultItemDTO } from "@/lib/results-api";

import {
  groupQuickResults,
  ResultsQuickSwitcher,
} from "./results-quick-switcher";

function item(
  key: string,
  overrides: Partial<ResultItemDTO> = {},
): ResultItemDTO {
  const artifactId = key.replace(/^artifact:/, "");
  return {
    key,
    object_type: "artifact",
    title: `Result ${artifactId}`,
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
    position: 1,
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

const report = item("report:cv_1", {
  object_type: "report",
  title: "尽调结论与报价建议",
  primary_group: "deliverables",
  secondary_group: null,
  viewer_profile: "report",
  route: { type: "report" },
  capabilities: ["read_report"],
  display_context: {
    source_role: "report",
    output_role: null,
    execution_label: null,
    occurred_at: null,
    duplicate_ordinal: null,
    duplicate_count: null,
  },
  artifact_metadata: null,
});

type HarnessProps = Partial<Parameters<typeof ResultsQuickSwitcher>[0]>;

function Harness(props: HarnessProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  return (
    <div className="v2-workspace">
      <aside className="v2-ws-left" />
      <div className="v2-ws-right-head">
        <div>
          <button ref={triggerRef} type="button">Results trigger</button>
          <ResultsQuickSwitcher
            activeKey="report:cv_1"
            authorizedTotal={135}
            compact={false}
            currentKey="report:cv_1"
            error={null}
            hasMore
            items={[report, item("artifact:a"), item("artifact:b")]}
            loading={false}
            matchedTotal={135}
            open
            query=""
            recentKeys={["artifact:a"]}
            triggerRef={triggerRef}
            onBrowseAll={vi.fn()}
            onClose={vi.fn()}
            onQueryChange={vi.fn()}
            onRetry={vi.fn()}
            onSelect={vi.fn()}
            {...props}
          />
        </div>
        <div className="r-actions" />
      </div>
      <main className="results-workspace-body" />
    </div>
  );
}

afterEach(cleanup);

describe("ResultsQuickSwitcher projection", () => {
  it("deduplicates by stable key and enforces 10 / 8 object limits", () => {
    const items = Array.from({ length: 12 }, (_, index) => item(`artifact:${index}`));
    items.splice(2, 0, items[0]);
    expect(groupQuickResults({
      currentKey: null,
      items,
      query: "",
      recentKeys: [],
    }).flatMap((section) => section.items)).toHaveLength(10);
    expect(groupQuickResults({
      currentKey: null,
      items,
      query: "needle",
      recentKeys: [],
    }).flatMap((section) => section.items)).toHaveLength(8);
  });

  it("uses the frozen report/current/recent/direct/deliverable priority", () => {
    const sections = groupQuickResults({
      currentKey: "artifact:current",
      recentKeys: ["artifact:recent", "artifact:direct"],
      query: "",
      items: [
        report,
        item("artifact:current"),
        item("artifact:recent"),
        item("artifact:direct", { relation_roles: ["report_chart_data"] }),
        item("artifact:related", { relation_roles: ["report_detail_table"] }),
        item("artifact:delivery", { primary_group: "deliverables", secondary_group: null }),
      ],
    });
    expect(sections.map((section) => section.id)).toEqual([
      "report",
      "current",
      "recent",
      "current_report",
      "deliverables",
    ]);
    expect(sections.find((section) => section.id === "recent")?.items.map(
      (entry) => entry.key,
    )).toEqual(["artifact:recent", "artifact:direct"]);
  });
});

const originalMatchMedia = window.matchMedia;

afterEach(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: originalMatchMedia });
});

function mockPointer(kind: "coarse" | "fine") {
  const matchMedia = (query: string) => ({
    matches: query === "(pointer: coarse)" ? kind === "coarse" : false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  });
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: matchMedia });
}

describe("ResultsQuickSwitcher combobox", () => {
  it("renders dialog + combobox + owned listbox and focuses search on desktop", async () => {
    renderIntl(<Harness />);
    const dialog = screen.getByRole("dialog", { name: "成果" });
    const combobox = screen.getByRole("combobox", { name: "搜索全部成果" });
    const listbox = screen.getByRole("listbox", { name: "快速切换结果" });

    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(combobox).toHaveAttribute("aria-controls", listbox.id);
    expect(combobox).not.toHaveAttribute("aria-activedescendant");
    expect(screen.getAllByRole("option")).toHaveLength(3);
    await waitFor(() => expect(document.activeElement).toBe(combobox));
  });

  it("keeps DOM focus in the combobox and selects only an active descendant", async () => {
    const onSelect = vi.fn();
    renderIntl(<Harness onSelect={onSelect} />);
    const combobox = screen.getByRole("combobox");
    await waitFor(() => expect(document.activeElement).toBe(combobox));

    fireEvent.keyDown(combobox, { key: "Enter" });
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(combobox, { key: "ArrowDown" });
    expect(document.activeElement).toBe(combobox);
    expect(combobox).toHaveAttribute("aria-activedescendant");
    fireEvent.keyDown(combobox, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(report);
  });

  it("does not close, select, navigate, or query while IME is composing", () => {
    const onClose = vi.fn();
    const onQueryChange = vi.fn();
    const onSelect = vi.fn();
    renderIntl(
      <Harness
        onClose={onClose}
        onQueryChange={onQueryChange}
        onSelect={onSelect}
      />,
    );
    const combobox = screen.getByRole("combobox");
    fireEvent.compositionStart(combobox);
    fireEvent.change(combobox, { target: { value: "营收" } });
    fireEvent.keyDown(combobox, { key: "ArrowDown", isComposing: true });
    fireEvent.keyDown(combobox, { key: "Enter", isComposing: true });
    fireEvent.keyDown(combobox, { key: "Escape", isComposing: true });

    expect(onQueryChange).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(combobox).not.toHaveAttribute("aria-activedescendant");

    fireEvent.compositionEnd(combobox, { data: "营收" });
    expect(onQueryChange).toHaveBeenCalledWith("营收");
  });

  it("closes on Escape and returns focus to the results trigger", async () => {
    function Toggle() {
      const [open, setOpen] = useState(true);
      return <Harness open={open} onClose={() => setOpen(false)} />;
    }
    renderIntl(<Toggle />);
    const combobox = screen.getByRole("combobox");
    await waitFor(() => expect(document.activeElement).toBe(combobox));
    fireEvent.keyDown(combobox, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Results trigger" })));
  });

  it("stays anchored in the workspace, makes the background inert, and closes on a press outside", () => {
    const onClose = vi.fn();
    const { container } = renderIntl(<Harness onClose={onClose} />);
    const dialog = screen.getByRole("dialog", { name: "成果" });
    expect(dialog.parentElement?.closest(".v2-ws-right-head")).not.toBeNull();
    expect(container.querySelector(".results-workspace-body")).toHaveAttribute("data-floating-ui-inert");

    fireEvent.pointerDown(screen.getByRole("combobox"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.pointerDown(container.querySelector(".results-quick-scrim")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("opens as the shared bottom sheet on phones instead of covering the top bar", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 639.98px)",
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    try {
      const onClose = vi.fn();
      const { container } = renderIntl(<Harness compact onClose={onClose} />);
      const dialog = await screen.findByRole("dialog", { name: "成果" });
      await waitFor(() => expect(dialog).toHaveAttribute("data-dialog-presentation", "sheet"));
      expect(dialog.parentElement).toHaveClass("ui-dialog-sheet");
      expect(dialog.closest(".v2-ws-right-head")).toBeNull();
      expect(container.querySelector(".results-quick-scrim")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "关闭成果导航" }));
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("focuses the heading on a coarse pointer without focusing search", async () => {
    mockPointer("coarse");
    renderIntl(<Harness />);
    const heading = screen.getByRole("heading", { name: "成果" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(document.activeElement).not.toBe(screen.getByRole("combobox"));
  });

  it("focuses search on a fine pointer even in the compact layout", async () => {
    mockPointer("fine");
    renderIntl(<Harness compact />);
    const combobox = screen.getByRole("combobox", { name: "搜索全部成果" });
    expect(document.querySelector(".results-quick--compact")).not.toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(combobox));
  });

  it("keeps zero-state recovery actions outside the listbox", () => {
    const onBrowseAll = vi.fn();
    renderIntl(
      <Harness
        items={[]}
        query="missing"
        matchedTotal={0}
        hasMore={false}
        onBrowseAll={onBrowseAll}
      />,
    );
    const listbox = screen.getByRole("listbox");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    const clear = screen.getByRole("button", { name: "清除搜索" });
    const browse = screen.getByRole("button", { name: "清除搜索并浏览全部成果" });
    expect(clear.closest("[role=listbox]")).toBeNull();
    expect(browse.closest("[role=listbox]")).toBeNull();
    fireEvent.click(browse);
    expect(onBrowseAll).toHaveBeenCalledWith("");
    expect(listbox).not.toContainElement(browse);
  });

  it("clears a fully satisfied search before browsing the complete set", () => {
    const onBrowseAll = vi.fn();
    renderIntl(
      <Harness
        items={[item("artifact:match")]}
        query="match"
        matchedTotal={1}
        hasMore={false}
        onBrowseAll={onBrowseAll}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "浏览全部成果（135）" }));
    expect(onBrowseAll).toHaveBeenCalledWith("");
  });

  it("does not invent a total before the authorized count is known", () => {
    renderIntl(<Harness authorizedTotal={null} items={[]} loading />);

    expect(screen.getByRole("button", { name: "浏览全部成果" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /（0）/ })).toBeNull();
  });
});
