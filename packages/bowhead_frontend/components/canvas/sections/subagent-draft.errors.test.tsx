/** Agent intent card and draft review: failures show only localized explanations, never “Failed to fetch” or raw API text; Copy Diagnostics stays outside the alert. */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({ generate: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/subagent-drafts-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/subagent-drafts-api")>()),
  generateSubagentDraft: (...args: unknown[]) => mocks.generate(...args),
  getSubagentDraft: (...args: unknown[]) => mocks.get(...args),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => null }));
vi.mock("@/lib/use-app-router", async (importOriginal) => ({
  // Keep the real BEFORE_APP_NAVIGATION_EVENT: the review registers a Canvas navigation guard on mount.
  ...(await importOriginal<typeof import("@/lib/use-app-router")>()),
  useAppRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/lib/agents-api", () => ({ getAgentAuthoringCatalog: vi.fn(async () => ({ tools: [] })) }));

import { SubagentDraftApiError } from "@/lib/subagent-drafts-api";
import { SubagentDraftReview } from "./subagent-draft-review";
import { SubagentIntentCard } from "./subagent-intent-card";
import { subagentDraftFailureKey } from "./subagent-draft-errors";

const LOCALES = [
  ["zh-CN", zh, "复制诊断"],
  ["en-US", en, "Copy diagnostics"],
  ["de-DE", de, "Diagnoseinformationen kopieren"],
] as const;
const RAW = /Failed to fetch|raw server prose|\b[45]\d\d\b/;

afterEach(() => {
  cleanup();
  mocks.generate.mockReset();
  mocks.get.mockReset();
});

describe("subagent intent card", () => {
  it.each(LOCALES)("%s: a dropped connection is explained, never shown as 'Failed to fetch'", async (locale, catalog) => {
    mocks.generate.mockRejectedValue(new TypeError("Failed to fetch"));
    renderIntl(<SubagentIntentCard initialPurpose="每周分析门店销售异常" onDraft={vi.fn()} onManual={vi.fn()} onClose={vi.fn()} />, { locale });

    fireEvent.click(screen.getByRole("button", { name: catalog.settings.subagents.intent_generate }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(catalog.settings.subagents.draft_failed_network);
    expect(alert.textContent).not.toMatch(RAW);
  });

  it.each(LOCALES)("%s: a server failure offers Copy diagnostics outside the alert", async (locale, catalog, copy) => {
    mocks.generate.mockRejectedValue(new SubagentDraftApiError(500, undefined, "raw server prose"));
    renderIntl(<SubagentIntentCard initialPurpose="每周分析门店销售异常" onDraft={vi.fn()} onManual={vi.fn()} onClose={vi.fn()} />, { locale });

    fireEvent.click(screen.getByRole("button", { name: catalog.settings.subagents.intent_generate }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).not.toMatch(RAW);
    const button = screen.getByRole("button", { name: copy });
    expect(alert.contains(button)).toBe(false);
  });
});

describe("subagent draft review", () => {
  it.each(LOCALES)("%s: a failed load is explained, with Copy diagnostics outside the alert", async (locale, catalog, copy) => {
    mocks.get.mockRejectedValue(new SubagentDraftApiError(500, undefined, "raw server prose"));
    renderIntl(<SubagentDraftReview draftId="sad_1" onClose={vi.fn()} />, { locale });

    const alert = await screen.findByText(catalog.settings.subagents.draft_failed_plain);
    expect(alert.closest("[role=alert]")?.textContent).not.toMatch(RAW);
    const button = await screen.findByRole("button", { name: copy });
    expect(alert.closest("[role=alert]")?.contains(button)).toBe(false);
  });

  it("a dropped connection while loading says to check the network", async () => {
    mocks.get.mockRejectedValue(new TypeError("Failed to fetch"));
    renderIntl(<SubagentDraftReview draftId="sad_1" onClose={vi.fn()} />);

    expect(await screen.findByText(zh.settings.subagents.draft_failed_network)).toBeTruthy();
    expect(document.body.textContent).not.toContain("Failed to fetch");
  });
});

describe("subagentDraftFailureKey", () => {
  it.each([
    [{ status: 409, code: "team_model_unconfigured" }, "intent_model_unconfigured"],
    [{ status: 503, code: "subagent_draft_store_unavailable" }, "draft_store_unavailable"],
    [{ status: 409, code: "subagent_draft_version_conflict" }, "draft_conflict"],
    [{ status: 422 }, "draft_fix_validation"],
    [{ status: 403 }, "draft_failed_forbidden"],
    [{ status: 404 }, "draft_failed_gone"],
    [{ status: 429 }, "draft_failed_too_frequent"],
    [{ status: 500 }, "draft_failed_plain"],
    [new TypeError("Failed to fetch"), "draft_failed_network"],
  ] as const)("%o → %s", (error, key) => {
    expect(subagentDraftFailureKey(error)).toBe(key);
  });
});
