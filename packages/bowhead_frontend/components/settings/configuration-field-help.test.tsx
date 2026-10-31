import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import { ConfigurationFieldHelp, ConfigurationFieldProvider } from "./configuration-field-help";
import { parseFieldContract, type FieldContract } from "@/lib/configuration-field-guidance";
import manifest from "@/edition/configuration-field-translations.json";
import en from "@/edition/shared-messages/en-US.json";
import zh from "@/edition/shared-messages/zh-CN.json";

const id = "team_model_retry.max_retries";
const entry = manifest.fields[id];
const description = en.configuration_fields[entry.key as keyof typeof en.configuration_fields];
const contract: FieldContract = {
  contract_version: 1,
  domain: "team_model_retry",
  contract_revision: "test",
  schema: {
    type: "object",
    properties: {
      max_retries: { type: "integer", minimum: 0, maximum: 5, description, "x-bowhead-field": { id, text_revision: entry.revision, unit: "count" } },
    },
  },
};

afterEach(cleanup);
describe("configuration field help", () => {
  it("uses current translations and displays typed zero bounds without editing the control", () => {
    render(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <input aria-label="Retries" defaultValue="0" />
        <ConfigurationFieldProvider contracts={{ team_model_retry: contract }}>
          <ConfigurationFieldHelp field={id} />
        </ConfigurationFieldProvider>
      </NextIntlClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "字段说明" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("零会禁用重试");
    expect(dialog.textContent).toContain("整数");
    expect(dialog.textContent).toContain("0 – 5");
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("0");
  });
  it("falls back to current source meaning when a newer server changes the description", () => {
    const next = structuredClone(contract);
    next.schema.properties!.max_retries.description = "New source meaning.";
    next.schema.properties!.max_retries["x-bowhead-field"]!.text_revision = "new-source";
    render(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <ConfigurationFieldHelp field={id} contract={next} />
      </NextIntlClientProvider>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("dialog").textContent).toContain("New source meaning.");
    expect(screen.getByRole("dialog").textContent).not.toContain("零会禁用重试");
  });
  it("rejects malformed metadata as a whole and clears help when the target changes", () => {
    const invalid = structuredClone(contract);
    invalid.schema.properties!.max_retries.minimum = 9;
    expect(parseFieldContract(invalid)).toBeUndefined();
    const view = render(
      <NextIntlClientProvider locale="en-US" messages={en}>
        <ConfigurationFieldHelp field={id} contract={contract} />
      </NextIntlClientProvider>,
    );
    fireEvent.click(screen.getByRole("button"));
    view.rerender(
      <NextIntlClientProvider locale="en-US" messages={en}>
        <ConfigurationFieldHelp field={id} />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

it("uses new retry bounds without copying a schema default into the existing draft", async () => {
  const { RetrySettingSection } = await import("./models/retry-setting-section");
  const next = structuredClone(contract);
  next.schema.properties!.max_retries.maximum = 7;
  next.schema.properties!.max_retries.default = 4;
  render(<NextIntlClientProvider locale="en-US" messages={en}>
    <ConfigurationFieldProvider contracts={{ team_model_retry: next }}>
      <RetrySettingSection value={2} version={3} onSaved={() => undefined} userFacingError={() => "Unavailable"} />
    </ConfigurationFieldProvider>
  </NextIntlClientProvider>);
  const input = screen.getByRole("spinbutton") as HTMLInputElement;
  expect(input.value).toBe("2");
  expect(input.max).toBe("7");
  fireEvent.change(input, { target: { value: "6" } });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(input.value).toBe("6");
});
