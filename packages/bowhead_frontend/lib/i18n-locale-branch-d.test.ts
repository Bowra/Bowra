/**
 * Batch D: out-of-tree copy that used Chinese/other or English/other branches now reads messages by UI language.
 *
 * Every entry calls the real function: Chinese UI reads the Chinese catalog, English the English catalog, German the German catalog
 * (untranslated keys have English placeholders)—German UI must contain no Han characters.
 * Reverting to binary branches reintroduces Chinese in these files, caught by hardcoded-han-guard's zero-Han list;
 * this file catches German falling back to Chinese.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";

const confirmCalls = vi.hoisted(() => [] as Array<Record<string, unknown>>);
const toastCalls = vi.hoisted(() => [] as Array<{ text: string }>);
vi.mock("./confirm-dialog-store", () => ({
  requestConfirm: vi.fn(async (options: Record<string, unknown>) => {
    confirmCalls.push(options);
    return true;
  }),
}));
vi.mock("./use-toast", () => ({ toast: (item: { text: string }) => toastCalls.push(item) }));
vi.mock("@/lib/use-toast", () => ({ toast: (item: { text: string }) => toastCalls.push(item) }));

import { agentDisplayName } from "@/components/chat/step-list/agent-meta";
import { ApiError } from "./api-client";
import { loginUser } from "./auth-api";
import { confirmCanvasNavigation, registerCanvasNavigationGuard } from "./canvas-navigation-guard";
import { sectionDeleteBlockedText } from "./canvas-pm-adapter/extensions/section-delete-guard";
import type { Locale } from "./locale";
import { reportSaveCopyLabels, reportSaveFailureMessage } from "./report-save-errors";
import { skillWorkflowErrorPresentation } from "./skill-workflow-error";
import { createSubagent } from "./team-subagents-api";

const CATALOGS = { "zh-CN": zhMessages, "en-US": enMessages, "de-DE": deMessages, "fr-FR": frMessages } as const;
const HAN = /\p{Script=Han}/u;

async function collectOutputs(): Promise<string[]> {
  const out: string[] = [];
  out.push(agentDisplayName("compute"), agentDisplayName("debate"), agentDisplayName("lead"));
  out.push(sectionDeleteBlockedText("block_multi"), sectionDeleteBlockedText("block_menu"), sectionDeleteBlockedText("block_unsupported"));
  out.push(
    reportSaveFailureMessage({ status: 401 }),
    reportSaveFailureMessage({ status: 403 }),
    reportSaveFailureMessage(new TypeError("offline")),
    reportSaveFailureMessage({ status: 500 }),
  );
  out.push(...Object.values(reportSaveCopyLabels()));
  out.push(
    skillWorkflowErrorPresentation(
      new ApiError(403, "ws_access_denied:not_a_member", { code: "ws_access_denied:not_a_member" }),
      { surface: "settings", copy: { generic: "G", transport: { requestTimedOut: "T", serviceUnreachable: "S", retry: "R" } } },
    ).message,
  );
  for (const detail of [
    { code: "subagent_tool_not_selectable", tool_ref: "skill_list" },
    { code: "subagent_tool_not_selectable", tool_ref: "web_search" },
    { code: "agent_dependency_blocked", reason_codes: ["precise_skill_scope_required"] },
    { code: "agent_dependency_blocked", reason_codes: [] },
    { code: "display_name_unavailable" },
    { code: "builtin_override_store_unavailable" },
  ]) {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ detail }), { status: 422, headers: { "content-type": "application/json" } })));
    await createSubagent({ name: "x", tool_refs: [] }).catch((error: Error) => out.push(error.message));
  }
  vi.stubGlobal("fetch", vi.fn(async () => {
    throw new TypeError("Failed to fetch");
  }));
  await loginUser("u@example.com", "pw").catch((error: Error) => out.push(error.message));
  confirmCalls.length = 0;
  toastCalls.length = 0;
  const off = registerCanvasNavigationGuard({ hasUnsaved: () => true, flush: async () => ({ ok: false }) });
  await confirmCanvasNavigation();
  off();
  for (const call of confirmCalls) {
    out.push(...["title", "description", "confirmLabel", "cancelLabel"].map((key) => String(call[key])));
  }
  out.push(...toastCalls.map((item) => item.text));
  return out;
}

describe("D 批：树外文案按界面语言取词", () => {
  beforeEach(() => {
    document.documentElement.lang = "";
  });
  afterEach(() => {
    document.documentElement.lang = "";
    vi.unstubAllGlobals();
  });

  it.each(["de-DE", "fr-FR"])("%s 界面一个汉字都没有", async (locale) => {
    document.documentElement.lang = locale;
    const outputs = await collectOutputs();
    // Guard against vacuous checks: every entry actually produced text.
    expect(outputs).toHaveLength(27);
    expect(outputs.every((text) => text.length > 0)).toBe(true);
    expect(outputs.filter((text) => HAN.test(text))).toEqual([]);
  });

  it("英文界面一个汉字都没有，中文界面确实是中文", async () => {
    document.documentElement.lang = "en-US";
    const english = await collectOutputs();
    expect(english.filter((text) => HAN.test(text))).toEqual([]);

    document.documentElement.lang = "zh-CN";
    const chinese = await collectOutputs();
    // Everything is Chinese except Lead's brand name "Bowhead".
    expect(chinese.filter((text) => !HAN.test(text))).toEqual(["Bowhead"]);
  });

  it.each(["zh-CN", "en-US", "de-DE", "fr-FR"] as Locale[])("%s 的取值就是该语言目录里的那一条", async (locale) => {
    document.documentElement.lang = locale;
    const catalog = CATALOGS[locale as keyof typeof CATALOGS];
    expect(agentDisplayName("compute")).toBe(catalog.agents_marketplace.system_compute_name);
    expect(sectionDeleteBlockedText("block_menu")).toBe(catalog.section_delete_guard.block_menu);
    expect(reportSaveFailureMessage({ status: 403 })).toBe(catalog.report_save_errors.forbidden);
    expect(reportSaveCopyLabels().copyFallbackPrompt).toBe(catalog.errors.diagnostics.copyFallbackPrompt);
  });
});
