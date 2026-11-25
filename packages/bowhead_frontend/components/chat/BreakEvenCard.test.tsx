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
  fireEvent.click(screen.getByRole("button"));
  expect((price as HTMLInputElement).value).toBe("90");
  expect(screen.getByRole("status").textContent).toContain("400");
  fireEvent.change(price, { target: { value: "" } });
  expect(screen.getByRole("status").textContent).toContain("请填写销售单价");
  fireEvent.change(price, { target: { value: "-1" } });
  expect(price.getAttribute("aria-invalid")).toBe("true");
  expect(screen.getByRole("status").textContent).toContain("请检查销售单价");
});

it.each([{}, { answer_card: { kind: "break_even", version: 2, initial: {} } }, payload("bad", "1", "2")])("ignores unsupported snapshots", data => {
  renderIntl(<ChatAnswerCard payload={data} />);
  expect(screen.queryByRole("region")).toBeNull();
});

it("keeps an earlier generation's payload out of a resumed answer", () => {
  renderIntl(<ChatAnswerCard generation={2} payload={{ ...payload("100", "60", "12000"), execution_generation: 1 }} />);
  expect(screen.queryByRole("region")).toBeNull();
});
