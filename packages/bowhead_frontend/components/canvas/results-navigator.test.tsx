import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResultsNavigator } from "./results-navigator";
import type { ArtifactItem } from "./data-tab-files";
import { projectResultsWorkspace } from "@/lib/results-workspace-model";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

afterEach(() => cleanup());

const artifacts: ArtifactItem[] = [
  {
    id: "art_output",
    filename: "brief.html",
    mime: "text/html",
    size: 120,
    source: "agent",
    created_at: "2026-07-30T00:00:00Z",
    preview_available: true,
    kind: "deliverable",
  },
  {
    id: "art_upload",
    filename: "customer-input.csv",
    mime: "text/csv",
    size: 240,
    source: "upload",
    created_at: "2026-07-30T00:00:00Z",
    preview_available: true,
    kind: "user_upload",
  },
];

const labels = {
  reportEditable: "report_meta",
  reportReadonly: "report_readonly_meta",
  codeRunning: "running",
  codeTitle: "code",
  codeLastRun: "last_run",
  sourceUpload: "source_upload",
  sourceGenerated: "source_generated",
  sourceExport: "source_export",
  sourceWeb: "source_web",
  sourceSandbox: "source_sandbox",
  sourceData: "source_data",
  exportVersions: (count: number) => `${count} versions`,
  formatDate: (value: string) => value,
};

function itemsFor({
  artifactItems = artifacts,
  codeAvailable = true,
  codeRunning = false,
  reportAvailable = true,
  reportEditable = true,
  reportOnly = false,
}: {
  artifactItems?: ArtifactItem[];
  codeAvailable?: boolean;
  codeRunning?: boolean;
  reportAvailable?: boolean;
  reportEditable?: boolean;
  reportOnly?: boolean;
} = {}) {
  return projectResultsWorkspace({
    artifacts: artifactItems,
    report: reportAvailable
      ? { canvasId: "canvas_1", title: "report", editable: reportEditable }
      : null,
    code: {
      sessionId: "session_1",
      available: codeAvailable,
      running: codeRunning,
    },
    labels,
    readOnly: !reportEditable,
    reportOnly,
  });
}

