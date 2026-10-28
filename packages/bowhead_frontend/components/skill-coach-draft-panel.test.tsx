/**
 * SkillCoachDraftPanel (skill-import batch 4) — coach-revision review panel.
 *
 * Enforce: untrusted banner first; real line-by-line diff; Apply blocked before “已逐行审阅” check;
 * four degradation types get distinct wording (especially list invented tools); panel never saves/changes state itself.
 */

import { cleanup, fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";
import type { SkillImportFinding } from "@/lib/api";
import type { SkillCoachDraft } from "@/lib/skill-coach-api";
import { SkillCoachDraftPanel } from "./skill-coach-draft-panel";

const BASE = "---\nname: onc-qbr\nallowed-tools: Read, data_fetch_skill\n---\n先读 references/x.md。";
const EDITED = "---\nname: onc-qbr\nallowed-tools: bi_query\n---\n先确定页面顺序（原文引用的文件没有随包上传）。";

const FINDINGS: SkillImportFinding[] = [
  {
    id: "f_1",
    code: "tool_ref_unresolved",
    severity: "red",
    path: "SKILL.md",
    line: null,
    subject: "data_fetch_skill",
    resolved_against: "tool_registry",
    message_zh: "声明要用的 data_fetch_skill 在这里不存在",
    message_en: "Declared tool does not exist here",
  },
  {
    id: "f_2",
    code: "frontmatter_gap",
    severity: "yellow",
    path: "SKILL.md",
    line: null,
    subject: "trigger_examples",
    resolved_against: "frontmatter",
    message_zh: "没有触发示例",
    message_en: "No trigger examples",
  },
];

const OK: SkillCoachDraft = {
  prefill: { kind: "user_skill_body", edited_content: EDITED, rationale: "我改了 allowed-tools；口径与数字没动；你还需要补传那份文件。" },
  base: { body_sha256: "abc" },
  report: { findings_addressed: ["f_1"], red_count: 1, yellow_count: 1, truncated: false },
};

const DEGRADED_BASE = { base: { body_sha256: "abc" } } as const;

function render(draft: SkillCoachDraft, onApply = vi.fn(), onDiscard = vi.fn()) {
  renderIntl(
    <SkillCoachDraftPanel draft={draft} baseline={BASE} findings={FINDINGS} locale="zh-CN" onApply={onApply} onDiscard={onDiscard} />,
  );
  return { onApply, onDiscard };
}

afterEach(cleanup);

describe("教练改稿面板", () => {
  it("横幅在最前，且明说来源不可信", () => {
    render(OK);
    const panel = screen.getByTestId("skill-coach-panel");
    const banner = screen.getByText(/来源不可信/);
    // Banner must precede diff/actions: explain origin before showing content.
    expect(panel.children[0].contains(banner)).toBe(true);
  });

  it("逐行对照真出 diff，不是摘要", () => {
    render(OK);
    const diff = screen.getByTestId("skill-coach-diff");
    expect(diff.querySelector('[data-kind="del"]')?.textContent).toContain("data_fetch_skill");
    expect(diff.querySelector('[data-kind="add"]')?.textContent).toContain("bi_query");
  });

  it("没勾「已逐行审阅」就不能应用", () => {
    const { onApply } = render(OK);
    const apply = screen.getByRole("button", { name: "用这份改稿" });
    expect(apply).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(apply).toHaveProperty("disabled", false);
    fireEvent.click(apply);
    expect(onApply).toHaveBeenCalledWith(EDITED);
  });

  it("教练的说明读的是 prefill.rationale——服务端的 report 里根本没有 notes", () => {
    render(OK);
    expect(screen.getByText(/你还需要补传那份文件/)).toBeTruthy();
  });

  it("只列教练自报处理过的那几条，按 id 匹配", () => {
    render(OK);
    expect(screen.getByText("声明要用的 data_fetch_skill 在这里不存在")).toBeTruthy();
    expect(screen.queryByText("没有触发示例")).toBeNull();
  });

  it("编造工具名被拦下时，把名字列出来", () => {
    render({ ...DEGRADED_BASE, degraded: true, degrade_reason: "invented_tool", invented: ["data_fetch_skill", "acme_api"] });
    expect(screen.getByText(/编一个名字填进去/)).toBeTruthy();
    expect(screen.getByText("acme_api")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "用这份改稿" })).toBeNull();
  });

  it("四种降级各说各的话，不共用一句「改不动」", () => {
    // Server has **seven** exits (review caught only four copied from own docs; common coach_unavailable exposed
    // i18n key to users). Check each routes/feedback_coach.py degrade_reason here.
    const REASONS = ["no_change", "invented_tool", "name_changed", "surface_too_large", "url_guard", "coach_unavailable", "coach_rerouted"] as const;
    const seen = new Set<string>();
    for (const reason of REASONS) {
      cleanup();
      render({ ...DEGRADED_BASE, degraded: true, degrade_reason: reason });
      const text = screen.getByTestId("skill-coach-degraded").textContent ?? "";
      expect(text).not.toContain("skill_coach."); // Never show users a key.
      // Check each wording, not only set size: removing a reason mapping silently falls back to
      // generic wording without changing size (review observed all 23 tests and tsc pass).
      expect(text).toContain(zhMessages.skill_coach[`degraded_${reason}` as keyof typeof zhMessages.skill_coach]);
      seen.add(text);
    }
    expect(seen.size).toBe(REASONS.length);

    // Future B-flow exit additions use generic wording, never a key.
    cleanup();
    render({ ...DEGRADED_BASE, degraded: true, degrade_reason: "something_new_from_b" });
    const unknown = screen.getByTestId("skill-coach-degraded").textContent ?? "";
    expect(unknown).not.toContain("skill_coach.");
    expect(unknown).toContain("没能完成");
  });
});

