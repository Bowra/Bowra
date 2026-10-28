/**
 * 0.39.8 #4 — DataTableSection legacy-shape adapter.
 *
 * Pre-0.39.8 canvas_composer emitted `data_table` sections with
 * `columns: string[]` + `rows: any[][]`. FE expected `columns: TableCol[]` +
 * `rows: TableRow[]` → every cell rendered "—". The 0.39.8 BE fix
 * (canvas_composer.py:1340) emits the correct shape, but persisted sections
 * from older turns still have the legacy form — adapt at render time.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import enMessages from "@/messages/en-US.json";

import { DataTableSection, type DataTableSectionData } from "./data-table";
import {
  CANVAS_FOCUS_DATA_EVENT,
  type CanvasFocusDataDetail,
} from "@/lib/canvas-workspace-focus";

afterEach(() => cleanup());

// Consolidation (2026-09-08): `data_table` now uses `ChartTable` (see
// `lib/chart/data-table-to-spec.ts`), moving i18n into the `chart_table` namespace,
// and actions from a persistent bar into the `⋯` menu (quiet by default).
// **Capability contract unchanged** — assertions below check the same things at their new locations.
/** Actions live in `⋯`; open the menu first. */
function openMore(getByRole: (r: string, o: { name: string }) => HTMLElement) {
  fireEvent.click(getByRole("button", { name: "More actions" }));
}

function renderWithIntl(node: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={enMessages}>
      {node}
    </NextIntlClientProvider>,
  );
}

