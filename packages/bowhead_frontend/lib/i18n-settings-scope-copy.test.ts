import { describe, expect, it } from "vitest";


import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

describe("settings scope copy", () => {
  it("describes Skills as a personal enablement surface in both locales", () => {
    expect(en.skills_page.subtitle).toMatch(/your sessions/i);
    expect(en.skills_page.subtitle).not.toMatch(/this space/i);

    expect(zh.skills_page.subtitle).toContain("个人会话");
    expect(zh.skills_page.subtitle).not.toContain("这个空间");
    expect([
      zh.skills_page.title,
      zh.skills_page.upload_btn,
      zh.skills_page.search_placeholder,
      zh.skills_page.group.mine,
    ].join(" ")).not.toMatch(/skill/i);
  });

  it("describes team skills as team assets with per-user switches", () => {
    expect(en.skills_page.group.shared).toBe("Team skills");
    expect(en.skills_page.group.shared_hint).toMatch(/your own sessions/i);
    expect(en.skills_page.group.shared_hint).toMatch(/audience/i);
    expect(en.skills_page.delete_confirm.team_wide_body).toMatch(/whole team/i);
    expect(en.skills_page.delete_confirm.custom_body).toMatch(/shared/i);
    expect(en.skills_page.delete_confirm.private_body).toMatch(/creator/i);

    expect(zh.skills_page.group.shared).toBe("团队技能");
    expect(zh.skills_page.group.shared_hint).toContain("只影响你自己的会话");
    expect(zh.skills_page.group.shared_hint).toContain("受众");
    expect(zh.skills_page.delete_confirm.team_wide_body).toContain("全团队");
    expect(zh.skills_page.delete_confirm.custom_body).toContain("分享");
    expect(zh.skills_page.delete_confirm.private_body).toContain("创建者");
  });

  it("describes team tool sources as team-scoped in both locales", () => {
    expect(en.settings.tools.subtitle).toMatch(/team/i);
    expect(en.settings.tools.subtitle).not.toMatch(/this space/i);
    expect(en.settings.tools.confirm_delete_desc).toMatch(/across the team/i);

    expect(zh.settings.tools.subtitle).toContain("团队");
    expect(zh.settings.tools.subtitle).not.toContain("这个空间");
    expect(zh.settings.tools.confirm_delete_desc).toContain("整个团队");
    // With the resource-impact guard enabled, deletion confirmation uses the impact preview's opening sentence, still specifying team scope
    expect(en.settings.tools.impact_delete_source_lead).toMatch(/across the team/i);
    expect(zh.settings.tools.impact_delete_source_lead).toContain("整个团队");
  });
});
