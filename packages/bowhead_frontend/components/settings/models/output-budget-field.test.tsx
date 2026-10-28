import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import messages from "@/messages/en-US.json";
import { OutputBudgetField } from "./output-budget-field";
import { outputCapacity, outputPolicy } from "@/lib/output-budget";

const defaults = {
  version: "model-output-budget.v1",
  automatic_ceiling: 256000,
  unknown_capacity: 32000,
  models: { small: 128000, large: 393216 },
};

describe("visible output defaults", () => {
  it.each([
    ["small", "128,000"],
    ["large", "256,000"],
    ["unknown", "32,000"],
  ])(
    "shows the exact automatic allowance for %s without editing policy",
    (modelId, amount) => {
      const onChange = vi.fn();
      render(
        <NextIntlClientProvider locale="en-US" messages={messages}>
          <OutputBudgetField
            index={0}
            modelId={modelId}
            defaults={defaults}
            capabilities={["chat", "tool"]}
            params={{}}
            onChange={onChange}
          />
        </NextIntlClientProvider>,
      );
      expect(
        screen.getByText(`Automatic · up to ${amount} tokens`),
      ).toBeVisible();
      expect(
        screen.getByLabelText("Model output capacity (tokens)"),
      ).toHaveValue(outputCapacity(defaults, modelId).value);
      expect(onChange).not.toHaveBeenCalled();
    },
  );

  it("keeps inherited capacity separate from request exceptions and exposes legal omission", () => {
    const onChange = vi.fn();
    render(
      <NextIntlClientProvider locale="en-US" messages={messages}>
        <OutputBudgetField
          index={0}
          modelId="unknown"
          inherited={64000}
          defaults={defaults}
          capabilities={["chat", "tool"]}
          params={{ max_tokens: { mode: "cap", value: 16000 } }}
          runtime={{
            capability_overrides: {
              tool: { outbound_params: { max_tokens: { mode: "omit" } } },
            },
          }}
          onChange={onChange}
        />
      </NextIntlClientProvider>,
    );
    expect(
      screen.getByText("Inherited from connection · Capacity: 64,000 tokens"),
    ).toBeVisible();
    expect(screen.getByText("Capped · up to 16,000 tokens")).toBeVisible();
    expect(screen.getByText("Omitted · server limit unknown")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Model output capacity (tokens)"), {
      target: { value: "128000" },
    });
    expect(onChange).toHaveBeenLastCalledWith(128000);
  });

  it("preserves explicit fixed values above the automatic ceiling", () => {
    expect(
      outputPolicy(defaults, 393216, "tool", {
        max_tokens: { mode: "fixed", value: 300000 },
      }),
    ).toEqual({ mode: "fixed", value: 300000 });
  });
  it("shows effective advanced values after legacy omission and preserves runtime follow", () => {
    expect(outputPolicy(defaults, 393216, "chat", { max_tokens: { mode: "fixed", value: 300000 } }, { defaults: { outbound_params: { max_tokens: { mode: "follow" } } } })).toEqual({ mode: "fixed", value: 300000 });
    expect(outputPolicy(defaults, 32000, "chat", { max_tokens: { mode: "omit" } }, { defaults: { advanced: { max_tokens: 9000 } } })).toEqual({ mode: "fixed", value: 9000 });
  });

});