describe("DataTableSection · legacy shape adapter (0.39.8 #4)", () => {
  it("renders legacy rows without inferring percentage shape from column names", () => {
    const legacy = {
      id: "sec_legacy",
      kind: "data_table" as const,
      caption: "医院等级 有哪些值",
      // Legacy shape from canvas_composer.py pre-0.39.8
      columns: ["医院等级", "销售数量达成率"] as unknown as never,
      rows: [
        ["Tier 3", 0.97],
        ["Tier 1", 1.05],
      ] as unknown as never,
    };
    const { container } = renderWithIntl(<DataTableSection data={legacy} />);
    const ths = container.querySelectorAll("th");
    expect(ths.length).toBe(2);
    expect(ths[0]?.textContent).toBe("医院等级");
    expect(ths[1]?.textContent).toBe("销售数量达成率");
    const tds = container.querySelectorAll("tbody td");
    // 2 rows × 2 cols = 4 cells. NONE should be em-dash (the legacy bug).
    expect(tds.length).toBe(4);
    expect(tds[0]?.textContent).toBe("Tier 3");
    // Legacy headers provide no fraction/points metadata, so retain the prepared numbers.
    expect(tds[1]?.textContent).toBe("0.97");
    expect(tds[2]?.textContent).toBe("Tier 1");
    expect(tds[3]?.textContent).toBe("1.05");
    // None should be em-dash
    const cellTexts = Array.from(tds).map((td) => td.textContent);
    expect(cellTexts).not.toContain("—");
  });

  it("renders modern {columns: TableCol[], rows: TableRow[]} unchanged", () => {
    const modern = {
      id: "sec_modern",
      kind: "data_table" as const,
      caption: "test",
      columns: [
        { key: "name", header: "Name", numeric: false },
        { key: "score", header: "Score", numeric: true },
      ],
      rows: [
        { name: "A", score: 95 },
        { name: "B", score: 87 },
      ],
    };
    const { container } = renderWithIntl(<DataTableSection data={modern} />);
    const ths = container.querySelectorAll("th");
    expect(ths[0]?.textContent).toBe("Name");
    expect(ths[1]?.textContent).toBe("Score");
    const tds = container.querySelectorAll("tbody td");
    expect(tds[0]?.textContent).toBe("A");
    expect(tds[1]?.textContent).toBe("95");
  });

  it.each([
    { shape: "fraction", scale: "fraction" as const, expected: ["97%", "105%"] },
    { shape: "points", scale: "points" as const, expected: ["0.97%", "1.05%"] },
    { shape: "unknown", scale: undefined, expected: ["0.97", "1.05"] },
  ])("respects $shape percentage metadata without changing the prepared rows", ({ scale, expected }) => {
    const table: DataTableSectionData = {
      id: "sec_percentage",
      kind: "data_table",
      caption: "Achievement rates",
      columns: [{ key: "rate", header: "Achievement rate", numeric: true, format: "percent", percent_scale: scale }],
      rows: [{ rate: 0.97 }, { rate: 1.05 }],
    };
    const original = structuredClone(table);
    const { container } = renderWithIntl(<DataTableSection data={table} />);
    expect(Array.from(container.querySelectorAll("tbody td"), (cell) => cell.textContent)).toEqual(expected);
    expect(table).toEqual(original);
  });

  it("legacy first-row empty value detects numeric col via later row peek", () => {
    // Real prod case: sec_00f08ec49602 row[0] = ["", 9905]; col_a numeric
    // would be misdetected from "" if not peeking further rows.
    const legacy = {
      id: "sec_x",
      kind: "data_table" as const,
      caption: "test",
      columns: ["col_a", "col_b"] as unknown as never,
      rows: [
        ["", 9905],
        ["Tier 1", 1024],
      ] as unknown as never,
    };
    const { container } = renderWithIntl(<DataTableSection data={legacy} />);
    const ths = container.querySelectorAll("th");
    // After consolidation, `ChartTable` supplies numeric column class (`is-num`); the criterion remains:
    // col_a must **look past** the first row's "" for correct detection, otherwise it is classified as numeric.
    expect(ths[0]?.className).not.toContain("num");
    expect(ths[1]?.className).toContain("num");
  });

  it("legacy null cells render '—' (preserves existing missing-data UX)", () => {
    const legacy = {
      id: "sec_y",
      kind: "data_table" as const,
      caption: "test",
      columns: ["a", "b"] as unknown as never,
      rows: [[null, "ok"], [undefined, "ok2"]] as unknown as never,
    };
    const { container } = renderWithIntl(<DataTableSection data={legacy} />);
    const tds = container.querySelectorAll("tbody td");
    // null/undefined → "" via adapter → empty string render (not em-dash)
    // but "ok" present.
    expect(tds[1]?.textContent).toBe("ok");
    expect(tds[3]?.textContent).toBe("ok2");
  });

  it("renders artifact actions even when the table has no caption", () => {
    const table = {
      id: "sec_artifact",
      kind: "data_table" as const,
      caption: "",
      columns: [{ key: "name", header: "Name", numeric: false }],
      rows: [{ name: "A" }],
      meta: {
        artifact_id: "file 1",
        filename: "rows.csv",
      },
    };

    const { getByRole } = renderWithIntl(<DataTableSection data={table} />);

    openMore(getByRole as never);
    expect(getByRole("menuitem", { name: "Open Data" })).toBeTruthy();
    // ★ With an original filename, download the **original file**, rather than exported CSV
    expect(getByRole("menuitem", { name: "⬇ rows.csv" }).getAttribute("href")).toBe(
      "/api/v1/files/file%201",
    );
  });

  it("uses the table section as Report return context", () => {
    const listener = vi.fn();
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
    const table = {
      id: "sec_artifact",
      kind: "data_table" as const,
      caption: "Exact rows",
      columns: [{ key: "name", header: "Name", numeric: false }],
      rows: [{ name: "A" }],
      meta: { artifact_id: "art_rows", filename: "rows.csv" },
    };
    const { getByRole } = renderWithIntl(<DataTableSection data={table} />);

    openMore(getByRole as never);
    fireEvent.click(getByRole("menuitem", { name: "Open Data" }));

    const event = listener.mock.calls[0]?.[0] as CustomEvent<CanvasFocusDataDetail>;
    expect(event.detail).toEqual({
      artifactId: "art_rows",
      entryPoint: "report",
      reportContext: { label: "Exact rows", sectionId: "sec_artifact" },
    });
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
  });
});
