import { describe, expect, it } from "vitest";
import {
  parseAdvancedJson,
  runtimeParametersValid,
  stageLegacyConversion,
  updateParameterScope,
} from "./runtime-parameters";
import {
  parseOutboundParams,
  serializeOutboundParams,
} from "./outbound-params";

describe("model parameter policy boundaries", () => {
  it("blocks reasoning without a selected profile and rejects numeric booleans in the new layer", () => {
    expect(runtimeParametersValid({ defaults: { thinking_mode: "off" } })).toBe(
      false,
    );
    expect(
      runtimeParametersValid({
        defaults: {
          outbound_params: { parallel_tool_calls: { mode: "fixed", value: 0 } },
        },
      }),
    ).toBe(false);
  });

  it("keeps a raw value when legacy follow has not overridden it", () => {
    const result = stageLegacyConversion(
      undefined,
      { chat: { temperature: 0.4 } },
      { temperature: { mode: "follow" } },
      undefined,
    );
    expect(
      result.runtime?.capability_overrides?.chat?.outbound_params?.temperature,
    ).toEqual({ mode: "fixed", value: 0.4 });
  });
  it("preserves false through parsing and serialization", () => {
    const value = { parallel_tool_calls: { mode: "fixed", value: false } };
    expect(serializeOutboundParams(parseOutboundParams(value))).toEqual(value);
  });

  it("removes only the selected exception, not shared defaults", () => {
    expect(
      updateParameterScope(
        {
          defaults: { thinking_mode: "off" },
          capability_overrides: {
            tool: { thinking_mode: "on" },
            vision: { reasoning_effort: "low" },
          },
        },
        "tool",
        {},
      ),
    ).toEqual({
      defaults: { thinking_mode: "off" },
      capability_overrides: { vision: { reasoning_effort: "low" } },
    });
  });

  it("rejects duplicate JSON names without confusing nested names or escaped strings", () => {
    expect(parseAdvancedJson('{"seed":1,"seed":2}')).toBeNull();
    expect(parseAdvancedJson('{"vendor_limit":1e999}')).toBeNull();
    expect(
      parseAdvancedJson('{"vendor_options":[{"limit":-1e999}]}'),
    ).toBeNull();
    expect(
      parseAdvancedJson(
        '{"a":{"seed":1},"b":{"seed":2},"c":"quoted\\\" value"}',
      ),
    ).toEqual({ a: { seed: 1 }, b: { seed: 2 }, c: 'quoted" value' });
  });

  it("stages legacy conversion at the original capability and retains existing typed precedence", () => {
    const raw = { chat: { temperature: 0.4, seed: 7 } };
    const result = stageLegacyConversion(
      undefined,
      raw,
      { temperature: { mode: "fixed", value: 0.8 } },
      undefined,
    );
    expect(result.runtime?.defaults).toBeUndefined();
    expect(
      result.runtime?.capability_overrides?.chat?.outbound_params?.temperature
        ?.value,
    ).toBe(0.8);
    expect(result.raw).toEqual({ chat: { seed: 7 } });
    expect(raw).toEqual({ chat: { temperature: 0.4, seed: 7 } });
  });
});
