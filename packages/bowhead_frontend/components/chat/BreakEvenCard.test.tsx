import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import cases from "../../../bowhead_core/tests/fixtures/break-even-cases.json";
import { ChatAnswerCard } from "./BreakEvenCard";

afterEach(cleanup);

const payload = (unit_price: string | null, unit_variable_cost: string | null, fixed_costs: string | null) => ({
  answer_card: { kind: "break_even", version: 1, initial: { unit_price, unit_variable_cost, fixed_costs } },
});

it.each(cases)("calculates %s, %s, %s", (price, cost, fixed, units) => {
  renderIntl(<ChatAnswerCard payload={payload(price, cost, fixed)} />);
  expect(screen.getByRole("status").textContent).toContain(units === null
    ? "当前无法收回固定成本"
    : new Intl.NumberFormat("zh-CN").format(BigInt(units)));
});

it("preserves edits across a duplicate payload and clears a missing or invalid result", () => {
  function Replay() {
    const [seq, setSeq] = useState(0);
    return <><button onClick={() => setSeq(seq + 1)}>Replay {seq}</button><ChatAnswerCard payload={payload("100", "60", "12000")} /></>;
  }
  renderIntl(<Replay />);
  const price = screen.getByRole("textbox", { name: "销售单价" });
  fireEvent.change(price, { target: { value: "90" } });
  fireEvent.click(screen.getByRole("button", { name: /Replay/ }));
  expect((price as HTMLInputElement).value).toBe("90");
  expect(screen.getByRole("status").textContent).toContain("400");
  fireEvent.change(price, { target: { value: "" } });
  expect(screen.getByRole("status").textContent).toContain("请填写销售单价");
  fireEvent.change(price, { target: { value: "-1" } });
  expect(price.getAttribute("aria-invalid")).toBe("true");
  expect(screen.getByRole("status").textContent).toContain("请检查销售单价");
});

it("compares slider edits to the saved quantity and restores all original inputs", () => {
  renderIntl(<ChatAnswerCard payload={payload("100", "60", "12000")} />);
  const reset = screen.getByRole("button", { name: "还原数值" });
  expect(reset).toBeDisabled();
  fireEvent.change(screen.getByRole("slider", { name: "调整销售单价" }), { target: { value: "90" } });
  expect(screen.getByRole("textbox", { name: "销售单价" })).toHaveValue("90");
  expect(screen.getByRole("status")).toHaveTextContent("原方案 300 件 · 变化 +100 件");
  fireEvent.change(screen.getByRole("slider", { name: "调整固定成本" }), { target: { value: "60" } });
  expect(screen.getByRole("status")).toHaveTextContent("200 件");
  fireEvent.click(reset);
  expect(screen.getByRole("textbox", { name: "固定成本" })).toHaveValue("12000");
  expect(screen.getByRole("status")).toHaveTextContent("300 件");
  expect(reset).toBeDisabled();
});

it("enables a missing slider after exact localized entry and restores the empty value", () => {
  renderIntl(<ChatAnswerCard payload={{ answer_card: { kind: "break_even", version: 1, initial: { unit_variable_cost: "0.2", fixed_costs: "0.3" } } }} />, { locale: "de-DE" });
  const slider = screen.getByRole("slider", { name: "Stückpreis anpassen" });
  const price = screen.getByRole("textbox", { name: "Stückpreis" });
  expect(slider).toBeDisabled();
  fireEvent.change(price, { target: { value: "0,30" } });
  expect(slider).toBeEnabled();
  expect(screen.getByRole("status")).toHaveTextContent("3 Stück");
  fireEvent.change(slider, { target: { value: "350" } });
  expect(price).toHaveValue("0,35");
  expect(screen.getByRole("status")).toHaveTextContent("2 Stück");
  fireEvent.click(screen.getByRole("button", { name: "Zurücksetzen" }));
  expect(price).toHaveValue("");
  expect(slider).toBeDisabled();
});

it("keeps slider endpoints inside the amount contract for large and fractional values", () => {
  renderIntl(<ChatAnswerCard payload={payload("999999999999999", "0.000001", "0")} />);
  for (const field of ["销售单价", "单位变动成本"]) {
    const slider = screen.getByRole("slider", { name: "调整" + field }) as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "0" } });
    fireEvent.change(slider, { target: { value: slider.max } });
    expect(screen.getByRole("textbox", { name: field })).toHaveAttribute("aria-invalid", "false");
    expect(screen.getByRole("status")).toHaveTextContent("0 件");
  }
});

it.each([{}, { answer_card: { kind: "break_even", version: 2, initial: {} } }, payload("bad", "1", "2")])("ignores unsupported snapshots", data => {
  renderIntl(<ChatAnswerCard payload={data} />);
  expect(screen.queryByRole("region")).toBeNull();
});

it("keeps an earlier generation's payload out of a resumed answer", () => {
  renderIntl(<ChatAnswerCard generation={2} payload={{ ...payload("100", "60", "12000"), execution_generation: 1 }} />);
  expect(screen.queryByRole("region")).toBeNull();
});