describe("陈旧基线（re-review P0-2：应用一份按旧内容写的稿会无声盖掉新改动）", () => {
  it("指纹对不上时只读：勾不了、也应用不了", async () => {
    const { bodySha256 } = await import("@/lib/skill-coach-api");
    const sha = await bodySha256(BASE);
    expect(sha).toBeTruthy(); // Criterion premise: environment can compute fingerprint.
    render({ ...OK, base: { body_sha256: "对不上的指纹" } });
    await screen.findByTestId("skill-coach-stale");
    expect(screen.getByRole("checkbox")).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "用这份改稿" })).toHaveProperty("disabled", true);
  });

  it("指纹对得上时照常可用", async () => {
    const { bodySha256 } = await import("@/lib/skill-coach-api");
    render({ ...OK, base: { body_sha256: (await bodySha256(BASE))! } });
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId("skill-coach-stale")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "用这份改稿" })).toHaveProperty("disabled", false);
  });

  it("基线在面板开着时被改过 ⇒ 勾选归零（同一标签页里手改保存就是这条路）", async () => {
    // ⚠ One provider only: renderIntl already wraps; another makes rerender change subtree type ⇒ component remount,
    //   resetting checkbox through a new instance rather than effect, making this previously falsely pass in review.
    const wrap = (baseline: string) => (
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <SkillCoachDraftPanel draft={OK} baseline={baseline} findings={FINDINGS} locale="zh-CN" onApply={vi.fn()} onDiscard={vi.fn()} />
      </NextIntlClientProvider>
    );
    const { rerender } = rtlRender(wrap(BASE));
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("checkbox")).toHaveProperty("checked", true);
    const before = screen.getByRole("checkbox");
    rerender(wrap(BASE + "\n用户刚手写的一段"));
    const after = screen.getByRole("checkbox");
    expect(after).toBe(before); // Same DOM node proves no remount; reset comes from effect.
    expect(after).toHaveProperty("checked", false);
  });
});

describe("fingerprints outside secure browser contexts", () => {
  it("没有 crypto.subtle 时仍算得出指纹，陈旧基线照样拦得住", async () => {
    // Remove subtle: browsers outside secure contexts behave this way (crypto itself is read-only getter; change its child property).
    const realSubtle = globalThis.crypto.subtle;
    Object.defineProperty(globalThis.crypto, "subtle", { value: undefined, configurable: true });
    try {
      const { bodySha256 } = await import("@/lib/skill-coach-api");
      const hex = await bodySha256(BASE);
      expect(hex).toMatch(/^[0-9a-f]{64}$/);
      render({ ...OK, base: { body_sha256: "对不上的指纹" } });
      await screen.findByTestId("skill-coach-stale");
      expect(screen.getByRole("button", { name: "用这份改稿" })).toHaveProperty("disabled", true);
    } finally {
      Object.defineProperty(globalThis.crypto, "subtle", { value: realSubtle, configurable: true });
    }
  });

  it("CRLF 与 LF 同文视为同一份，不因换行差异误报基线漂移", async () => {
    const { bodySha256 } = await import("@/lib/skill-coach-api");
    expect(await bodySha256("a\r\nb\r\n")).toBe(await bodySha256("a\nb\n"));
  });
});

describe("overridden 的翻回提示（re-review P2-6：此前只靠模型散文）", () => {
  it("起草时是 overridden ⇒ 硬编码提示保存后可能退回未生效", () => {
    render({ ...OK, base: { body_sha256: "abc", import_status: "overridden" } });
    expect(screen.getByTestId("skill-coach-overridden-warn")).toBeTruthy();
  });

  it("pending 不出这条", () => {
    render({ ...OK, base: { body_sha256: "abc", import_status: "pending" } });
    expect(screen.queryByTestId("skill-coach-overridden-warn")).toBeNull();
  });
});
