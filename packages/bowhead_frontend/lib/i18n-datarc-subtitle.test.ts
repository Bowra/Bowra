/**
 * Copy contract for settings.datarc.subtitle (data-source page subtitle).
 *
 * Since ed543df60 (unified data-source addition and listing), the subtitle covers connection management alone, without describing in-chat Datarc workspace reselection
 * (the hint belongs in chat clarification cards). After Power BI moved to team-built-in third-party tools, the subtitle points to Tools.
 */

import { describe, expect, it } from "vitest";

import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

type Bundle = { settings: { datarc: { subtitle: string } } };

describe("settings.datarc.subtitle", () => {
  it("Power BI 连接指向「工具」（Power BI 已迁入团队内置第三方工具）", () => {
    const zhSubtitle = (zh as unknown as Bundle).settings.datarc.subtitle;
    const enSubtitle = (en as unknown as Bundle).settings.datarc.subtitle;
    expect(zhSubtitle).toMatch(/Power BI.*工具/);
    expect(enSubtitle).toMatch(/Power BI.*Tools/);
  });

  it("术语 P0：zh 副标题只用「空间」「北极九章工作区」词族", () => {
    const subtitle = (zh as unknown as Bundle).settings.datarc.subtitle;
    expect(subtitle).toContain("北极九章");
    expect(subtitle).toContain("空间");
  });
});
