/**
 * Why Save is disabled—one enforced contract per reason.
 *
 * User-tested trigger: manually add a model row, fill ID, Save immediately disables. Criterion was correct (v2 requires
 * each model to declare a capability), but the page only said elsewhere that **discovered** models are not automatically marked Chat—
 * manual-entry users had discovered nothing and would assume it did not apply to them; missing name/address,
 * missing Azure version and invalid source identifier had **no explanation at all**.
 *
 * Save eligibility and its explanation now share one implementation (`saveEnabled === (saveBlockReason(...) === null)`),
 * so these tests enforce both the copy's source and the gate itself.
 */

import { describe, expect, it } from "vitest";

import {
  saveBlockReason,
  shouldReleaseSingleRowBatch,
  type SaveBlockReason,
  type SaveGateInput,
} from "./models";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

/** Button-adjacent copy uses `connect.blocked_<reason>`; a missing key prints the key itself in the interface. */
const ALL_REASONS: SaveBlockReason[] = [
  "name_missing",
  "name_format",
  "address_missing",
  "api_version_missing",
  "no_model",
  "duplicate_ids",
  "capability_missing",
  "outbound_value_missing",
  "overrides_invalid",
  "test_required",
];

const OK_OPTS = {
  v2Enabled: true,
  addressValid: true,
  protocolFieldsValid: true,
  v1TestSatisfied: true,
};

function draft(overrides: Partial<SaveGateInput> = {}): SaveGateInput {
  return {
    name: "example_provider",
    protocol: "openai_compatible",
    mode: "edit",
    models: [{ id: "gpt-5-luna", caps: ["chat"] }],
    ...overrides,
  };
}

describe("全满足", () => {
  it("★ 正对照：判据齐了就必须放行（否则下面每条红都不能说明问题）", () => {
    expect(saveBlockReason(draft(), OK_OPTS)).toBeNull();
  });

  it("能力齐全的手填模型同样放行 —— 手填本身从来不是障碍", () => {
    const manual = draft({ models: [{ id: "deepseek-v4-turbo", caps: ["chat"] }] });

    expect(saveBlockReason(manual, OK_OPTS)).toBeNull();
  });
});

describe("此前完全静默的四条", () => {
  it("没名字", () => {
    expect(saveBlockReason(draft({ name: "   " }), OK_OPTS)).toBe("name_missing");
  });

  it("来源标识含非法字符（v2）", () => {
    expect(saveBlockReason(draft({ name: "invalid provider!" }), OK_OPTS)).toBe("name_format");
    // Negative control: v1 does not enforce this.
    expect(saveBlockReason(draft({ name: "invalid provider!" }), { ...OK_OPTS, v2Enabled: false })).toBeNull();
  });

  it("地址没填", () => {
    expect(saveBlockReason(draft(), { ...OK_OPTS, addressValid: false })).toBe("address_missing");
  });

  it("Azure 缺 API Version", () => {
    expect(saveBlockReason(draft(), { ...OK_OPTS, protocolFieldsValid: false })).toBe("api_version_missing");
  });
});

describe("模型目录", () => {
  it("一个模型都没填", () => {
    expect(saveBlockReason(draft({ models: [{ id: "  ", caps: [] }] }), OK_OPTS)).toBe("no_model");
  });

  it("模型 ID 重复", () => {
    const dup = draft({ models: [{ id: "m", caps: ["chat"] }, { id: " m ", caps: ["chat"] }] });

    expect(saveBlockReason(dup, OK_OPTS)).toBe("duplicate_ids");
  });

  it("★ 用户报的那一幕：手动加行、填了 ID、一个能力都没勾", () => {
    const manual = draft({ models: [{ id: "deepseek-v4-turbo", caps: [] }] });

    expect(saveBlockReason(manual, OK_OPTS)).toBe("capability_missing");
  });

  it("负对照：v1 不要求显式能力", () => {
    const manual = draft({ models: [{ id: "deepseek-v4-turbo", caps: [] }] });

    expect(saveBlockReason(manual, { ...OK_OPTS, v2Enabled: false })).toBeNull();
  });

  it("负对照：空 ID 的行不算数（用户还没填完，别提前报错）", () => {
    const half = draft({ models: [{ id: "m", caps: ["chat"] }, { id: "", caps: [] }] });

    expect(saveBlockReason(half, OK_OPTS)).toBeNull();
  });
});

