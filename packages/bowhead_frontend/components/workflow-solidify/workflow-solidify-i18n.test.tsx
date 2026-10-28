/**
 * Workflow-solidification surfaces follow the UI language: Chinese matches the original verbatim; English and German UIs contain no Chinese.
 *
 * wf-shared badges / stepper also serve real solidification cards (create-workflow-card) and team workflow settings;
 * other surfaces (solidification confirmation, turn card, management page, first-screen suggestions) are design previews at /spike/workflow-solidify.
 */
import type { ReactElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { ManagePage } from "./manage-page";
import { SolidifyCard } from "./solidify-card";
import { SuggestScreen } from "./suggestions";
import { TurnCard } from "./turn-chip";
import { FormBadge, ScriptBlock, StepLine, Stepper, VerdictBadge, VisBadge } from "./wf-shared";

afterEach(() => cleanup());

const HAN = /\p{Script=Han}/u;
type Locale = "zh-CN" | "en-US" | "de-DE";

function text(ui: ReactElement, locale: Locale): string {
  const { container } = renderIntl(ui, { locale });
  const out = container.textContent ?? "";
  cleanup();
  return out;
}

const SURFACES: Array<[string, ReactElement]> = [
  // Stepper active=1: step 0 is complete, drawing a check glyph (no text) rather than ①.
  ["shared", (
    <div>
      <Stepper active={1} />
      <FormBadge form="team-skill" />
      <VisBadge vis="team_wide" />
      <VerdictBadge verdict="error" />
      <ScriptBlock name="a.py" lines={["x"]} truncated />
      <StepLine n={2} truncated>s</StepLine>
    </div>
  )],
  ["card:step1", <SolidifyCard scenario="step1" />],
  ["card:conflict", <SolidifyCard scenario="conflict" />],
  ["card:oral", <SolidifyCard scenario="oral" />],
  ["card:dryrun-fail", <SolidifyCard scenario="dryrun-fail" />],
  ["turn:done", <TurnCard variant="done" />],
  ["manage:team", <ManagePage scope="team" scenario="expanded" />],
  ["suggest", <SuggestScreen />],
];

describe("workflow-solidify i18n", () => {
  it("zh-CN：与原中文逐字一致", () => {
    expect(text(SURFACES[0][1], "zh-CN")).toBe(
      "草稿预览②触发 · 参数③形态 · 可见完成team 共享 skill全 team 可见error · 没跑起来a.py部分2s按意图现场实现",
    );
    const card = text(<SolidifyCard scenario="step1" />, "zh-CN");
    expect(card).toContain("第 ② 步未抓到全文脚本——保留为「按意图现场实现」，不伪造半截代码。其余步骤已带可复用脚本。");
    expect(card).toContain("执行步骤body_md · 4 步");
    expect(card).toContain("触发：渠道毛利 / 口径核对 / 毛利异常");
    expect(text(<SolidifyCard scenario="conflict" />, "zh-CN")).toContain(
      "同 team 已存在名为 channel-margin-reconcile 的流程，换个名字再保存。",
    );
    expect(text(<ManagePage scope="team" />, "zh-CN")).toContain("林珂 · 6.2 KB · 更新于 昨天");
    expect(text(<ManagePage scope="personal" />, "zh-CN")).toContain("3 个流程");
  });

  it("en-US：英文文案，没有中文", () => {
    for (const [name, ui] of SURFACES) {
      expect(text(ui, "en-US"), name).not.toMatch(HAN);
    }
    expect(text(SURFACES[0][1], "en-US")).toBe(
      "Draft preview②Triggers · parameters③Form · visibilityDoneTeam-shared skillVisible to whole teamError · didn't runa.pyPartial2sImplemented on intent at runtime",
    );
    expect(text(<SolidifyCard scenario="step1" />, "en-US")).toContain("Stepsbody_md · 4 steps");
    expect(text(<ManagePage scope="team" />, "en-US")).toContain("3 workflows");
    expect(text(<SolidifyCard scenario="oral" />, "en-US")).toContain("Kept 2 verified scripts and the tool sequence");
  });

  it("de-DE：没有中文（未翻译处是英文占位）", () => {
    for (const [name, ui] of SURFACES) {
      expect(text(ui, "de-DE"), name).not.toMatch(HAN);
    }
    // For sentences shared with the live solidification card, reuse the German already reviewed there.
    expect(text(<SolidifyCard scenario="step3-skill" />, "de-DE")).toContain("Als wiederverwendbaren Ablauf festhalten");
  });
});