describe("ResultsNavigator", () => {
  it("offers inline exact file choices without capturing input focus or closing the view", async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<><input aria-label="Chat input" autoFocus /><ResultsNavigator
      items={itemsFor({ reportAvailable: false, codeAvailable: false })}
      open query="" selection={{ type: "none" }} presentation="inline"
      onClose={onClose} onQueryChange={() => {}} onSelect={onSelect}
    /></>);
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    expect(screen.getByRole("textbox", { name: "Chat input" })).toHaveFocus();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /customer-input.csv/ }));
    expect(onSelect.mock.calls[0][0].route).toEqual({ type: "artifact", artifactId: "art_upload", mode: "preview" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("groups stable objects and selects the exact artifact ID", () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(
      <ResultsNavigator
        items={itemsFor()}
        open
        query=""
        selection={{ type: "report" }}
        onClose={onClose}
        onQueryChange={() => {}}
        onSelect={onSelect}
      />,
    );

    const navigator = screen.getByRole("navigation", {
      name: "navigator_label",
    });
    expect(within(navigator).getByText("deliverables")).toBeInTheDocument();
    expect(within(navigator).getByText("evidence")).toBeInTheDocument();
    expect(within(navigator).getByText("brief.html")).toBeInTheDocument();
    expect(
      within(navigator).getByText("customer-input.csv"),
    ).toBeInTheDocument();
    expect(within(navigator).getByText("</>")).toBeInTheDocument();
    expect(within(navigator).getByText("last_run")).toBeInTheDocument();

    fireEvent.click(
      within(navigator).getByRole("button", {
        name: /customer-input\.csv/,
      }),
    );
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({
      key: "artifact:art_upload",
      route: { type: "artifact", artifactId: "art_upload", mode: "preview" },
    }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("uses current-turn running status and hides Code in report-only mode", () => {
    const { rerender } = render(
      <ResultsNavigator
        items={itemsFor({ artifactItems: [], codeRunning: true })}
        open
        query=""
        selection={{ type: "code" }}
        onClose={() => {}}
        onQueryChange={() => {}}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("running")).toBeInTheDocument();

    rerender(
      <ResultsNavigator
        items={itemsFor({
          artifactItems: [],
          codeRunning: true,
          reportEditable: false,
          reportOnly: true,
        })}
        open
        query=""
        selection={{ type: "report" }}
        onClose={() => {}}
        onQueryChange={() => {}}
        onSelect={() => {}}
      />,
    );
    expect(screen.queryByText("code")).not.toBeInTheDocument();
    expect(screen.getByText("report_readonly_meta")).toBeInTheDocument();
  });

  it("focuses search on open and keeps the scrim out of the tab order", async () => {
    const onClose = vi.fn();
    const { container } = render(
      <ResultsNavigator
        items={itemsFor()}
        open
        query=""
        selection={{ type: "report" }}
        onClose={onClose}
        onQueryChange={() => {}}
        onSelect={() => {}}
      />,
    );

    const search = screen.getByRole("searchbox", { name: "search_results" });
    const dialog = screen.getByRole("dialog", { name: "navigator_label" });
    await waitFor(() => expect(search).toHaveFocus());
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const scrim = container.querySelector(
      ".results-nav-scrim",
    ) as HTMLButtonElement;
    expect(scrim.tabIndex).toBe(-1);
    expect(scrim).toHaveAttribute("aria-hidden", "true");

    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(first).toHaveFocus();
    first.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps a pinned navigator open while selecting and exposes an unpin control", () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    const onPinnedChange = vi.fn();
    const { container } = render(
      <ResultsNavigator
        items={itemsFor()}
        open
        query=""
        selection={{ type: "report" }}
        presentation="pinned"
        canPin
        onPinnedChange={onPinnedChange}
        onClose={onClose}
        onQueryChange={() => {}}
        onSelect={onSelect}
      />,
    );

    expect(container.querySelector(".results-nav-scrim")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /customer-input\.csv/ }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({
      key: "artifact:art_upload",
    }));
    expect(onClose).not.toHaveBeenCalled();

    const unpin = screen.getByRole("button", { name: "unpin_navigator" });
    expect(unpin).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(unpin);
    expect(onPinnedChange).toHaveBeenCalledWith(false);
  });

  it("uses an in-flow mobile list without dialog or scrim semantics", () => {
    const { container } = render(
      <ResultsNavigator
        items={itemsFor({ codeAvailable: false, reportEditable: false })}
        open
        query=""
        selection={{ type: "report" }}
        presentation="mobile"
        onClose={() => {}}
        onQueryChange={() => {}}
        onSelect={() => {}}
      />,
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(container.querySelector(".results-nav-scrim")).toBeNull();
    expect(container.querySelector(".results-navigator--mobile")).not.toBeNull();
  });

  it("filters every navigator object from one controlled search query", () => {
    const onQueryChange = vi.fn();
    const props = {
      items: itemsFor(),
      open: true,
      query: "",
      selection: { type: "report" } as const,
      onClose: () => {},
      onQueryChange,
      onSelect: () => {},
    };
    const { rerender } = render(<ResultsNavigator {...props} />);

    fireEvent.change(screen.getByRole("searchbox", { name: "search_results" }), {
      target: { value: "customer" },
    });
    expect(onQueryChange).toHaveBeenCalledWith("customer");

    rerender(<ResultsNavigator {...props} query="customer" />);
    const navigator = screen.getByRole("navigation", { name: "navigator_label" });
    expect(within(navigator).getByText("customer-input.csv")).toBeInTheDocument();
    expect(within(navigator).queryByText("brief.html")).not.toBeInTheDocument();
    expect(within(navigator).queryByText("report")).not.toBeInTheDocument();
    expect(within(navigator).queryByText("code")).not.toBeInTheDocument();
  });

  it("keeps server alias matches and exposes complete pagination state", () => {
    const onLoadMore = vi.fn();
    render(
      <ResultsNavigator
        items={itemsFor({ codeAvailable: false, reportAvailable: false })}
        open
        query="图表数据"
        selection={{ type: "none" }}
        listState={{
          searchMode: "server",
          completeness: "complete",
          loadedCount: 2,
          authorizedTotal: 135,
          matchedTotal: 12,
          loading: false,
          loadingMore: false,
          hasMore: true,
          error: false,
        }}
        onClose={() => {}}
        onQueryChange={() => {}}
        onSelect={() => {}}
        onLoadMore={onLoadMore}
      />,
    );

    expect(screen.getByText("brief.html")).toBeInTheDocument();
    expect(screen.getByText("customer-input.csv")).toBeInTheDocument();
    expect(screen.getByText("list_matches")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "list_load_more" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("labels a 100-row legacy list as partial and scopes its search placeholder", () => {
    render(
      <ResultsNavigator
        items={itemsFor({ codeAvailable: false })}
        open
        query=""
        selection={{ type: "none" }}
        listState={{
          searchMode: "local",
          completeness: "partial",
          loadedCount: 100,
          authorizedTotal: null,
          matchedTotal: null,
          loading: false,
          loadingMore: false,
          hasMore: false,
          error: false,
        }}
        onClose={() => {}}
        onQueryChange={() => {}}
        onSelect={() => {}}
      />,
    );

    expect(screen.getByPlaceholderText("search_loaded_results")).toBeInTheDocument();
    expect(screen.getByText("list_partial")).toBeInTheDocument();
  });

  it("adds a stable short ID when artifact filenames collide", () => {
    const duplicateArtifacts: ArtifactItem[] = [
      {
        id: "art_run_aaa111",
        filename: "sandbox-stdout.txt",
        mime: "text/plain",
        size: 10,
        source: "python_sandbox",
        created_at: "2026-07-30T00:00:00Z",
        preview_available: true,
        kind: "sandbox_output",
      },
      {
        id: "art_run_bbb222",
        filename: "sandbox-stdout.txt",
        mime: "text/plain",
        size: 12,
        source: "python_sandbox",
        created_at: "2026-07-30T00:01:00Z",
        preview_available: true,
        kind: "sandbox_output",
      },
    ];
    render(
      <ResultsNavigator
        items={itemsFor({
          artifactItems: duplicateArtifacts,
          codeAvailable: false,
          reportAvailable: false,
        })}
        open
        query=""
        selection={{ type: "none" }}
        onClose={() => {}}
        onQueryChange={() => {}}
        onSelect={() => {}}
      />,
    );

    expect(screen.getByText(
      "source_sandbox · 2026-07-30T00:00:00Z",
    )).toBeInTheDocument();
    expect(screen.getByText(
      "source_sandbox · 2026-07-30T00:01:00Z",
    )).toBeInTheDocument();
    expect(screen.queryByText(/aaa111|bbb222/)).not.toBeInTheDocument();
  });

  it("keeps all 100 objects searchable and preserves the exact selection", () => {
    const hundredArtifacts: ArtifactItem[] = Array.from({ length: 100 }, (_, index) => ({
      id: `art_scale_${String(index).padStart(3, "0")}`,
      filename: `result-${String(index).padStart(3, "0")}.csv`,
      mime: "text/csv",
      size: index + 1,
      source: "bi_fetch_rows",
      created_at: "2026-08-07T00:00:00Z",
      preview_available: true,
      kind: "tool_data",
    }));
    const items = itemsFor({
      artifactItems: hundredArtifacts,
      codeAvailable: false,
      reportAvailable: false,
    });
    const props = {
      items,
      open: true,
      query: "",
      selection: { type: "artifact", artifactId: "art_scale_099" } as const,
      onClose: () => {},
      onQueryChange: () => {},
      onSelect: () => {},
    };
    const { container, rerender } = render(<ResultsNavigator {...props} />);

    expect(container.querySelectorAll(".results-nav-item")).toHaveLength(100);
    expect(screen.getByRole("button", { name: /result-099\.csv/ }))
      .toHaveAttribute("aria-current", "page");

    rerender(<ResultsNavigator {...props} query="result-099" />);
    expect(container.querySelectorAll(".results-nav-item")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /result-099\.csv/ }))
      .toHaveAttribute("aria-current", "page");
  });

  it("collapses 30+ data artifacts by stable turn and expands search matches", () => {
    const groupedArtifacts: ArtifactItem[] = Array.from({ length: 32 }, (_, index) => {
      const recent = index >= 16;
      return {
        id: `art_group_${index}`,
        filename: `${recent ? "recent" : "older"}-${index}.csv`,
        mime: "text/csv",
        size: index + 1,
        source: "python_sandbox",
        created_at: recent ? "2026-08-08T02:00:00Z" : "2026-08-08T01:00:00Z",
        created_by_turn: recent ? "turn_recent" : "turn_older",
        preview_available: true,
        kind: "sandbox_output",
      };
    });
    const items = itemsFor({
      artifactItems: groupedArtifacts,
      codeAvailable: false,
      reportAvailable: false,
    });
    const props = {
      items,
      open: true,
      query: "",
      selection: { type: "none" } as const,
      onClose: () => {},
      onQueryChange: () => {},
      onSelect: () => {},
    };
    const { container, rerender } = render(<ResultsNavigator {...props} />);

    expect(container.querySelectorAll(".results-nav-run-toggle")).toHaveLength(2);
    expect(container.querySelectorAll(".results-nav-item")).toHaveLength(16);
    expect(screen.getByText("recent-31.csv")).toBeInTheDocument();
    expect(screen.queryByText("older-3.csv")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /run_group_earlier/ }));
    expect(screen.getByText("older-3.csv")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /run_group_earlier/ }));
    rerender(<ResultsNavigator {...props} query="older-3" />);
    expect(screen.getByText("older-3.csv")).toBeInTheDocument();
  });

  it("collapses a recent run at 30 items while preserving manual and search access", () => {
    const artifactsForLargeRun = (recentCount: number): ArtifactItem[] => [
      ...Array.from({ length: 16 }, (_, index) => ({
        id: `art_older_run_${index}`,
        filename: `older-run-${index}.csv`,
        mime: "text/csv",
        size: index + 1,
        source: "python_sandbox",
        created_at: "2026-08-08T01:00:00Z",
        created_by_turn: "turn_older",
        preview_available: true,
        kind: "sandbox_output" as const,
      })),
      ...Array.from({ length: recentCount }, (_, index) => ({
        id: `art_large_run_${index}`,
        filename: `large-run-${index}.csv`,
        mime: "text/csv",
        size: index + 1,
        source: "python_sandbox",
        created_at: "2026-08-08T02:00:00Z",
        created_by_turn: "turn_large",
        preview_available: true,
        kind: "sandbox_output" as const,
      })),
    ];
    const itemsAt14 = itemsFor({
      artifactItems: artifactsForLargeRun(14),
      codeAvailable: false,
      reportAvailable: false,
    });
    const props = {
      items: itemsAt14,
      open: true,
      query: "",
      selection: { type: "none" } as const,
      onClose: () => {},
      onQueryChange: () => {},
      onSelect: () => {},
    };
    const { container, rerender } = render(<ResultsNavigator {...props} />);

    expect(screen.getByRole("button", { name: /run_group_recent/ }))
      .toHaveAttribute("aria-expanded", "true");
    expect(container.querySelectorAll(".results-nav-item")).toHaveLength(14);

    const itemsAt30 = itemsFor({
      artifactItems: artifactsForLargeRun(30),
      codeAvailable: false,
      reportAvailable: false,
    });
    rerender(<ResultsNavigator {...props} items={itemsAt30} />);
    const recentRunToggle = screen.getByRole("button", { name: /run_group_recent/ });
    expect(recentRunToggle).toHaveAttribute("aria-expanded", "false");
    expect(container.querySelectorAll(".results-nav-item")).toHaveLength(0);

    fireEvent.click(recentRunToggle);
    expect(container.querySelectorAll(".results-nav-item")).toHaveLength(30);

    fireEvent.click(recentRunToggle);
    rerender(<ResultsNavigator {...props} items={itemsAt30} query="large-run-29" />);
    expect(screen.getByText("large-run-29.csv")).toBeInTheDocument();
  });
});
