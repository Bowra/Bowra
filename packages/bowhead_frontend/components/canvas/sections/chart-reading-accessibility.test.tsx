import "@testing-library/jest-dom/vitest";
import { StrictMode, useState } from "react";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { ChartTable } from "./chart-table";
import { EditModeToggle } from "../edit-mode-toggle";
import type { ChartSpec } from "@/lib/chart/types";

afterEach(() => { cleanup(); document.documentElement.lang = ""; });

it.each(["en-US", "zh-CN", "de-DE", "fr-FR"] as const)("explains known source scales without inferring currency from %s", (locale) => {
  document.documentElement.lang = locale;
  const spec: ChartSpec = {
    kind: "table", title: "Original observations", row_count: 1,
    columns: [
      { key: "cny", header: "Revenue", type: "numeric", unit: "万元" },
      { key: "usd", header: "Cost", type: "numeric", unit: "million USD" },
      { key: "eur", header: "Budget", type: "numeric", unit: "million EUR" },
      { key: "unknown", header: "Unspecified", type: "numeric", unit: "Source units" },
    ],
    rows: [{ cny: 52.12345, usd: 1.25, eur: 2.5, unknown: 4 }],
  };
  const view = renderIntl(<ChartTable spec={spec} />, { locale });
  const note = view.container.querySelector("[data-chart-unit-note]");
  expect(note).toHaveTextContent("万元");
  expect(note).toHaveTextContent("USD");
  expect(note).toHaveTextContent("EUR");
  expect(note).not.toHaveTextContent("Unspecified");
  expect(note).not.toHaveTextContent("source_scale");
  expect(screen.getByText("Original observations")).toBeInTheDocument();
  view.unmount();
  const exact = renderIntl(<ChartTable spec={spec} exactNumbers />, { locale });
  expect(exact.container.querySelector("[data-chart-unit-note]")).toHaveTextContent("万元");
  expect(screen.getByText("52.12345")).toBeInTheDocument();
});

it.each(["en-US", "zh-CN", "de-DE", "fr-FR"] as const)("discloses conversion beside the caption in interactive and export tables in %s", (locale) => {
  document.documentElement.lang = locale;
  const spec: ChartSpec = { kind: "table", title: "Revenue (万元)", subtitle: "单位：万元；synthetic values", row_count: 1,
    columns: [{ key: "amount", header: "Revenue", type: "numeric", unit: "万元" }], rows: [{ amount: 120.5 }] };
  const before = JSON.stringify(spec);
  for (const surface of ["canvas", "export"] as const) {
    const view = renderIntl(<StrictMode><ChartTable spec={spec} surface={surface} /></StrictMode>, { locale });
    const note = view.container.querySelector("[data-chart-unit-note]")!;
    expect(note).toHaveTextContent("万元");
    expect(note).toHaveTextContent("元");
    expect(note.compareDocumentPosition(screen.getByRole("table")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Revenue (元)" })).toBeInTheDocument();
    expect(screen.getByRole("cell").textContent).toBe(new Intl.NumberFormat(locale).format(1205000));
    expect(view.container.querySelector(".v2-chart-subtitle")!.textContent).not.toMatch(/^单位：万元/);
    expect(view.container.querySelector(".v2-chart-subtitle")).toHaveTextContent("synthetic values");
    view.unmount();
  }
  expect(JSON.stringify(spec)).toBe(before);
});

it.each(["en-US", "zh-CN", "de-DE", "fr-FR"] as const)("keeps distinct source-unit columns in %s", (locale) => {
  document.documentElement.lang = locale;
  const spec: ChartSpec = { kind: "table", title: "Units", row_count: 1,
    columns: [{ key: "date", header: "Date", type: "date" },
      { key: "a", header: "Revenue (亿元)", type: "numeric", unit: "亿元" },
      { key: "b", header: "Revenue (万元)", type: "numeric", unit: "万元" },
      { key: "c", header: "Revenue (元)", type: "numeric", unit: "元" }],
    rows: [{ date: "2026-01-02", a: 0.0009, b: 9, c: 90000 }] };
  const view = renderIntl(<ChartTable spec={spec} />, { locale });
  expect(screen.getByRole("columnheader", { name: "Revenue (亿元)" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "Revenue (万元)" })).toBeInTheDocument();
  expect(screen.getByRole("columnheader", { name: "Revenue (元)" })).toBeInTheDocument();
  expect(screen.getAllByRole("cell")[1]).toHaveTextContent(locale.startsWith("de") || locale.startsWith("fr") ? "0,0009" : "0.0009");
  expect(view.container.querySelector("[data-chart-unit-note]")).toBeNull();
});

it("keeps a stable polite save-status host across editing and saving states", () => {
  function Harness() {
    const [state, setState] = useState("view");
    return <><button onClick={() => setState("saving")}>Save</button><button onClick={() => setState("saved")}>Finish</button>
      <EditModeToggle isEditMode={state !== "view"} saving={state === "saving"} onToggle={() => {}} /></>;
  }
  renderIntl(<Harness />, { locale: "en-US" });
  const status = screen.getByRole("status");
  expect(status).toHaveAttribute("aria-live", "polite");
  expect(status).toBeEmptyDOMElement();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(screen.getByRole("status")).toBe(status);
  expect(status).toHaveTextContent("Saving");
  fireEvent.click(screen.getByRole("button", { name: "Finish" }));
  expect(screen.getByRole("status")).toBe(status);
  expect(status).toHaveTextContent("Saved");
});
