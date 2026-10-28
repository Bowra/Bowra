/**
 * Composer handoff across pages.
 *
 * A one-shot Skill-edit prefill must be consumed on the destination page, before switching sessions.
 *
 * Root cause is the **consumer's location**, not prefill content: the one-shot key was consumed by `ChatWorkbench`'s mount
 * effect, but it mounts only with `runner.started || showingReportOnly`; the skill drawer navigates
 * to an empty-state home page where the component never mounts ⇒ nobody consumes the key in sessionStorage; opening any
 * existing session later mounts the workbench, which picks it up.
 *
 * The repository hit the same trap before (attachment prestaging in file-management-v2 P2, caught by 0.87.0 prod dogfood);
 * that fix moved consumption up to the unconditionally mounted page. The skill key did not move with it.
 *
 * The second group below is a **source-level drift guard**: unit tests cannot prove who reads the key, but that is
 * the entire bug, so lock the source facts directly.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { act, renderHook } from "@testing-library/react";
import { StrictMode } from "react";

import {
  COMPOSER_AGENT_USE_KEY,
  composerAgentHandoffAdmitted,
  COMPOSER_PREFILL_KEY,
  takeComposerAgentUse,
  takeComposerPrefill,
  useComposerAgentUseHandoff,
} from "./composer-handoff";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative: string) => readFileSync(join(ROOT, relative), "utf8");

afterEach(() => vi.restoreAllMocks());

describe("takeComposerPrefill", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("读到就返回", () => {
    sessionStorage.setItem(COMPOSER_PREFILL_KEY, "Help me update the example-router Skill:");
    expect(takeComposerPrefill()).toBe("Help me update the example-router Skill:");
  });

  it("★ 读到即删 —— 留着就是下一个会话的污染源，这正是原 bug", () => {
    sessionStorage.setItem(COMPOSER_PREFILL_KEY, "帮我修改技能 X：");
    takeComposerPrefill();
    expect(sessionStorage.getItem(COMPOSER_PREFILL_KEY)).toBeNull();
    expect(takeComposerPrefill()).toBeNull();
  });

  it("★ 空白串也要删掉，不能当「没有」就留在那儿", () => {
    sessionStorage.setItem(COMPOSER_PREFILL_KEY, "   ");
    expect(takeComposerPrefill()).toBeNull();
    expect(sessionStorage.getItem(COMPOSER_PREFILL_KEY)).toBeNull();
  });

  it("没有键时返回 null", () => {
    expect(takeComposerPrefill()).toBeNull();
  });

  it("storage 不可用（隐私模式等）不得抛", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(() => takeComposerPrefill()).not.toThrow();
    expect(takeComposerPrefill()).toBeNull();
  });
});

describe("takeComposerAgentUse", () => {
  beforeEach(() => sessionStorage.clear());

  it("consumes a valid Agent identity exactly once", () => {
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify({ agentId: "osa_1", label: "公告核对" }));
    expect(takeComposerAgentUse()).toEqual({ agentId: "osa_1", label: "公告核对" });
    expect(takeComposerAgentUse()).toBeNull();
  });

  it("carries a system Agent and the request staged after its reference", () => {
    sessionStorage.setItem(
      COMPOSER_AGENT_USE_KEY,
      JSON.stringify({ agentId: "system:agent-coach", label: "Coach", text: "Help me change the Agent “Finance Helper”: " }),
    );
    expect(takeComposerAgentUse()).toEqual({
      agentId: "system:agent-coach",
      label: "Coach",
      text: "Help me change the Agent “Finance Helper”: ",
    });
  });

  it("drops a staged request that is not text or is too long", () => {
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify({ agentId: "system:agent-coach", label: "Coach", text: 42 }));
    expect(takeComposerAgentUse()).toBeNull();
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify({ agentId: "system:agent-coach", label: "Coach", text: "x".repeat(8001) }));
    expect(takeComposerAgentUse()).toBeNull();
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify({ agentId: "system:agent-coach", label: "Coach", text: "   " }));
    expect(takeComposerAgentUse()).toEqual({ agentId: "system:agent-coach", label: "Coach" });
  });

  it("drops malformed identities before they reach the reference catalog", () => {
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify({ agentId: "../other", label: "Agent" }));
    expect(takeComposerAgentUse()).toBeNull();
    expect(sessionStorage.getItem(COMPOSER_AGENT_USE_KEY)).toBeNull();
  });
});

describe("useComposerAgentUseHandoff", () => {
  beforeEach(() => sessionStorage.clear());
  const coach = { agentId: "system:agent-coach", label: "Coach", text: "Help me change the Agent “Finance Helper”: " };

  it("keeps an Ask Coach handoff through Strict Mode's double mount until it is staged", () => {
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify(coach));
    const { result } = renderHook(() => useComposerAgentUseHandoff(), { wrapper: StrictMode });

    expect(result.current.pending).toEqual(coach);
    act(() => result.current.settle());
    expect(result.current.pending).toBeNull();
    expect(sessionStorage.getItem(COMPOSER_AGENT_USE_KEY)).toBeNull();
  });

  it("offers the same handoff to a page that remounts before staging it, and only once after", () => {
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify(coach));
    renderHook(() => useComposerAgentUseHandoff()).unmount();
    const remounted = renderHook(() => useComposerAgentUseHandoff());
    expect(remounted.result.current.pending).toEqual(coach);
    act(() => remounted.result.current.settle());
    remounted.unmount();
    expect(renderHook(() => useComposerAgentUseHandoff()).result.current.pending).toBeNull();
  });
});

describe("composerAgentHandoffAdmitted", () => {
  it("binds @Coach whenever reference input is ready, even with the Start use switch off", () => {
    // 116 dogfood of 1.5.84: Ask Coach to change this opened Home without @Coach because the destination
    // also required the unrelated `agent_start_use` switch, which is off by default.
    expect(composerAgentHandoffAdmitted("system:agent-coach", { reference_input: true, agent_start_use: false })).toBe(true);
    expect(composerAgentHandoffAdmitted("system:agent-coach", { reference_input: false, agent_start_use: true })).toBe(false);
  });

  it("keeps Start use of other Agents behind its own switch", () => {
    expect(composerAgentHandoffAdmitted("osa_1", { reference_input: true, agent_start_use: false })).toBe(false);
    expect(composerAgentHandoffAdmitted("osa_1", { reference_input: true, agent_start_use: true })).toBe(true);
    expect(composerAgentHandoffAdmitted("osa_1", undefined)).toBe(false);
  });

  it("is the gate the home page applies to a pending handoff", () => {
    expect(read("app/(app)/page.tsx")).toContain("composerAgentHandoffAdmitted(pendingAgentUse.agentId");
  });
});

describe("★ 消费点必须挂在无条件渲染的页面上（漂移闸）", () => {
  it("ChatWorkbench 不得再读这个键", () => {
    // It mounts only with runner.started || showingReportOnly; reading here means the empty-state home-page
    // path never consumes the key, leaving it for the next session to pick up.
    expect(read("components/chat-workbench.tsx")).not.toContain("getItem(COMPOSER_PREFILL_KEY)");
    expect(read("components/chat-workbench.tsx")).not.toContain('"bowhead:composer-prefill"');
  });

  it("页面这一层必须读它", () => {
    const page = read("app/(app)/page.tsx");
    expect(page).toContain("takeComposerPrefill");
  });

  it("★ 键名只有一处定义 —— 两边各写一遍字面量正是上次改漏一边的原因", () => {
    const writer = read("components/settings/skills-settings-content.tsx");
    expect(writer).toContain("COMPOSER_PREFILL_KEY");
    expect(writer).not.toContain('"bowhead:composer-prefill"');
  });
});
