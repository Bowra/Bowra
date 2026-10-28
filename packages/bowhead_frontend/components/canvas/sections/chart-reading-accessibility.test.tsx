import "@testing-library/jest-dom/vitest";
import { useState } from "react";
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
  expect(exact.container.querySelector("[data-chart-unit-note]")).toBeNull();
  expect(screen.getByText("52.12345")).toBeInTheDocument();
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
