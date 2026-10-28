/**
 * Outbound-parameter criterion layer.
 *
 * Each criterion **also has a backend copy** (`bowhead_router/outbound.py`,
 * `team_llm_config._validate_outbound_params`). Duplicated implementations of one criterion repeatedly cause repository failures,
 * so both sides need their own enforcing tests, with semantics aligned rule by rule.
 */

import { describe, expect, it } from "vitest";

import {
  adaptationsForModel,
  declaredOutboundParamCount,
  outboundParamModes,
  outboundParamsConflicts,
  outboundParamsValid,
  parseOutboundParams,
  serializeOutboundParams,
} from "./outbound-params";

describe("协议差异", () => {
  it("Anthropic 的单次输出上限不给「不发送」这一档", () => {
    // Required by this protocol: omitting it invalidates the request (backend also rejects on save; identical criterion on both sides).
    expect(outboundParamModes("max_tokens", "anthropic_compatible")).not.toContain("omit");
    expect(outboundParamModes("max_tokens", "openai_compatible")).toContain("omit");
  });

  it("只有单次输出上限有「不超过…」", () => {
    expect(outboundParamModes("max_tokens", "openai_compatible")).toContain("cap");
    expect(outboundParamModes("temperature", "openai_compatible")).not.toContain("cap");
  });

  it("Anthropic 的其他参数不受影响", () => {
    expect(outboundParamModes("temperature", "anthropic_compatible")).toContain("omit");
  });
});

describe("落库形状", () => {
  it("全跟随默认时不写这个键", () => {
    // Unconfigured and configured-but-all-follow must yield identical evidence signatures, or every save makes evidence stale.
    expect(serializeOutboundParams({})).toBeUndefined();
    expect(serializeOutboundParams({ temperature: { mode: "follow" } })).toBeUndefined();
  });

  it("只写表过态的参数，且不带模式用不上的值", () => {
    expect(
      serializeOutboundParams({
        temperature: { mode: "fixed", value: 1 },
        top_p: { mode: "follow" },
        max_tokens: { mode: "omit", value: 999 },
      }),
    ).toEqual({
      temperature: { mode: "fixed", value: 1 },
      max_tokens: { mode: "omit" },
    });
  });

  it("往返：服务端形状 → 表单 → 服务端形状", () => {
    const stored = { temperature: { mode: "omit" }, max_tokens: { mode: "cap", value: 4096 } };

    expect(serializeOutboundParams(parseOutboundParams(stored))).toEqual(stored);
  });

  it("脏配置不卡住编辑器：未知键与坏形状一律忽略", () => {
    expect(
      parseOutboundParams({
        messages: { mode: "omit" },
        temperature: { mode: "nope" },
        top_p: "1",
        max_tokens: { mode: "fixed", value: 4096 },
      }),
    ).toEqual({ max_tokens: { mode: "fixed", value: 4096 } });
  });
});

describe("可保存判据", () => {
  it("固定/上限缺数值时不可保存", () => {
    expect(outboundParamsValid({ temperature: { mode: "fixed" } })).toBe(false);
    expect(outboundParamsValid({ max_tokens: { mode: "cap" } })).toBe(false);
  });

  it("跟随与不发送不需要数值", () => {
    expect(outboundParamsValid({ temperature: { mode: "omit" }, top_p: { mode: "follow" } })).toBe(true);
  });
});

describe("与原始 JSON 逃生口撞键", () => {
  it("撞了就说出来（不改用户的 JSON）", () => {
    expect(
      outboundParamsConflicts({ temperature: { mode: "omit" } }, {
        chat: { enable_thinking: false, temperature: 0.4 },
      }),
    ).toEqual(["temperature"]);
  });

  it("负对照：没表态的参数不算撞", () => {
    // With all-follow, JSON temperature is the sole declaration, so no conflict exists.
    expect(outboundParamsConflicts({}, { chat: { temperature: 0.4 } })).toEqual([]);
  });

  it("负对照：JSON 里没这个键也不算撞", () => {
    expect(
      outboundParamsConflicts({ temperature: { mode: "omit" } }, { chat: { enable_thinking: false } }),
    ).toEqual([]);
  });
});

describe("已自动适配的展示判据", () => {
  const adaptations = [
    { provider: "example-provider", model_id: "openai_gpt56_luna", param: "temperature", since: "2026-08-17T10:02:19+00:00" },
  ];

  it("认本来源本模型", () => {
    expect(adaptationsForModel(adaptations, "example-provider", "openai_gpt56_luna", {})).toHaveLength(1);
    expect(adaptationsForModel(adaptations, "example-provider", "other-model", {})).toHaveLength(0);
    expect(adaptationsForModel(adaptations, "other-source", "openai_gpt56_luna", {})).toHaveLength(0);
  });

  it("★ 人表过态之后就不再显示成「机器改过」", () => {
    // Runtime no longer intervenes (human declaration > machine observation). If UI still shows adaptation, it describes something
    // runtime will not do—the recurring problem of the page issuing server verdicts that this sprint addresses.
    expect(
      adaptationsForModel(adaptations, "example-provider", "openai_gpt56_luna", {
        temperature: { mode: "fixed", value: 1 },
      }),
    ).toHaveLength(0);
  });

  it("白名单外的参数名不渲染（上游可以说任何话）", () => {
    expect(
      adaptationsForModel(
        [{ provider: "p", model_id: "m", param: "messages", since: "2026-08-17T00:00:00+00:00" }],
        "p",
        "m",
        {},
      ),
    ).toHaveLength(0);
  });
});

describe("计数", () => {
  it("跟随不算已设", () => {
    expect(declaredOutboundParamCount({ temperature: { mode: "follow" } })).toBe(0);
    expect(
      declaredOutboundParamCount({ temperature: { mode: "omit" }, max_tokens: { mode: "cap", value: 4096 } }),
    ).toBe(2);
  });
});