describe("声明与逃生口", () => {
  it("出站参数「固定为…」缺数值", () => {
    const bad = draft({
      models: [{ id: "m", caps: ["chat"], outboundParams: { temperature: { mode: "fixed" } } }],
    });

    expect(saveBlockReason(bad, OK_OPTS)).toBe("outbound_value_missing");
  });

  it("原始请求参数不是合法 JSON 对象", () => {
    const bad = draft({
      models: [{ id: "m", caps: ["chat"], requestBodyOverrides: "{ oops" }],
    });

    expect(saveBlockReason(bad, OK_OPTS)).toBe("overrides_invalid");
  });
});

describe("v1 的测试要求", () => {
  it("add 模式未测通", () => {
    expect(saveBlockReason(draft({ mode: "add" }), { ...OK_OPTS, v2Enabled: false, v1TestSatisfied: false }))
      .toBe("test_required");
  });

  it("负对照：v2 不看这条（v2 走验证计划，不走连接测试）", () => {
    expect(saveBlockReason(draft({ mode: "add" }), { ...OK_OPTS, v1TestSatisfied: false })).toBeNull();
  });
});

describe("理由是有序的", () => {
  it("同时坏了多处时，先说最靠前那条 —— 一次只让用户修一件事", () => {
    const messy = draft({ name: "", models: [{ id: "m", caps: [] }] });

    expect(saveBlockReason(messy, { ...OK_OPTS, addressValid: false })).toBe("name_missing");
  });
});

describe("每条理由都得有话可说", () => {
  it.each([["zh-CN", messagesZh], ["en-US", messagesEn]] as const)(
    "%s 两种语言都齐 —— 少一个 key，界面上印的就是 key 本身",
    (_locale, messages) => {
      const connect = (messages as Record<string, any>).settings.models.connect;

      for (const reason of ALL_REASONS) {
        const text = connect[`blocked_${reason}`];
        expect(text, reason).toBeTruthy();
        expect(text, reason).not.toContain("blocked_");
      }
    },
  );

  it("「先填写「{field}」」的占位符不能被删 —— 字段在 v2/v1 下叫法不同，写死就会指错框", () => {
    expect(messagesZh.settings.models.connect.blocked_name_missing).toContain("{field}");
    expect(messagesEn.settings.models.connect.blocked_name_missing).toContain("{field}");
  });

  it("★ 能力那条不再只讲「发现模型」——手填的人得知道说的是自己", () => {
    // Old copy: “发现模型不会自动标记为『对话』”. Users manually entered models and Save disabled;
    // that sentence seemed unrelated to them—the exact bug scenario.
    expect(messagesZh.settings.models.v2.capability_required_body).toContain("每个模型");
    expect(messagesEn.settings.models.v2.capability_required_body).toContain("each model");
  });
});


describe("单项验证建的批次必须被主动清掉", () => {
  /**
   * ★ P1 caught on real 116 (unit tests missed it because it lives in parent workflow state):
   * `verificationBatch` also determines whether verification workflow is open → `configurationActionsDisabled`.
   * Full-batch verification has a closeable panel; single-item does not—without clearing, Edit Configuration / Add Connection
   * **stay disabled forever with no explanation** after one verification, recovering only on page refresh.
   */
  const mine = { batch_id: "b_mine", state: "completed" };

  it("★ 自己建的批次到终态 → 该放行", () => {
    expect(shouldReleaseSingleRowBatch(mine, "b_mine")).toBe(true);
  });

  it.each(["completed", "cancelled", "interrupted"])(
    "三种终态都算结束（%s）",
    (state) => {
      expect(shouldReleaseSingleRowBatch({ batch_id: "b_mine", state }, "b_mine")).toBe(true);
    },
  );

  it("★ 还在跑就别清——清了行上的「正在验证」会提前消失", () => {
    expect(shouldReleaseSingleRowBatch({ batch_id: "b_mine", state: "running" }, "b_mine")).toBe(false);
    expect(shouldReleaseSingleRowBatch({ batch_id: "b_mine", state: "queued" }, "b_mine")).toBe(false);
  });

  it("★★ 别人的批次一律不动——否则会把整批验证的结果面板关掉，用户还没看呢", () => {
    expect(shouldReleaseSingleRowBatch(mine, "b_someone_else")).toBe(false);
    expect(shouldReleaseSingleRowBatch(mine, null)).toBe(false);
  });

  it("负对照：没有批次时无事可做", () => {
    expect(shouldReleaseSingleRowBatch(null, "b_mine")).toBe(false);
    expect(shouldReleaseSingleRowBatch(undefined, "b_mine")).toBe(false);
  });
});
