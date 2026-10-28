/**
 * Outbound parameters: three-state collapsed heading, protocol disabling, human/machine precedence.
 *
 * Design `docs/models-settings-outbound-params-design-brief-2026-08-18.md`.
 * Primary UI principle is **quiet**: 99% of teams never touch it; collapsed state must show no colored badges.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OutboundParamsSection } from "@/components/settings/models/outbound-params-section";
import messagesZh from "@/messages/zh-CN.json";
import type { OutboundParams } from "@/lib/outbound-params";

afterEach(cleanup);

function renderSection(overrides: Partial<React.ComponentProps<typeof OutboundParamsSection>> = {}) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <OutboundParamsSection
        index={0}
        providerKey="example-provider"
        modelId="openai_gpt56_luna"
        protocol="openai_compatible"
        params={{}}
        onChange={vi.fn()}
        rawOverrides={null}
        rawSlot={<textarea name="provider_model_overrides_0" defaultValue="{}" />}
        {...overrides}
      />
    </NextIntlClientProvider>,
  );
}

const ADAPTED = [
  {
    provider: "example-provider",
    model_id: "openai_gpt56_luna",
    param: "temperature",
    since: "2026-08-17T10:02:19+00:00",
  },
];

describe("收起头三态", () => {
  it("默认安静：只说「跟随默认」，不出现任何计数徽标", () => {
    const { container } = renderSection();

    // Collapsed heading here (Chinese shares wording with the dropdown default; English uses Using defaults / Use default respectively).
    expect(container.querySelector(".mset-outbound__quiet")?.textContent).toBe("跟随默认");
    expect(container.querySelector(".mset-outbound__chip")).toBeNull();
    expect(screen.queryByText(/已设/)).toBeNull();
    expect(screen.queryByText(/已自动适配/)).toBeNull();
  });

  it("人设过 → 中性「已设 N 项」", () => {
    renderSection({ params: { temperature: { mode: "fixed", value: 1 } } });

    expect(screen.getByText("已设 1 项")).toBeTruthy();
  });

  it("机器改过 → 暖色「已自动适配 N 项」（唯一需要吸引注意的态）", () => {
    renderSection({ adaptations: ADAPTED });

    expect(screen.getByText("已自动适配 1 项")).toBeTruthy();
  });
});

describe("协议差异（与后端保存期校验同一判据）", () => {
  it("Anthropic 的单次输出上限没有「不发送」这一档，并说明原因", () => {
    renderSection({ protocol: "anthropic_compatible" });

    const select = screen.getByLabelText("单次输出上限 — 出站参数") as HTMLSelectElement;
    const options = [...select.options].map((option) => option.textContent);
    expect(options).not.toContain("不发送");
    expect(screen.getByText("这个协议要求必须带上限，不能不发送。")).toBeTruthy();
  });

  it("负对照：openai 面同一行有「不发送」", () => {
    renderSection({ protocol: "openai_compatible" });

    const select = screen.getByLabelText("单次输出上限 — 出站参数") as HTMLSelectElement;
    expect([...select.options].map((option) => option.textContent)).toContain("不发送");
  });
});

describe("自动适配说明条", () => {
  it("说的是「服务拒绝了、我们已自动不发送」，不是「已帮你改好配置」", () => {
    renderSection({ adaptations: ADAPTED });

    expect(screen.getByText("模型服务拒绝了「随机性」，我们已自动不发送它。")).toBeTruthy();
    expect(screen.getByText("2026-08-17 起生效")).toBeTruthy();
    expect(screen.getByText("固化为配置")).toBeTruthy();
  });

  it("★ 人表过态之后不再显示成「机器改过」", () => {
    // Runtime no longer intervenes here; still showing adaptation would claim behavior that no longer happens.
    renderSection({
      adaptations: ADAPTED,
      params: { temperature: { mode: "fixed", value: 1 } },
    });

    expect(screen.queryByText(/已自动适配/)).toBeNull();
    expect(screen.getByText("已设 1 项")).toBeTruthy();
  });

  it("「固化为配置」写的是「不发送」——把观测变成人的声明", () => {
    const onChange = vi.fn();
    renderSection({ adaptations: ADAPTED, onChange });

    screen.getByText("固化为配置").click();

    expect(onChange).toHaveBeenCalledWith({ temperature: { mode: "omit", value: undefined } });
  });
});

describe("逃生口", () => {
  it("原始 JSON 收在下一层，但仍在这个区里", () => {
    renderSection();

    expect(screen.getByText("原始请求参数（高级）")).toBeTruthy();
    expect(document.querySelector("textarea[name='provider_model_overrides_0']")).not.toBeNull();
  });

  it("与声明撞键时说出来（并说明以哪边为准）", () => {
    renderSection({
      params: { temperature: { mode: "omit" } },
      rawOverrides: { chat: { temperature: 0.4 } },
    });

    expect(screen.getByText("「temperature」在上面已经设过了，以上面的设定为准。")).toBeTruthy();
  });

  it("负对照：没撞键就不提示", () => {
    renderSection({
      params: { temperature: { mode: "omit" } },
      rawOverrides: { chat: { enable_thinking: false } },
    });

    expect(screen.queryByText(/已经设过了/)).toBeNull();
  });
});

describe("逃生口对不消费它的协议整层不渲染", () => {
  it("rawSlot=null 时没有空的「原始请求参数」展开层，四行参数照常在", () => {
    // Anthropic-compatible providers consume declarations but not raw JSON; retaining an expandable layer where nothing can change
    // is exactly the false affordance this section addresses.
    const { container } = renderSection({ protocol: "anthropic_compatible", rawSlot: null });

    expect(screen.queryByText("原始请求参数（高级）")).toBeNull();
    expect(container.querySelectorAll(".mset-outbound__row")).toHaveLength(4);
  });

  it("负对照：openai 面照常有逃生口", () => {
    renderSection({ protocol: "openai_compatible" });

    expect(screen.getByText("原始请求参数（高级）")).toBeTruthy();
  });
});

describe("值输入", () => {
  it("「固定为…」缺数值时标错，并说人话", () => {
    renderSection({ params: { temperature: { mode: "fixed" } } });

    expect(screen.getByText("请填写有效值。")).toBeTruthy();
  });

  it("「不发送」不给输入框，改说由谁决定", () => {
    renderSection({ params: { temperature: { mode: "omit" } } });

    expect(screen.getAllByText("由模型服务自己决定").length).toBeGreaterThan(0);
  });
});
