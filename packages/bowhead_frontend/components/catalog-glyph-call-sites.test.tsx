// Call sites whose icon glyph moved out of messages/*.json into `<CharIcon>`: in every locale the
// glyph is a Lucide svg in the place the character had, with the same spacing and accessible name.
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  GLYPH_LOCALES,
  type GlyphLocale,
  catalogText,
  expectRefreshedGlyph,
} from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";
import { _resetEntitlement, applyEntitlementDenial, applyEntitlementHeaders } from "@/lib/entitlement-store";

const mocks = vi.hoisted(() => ({
  resolveClarification: vi.fn(),
  cancelClarificationDirective: vi.fn(),
  draftKnowledgeTableFields: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  resolveClarification: mocks.resolveClarification,
  cancelClarificationDirective: mocks.cancelClarificationDirective,
}));
vi.mock("@/lib/kb-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/kb-api")>()),
  bulkChangeKbFilesScope: vi.fn(),
}));
vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({ useKbChunkContent: () => ({ status: "idle" }) }));
vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>()),
  draftKnowledgeTableFields: mocks.draftKnowledgeTableFields,
}));
vi.mock("@/lib/use-space", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/use-space")>()),
  useSpaceSelection: () => ({ activeId: "ws_1" }),
}));

import { SaveToMemoryButton } from "@/components/canvas/save-to-memory-button";
import { ClarificationCard as CanvasClarificationCard } from "@/components/canvas/sections/clarification-card";
import { ExternalIframeSection } from "@/components/canvas/sections/external-iframe";
import { FormulaConfirmCard } from "@/components/canvas/sections/formula-confirm-card";
import { RecommendationsSection } from "@/components/canvas/sections/recommendations";
import { HighlightedBlock } from "@/components/canvas/source-panel";
import { UnverifiedNumbersNote } from "@/components/canvas/unverified-numbers-note";
import { EntitlementBanner } from "@/components/chat/entitlement-banner";
import { DraftWizard } from "@/components/knowledge/extraction-template-wizard";
import { KbScopeModal } from "@/components/knowledge/kb-scope-modal";
import { SidePanel } from "@/components/knowledge/matrix/side-panel";

beforeEach(() => _resetEntitlement());
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetEntitlement();
  vi.unstubAllGlobals();
});

type Case = { locale: GlyphLocale };
const CASES: Case[] = GLYPH_LOCALES.map((locale) => ({ locale }));

function check(_c: Case, el: Element, glyph: string, at: "start" | "end", message: string, exact = true): void {
  expectRefreshedGlyph(el, glyph, at, message, { exact });
}

describe.each(CASES.map((c) => [c.locale, c] as const))("%s", (_name, c) => {
  it("save-to-memory button: 📌 before the label", () => {
    renderIntl(<SaveToMemoryButton canvasId="cnv_1" sectionId="sec_1" />, { locale: c.locale });
    check(c, screen.getByTestId("save-to-memory-btn"), "📌", "start", catalogText(c.locale, "canvas.stm_button"));
  });

  it("unverified numbers note: ⚠ before the warning", () => {
    const { container } = renderIntl(<UnverifiedNumbersNote numbers={["42"]} />, { locale: c.locale });
    const label = container.querySelector(".v2-narrative-unverified > span") as HTMLElement;
    check(c, label, "⚠", "start", catalogText(c.locale, "canvas.unverified_numbers_warning"));
  });

  it("source panel: ⚠ before the unmatched-highlight note", () => {
    const { container } = renderIntl(<HighlightedBlock content="unrelated" highlight="missing" />, { locale: c.locale });
    const note = container.querySelector(".legacy-source-panel-unmatched") as HTMLElement;
    check(c, note, "⚠", "start", catalogText(c.locale, "source_panel.highlight_not_matched"), false);
    expect(note.textContent?.startsWith(" ")).toBe(true);
  });

  it("external iframe: → after the open-in-new-tab link", () => {
    const { container } = renderIntl(
      <ExternalIframeSection data={{ id: "s1", kind: "external_iframe", url: "/api/v1/files/f_1", title: "Example" } as never} />,
      { locale: c.locale },
    );
    expect(container.querySelector("iframe")).toBeNull();
    const link = screen.getByRole("link", { name: new RegExp(catalogText(c.locale, "external_iframe.open_new_tab_link")) });
    check(c, link, "→", "end", catalogText(c.locale, "external_iframe.open_new_tab_link"));
  });

  it("recommendations: 🛠️ before the heading", () => {
    const { container } = renderIntl(
      <RecommendationsSection data={{ id: "r1", kind: "recommendations", items: [{ action: "Do it", priority: "P1" }] } as never} />,
      { locale: c.locale },
    );
    const head = container.querySelector(".rc-rec-head-row") as HTMLElement;
    check(c, head, "🛠️", "start", `${catalogText(c.locale, "recommendations.head")} · ranked`);
  });

  it("entitlement banner: ↗ after the renewal and suspension links", () => {
    act(() => applyEntitlementHeaders({ headers: new Headers({ "X-Entitlement-Health": "grace", "X-Entitlement-Grace-Until": "2026-06-30" }) }));
    const grace = renderIntl(<EntitlementBanner />, { locale: c.locale });
    check(c, grace.container.querySelector(".ent-banner__link") as HTMLElement, "↗", "end", catalogText(c.locale, "entitlement.contact_sales_renew"));
    grace.unmount();

    act(() => applyEntitlementDenial("over_grace"));
    const over = renderIntl(<EntitlementBanner />, { locale: c.locale });
    check(c, over.container.querySelector(".ent-banner__link") as HTMLElement, "↗", "end", catalogText(c.locale, "entitlement.contact_sales_renew"));
    over.unmount();

    act(() => applyEntitlementDenial("suspended"));
    const suspended = renderIntl(<EntitlementBanner />, { locale: c.locale });
    check(c, suspended.container.querySelector(".ent-banner__link") as HTMLElement, "↗", "end", catalogText(c.locale, "entitlement.view_suspend_reason"));
  });

  it("knowledge scope modal: 🔒 on the team card and ← on the back button", () => {
    renderIntl(
      <KbScopeModal
        targets={[{ file_id: "kbf_1", filename: "a.pdf", scope: "personal" } as never]}
        bulk={false}
        me={{ user_id: "u_1", team_id: "t_1", is_team_admin: false, workspace_id: "ws_1" } as never}
        onClose={vi.fn()}
        onMigrated={vi.fn()}
        toast={vi.fn()}
      />,
      { locale: c.locale },
    );
    const lock = screen.getByTestId("kb-scope-modal-team").querySelector(".lock") as HTMLElement;
    check(c, lock, "🔒", "start", catalogText(c.locale, "knowledge_base.scope_modal.lock"));

    fireEvent.click(screen.getByTestId("kb-scope-modal-space"));
    fireEvent.click(screen.getByRole("button", { name: catalogText(c.locale, "knowledge_base.scope_modal.next") }));
    const back = screen.getByRole("button", { name: new RegExp(catalogText(c.locale, "knowledge_base.scope_modal.back")) });
    check(c, back, "←", "start", catalogText(c.locale, "knowledge_base.scope_modal.back"));
  });

  it.each([
    [1, "✓", "kbMatrix.cv_agree_ok"],
    [0.5, "⚠", "kbMatrix.cv_agree_bad"],
  ] as const)("matrix side panel: glyph after the cross-check line (agreement %s)", (agree, glyph, key) => {
    const { container } = renderIntl(
      <SidePanel
        row={{ fileId: "kbf_1", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "rec_1", approved: false, cells: {} }}
        col={{ columnId: "c1", name: "Grade", type: "string", options: [] }}
        cell={{ status: "done", value: "B", variants: [], edited: false, verified: false, selfConsistencyAgree: agree } as never}
        canEdit
        saving={false}
        onClose={vi.fn()}
        onSaveValue={vi.fn()}
        onMarkVerified={vi.fn()}
      />,
      { locale: c.locale },
    );
    const message = catalogText(c.locale, key);
    const host = Array.from(container.querySelectorAll("*")).find(
      (el) => Array.from(el.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent === message || n.textContent === `${message} ${glyph}`)),
    ) as HTMLElement;
    expect(host).toBeTruthy();
    check(c, host, glyph, "end", message, false);
  });

  it("formula confirm card: ✓ before the confirmation", async () => {
    mocks.resolveClarification.mockResolvedValue({ ok: true });
    const { container } = renderIntl(
      <FormulaConfirmCard workspaceId="ws_1" requestId="clar_formula" token="EBITDA" proposedFormula="EBITDA = revenue - cost" />,
      { locale: c.locale },
    );
    const confirm = container.querySelector("button.primary, button[type='submit']") ?? screen.getAllByRole("button")[0];
    fireEvent.click(confirm);
    const ack = await waitFor(() => {
      const el = container.querySelector(".ack");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    check(c, ack, "✓", "start", catalogText(c.locale, "clarification.formula.ack_confirmed"));
  });

  it("canvas clarification card: ✓ before the skip acknowledgement", async () => {
    mocks.resolveClarification.mockResolvedValue({ ok: true });
    const { container } = renderIntl(
      <CanvasClarificationCard workspaceId="ws_1" requestId="clar_token" token="GMV" kind="token" candidates={[{ value: "gmv", label: "GMV" }]} />,
      { locale: c.locale },
    );
    fireEvent.click(screen.getByRole("button", { name: catalogText(c.locale, "clarification.btn.skip") }));
    const ack = await waitFor(() => {
      const el = container.querySelector(".ack");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    check(c, ack, "✓", "start", catalogText(c.locale, "clarification.ack.skipped"));
  });

  it("canvas clarification card: 💡 tip, ✓ confirmation and ✕ cancelled directive for a task plan", async () => {
    mocks.resolveClarification.mockResolvedValue({ ok: true });
    mocks.cancelClarificationDirective.mockResolvedValue({ cancelled: true });
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    });
    const { container } = renderIntl(
      <CanvasClarificationCard
        workspaceId="ws_1"
        requestId="clar_plan"
        token="__task_plan__"
        kind="task_plan"
        candidates={[{ value: "a", label: "Step A" }, { value: "b", label: "Step B" }]}
      />,
      { locale: c.locale },
    );
    check(c, container.querySelector(".directive-tooltip") as HTMLElement, "💡", "start", catalogText(c.locale, "clarification.edit.directive_first_tip"));

    fireEvent.click(container.querySelector(".actions .primary") as HTMLButtonElement);
    const ack = await waitFor(() => {
      const el = container.querySelector(".ack");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    const confirmed = catalogText(c.locale, "clarification.ack.confirmed_many", { count: 2 });
    const cancelLabel = catalogText(c.locale, "clarification.edit.directive_cancel");
    check(c, ack, "✓", "start", `${confirmed}${cancelLabel}`);

    fireEvent.click(container.querySelector(".ack-cancel-directive") as HTMLButtonElement);
    const cancelled = await waitFor(() => {
      const el = container.querySelector(".ack-cancelled");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    check(c, cancelled, "✕", "start", catalogText(c.locale, "clarification.edit.directive_cancelled"));
  });

  it("extraction table wizard: → after the draft button and ← before the back button", async () => {
    mocks.draftKnowledgeTableFields.mockResolvedValue({ fields: [{ name: "f", type: "string", description: "", required: false, cross_verify: false, extraction_hint: "" }] });
    const { container } = renderIntl(<DraftWizard mode="create" onClose={vi.fn()} onSaved={vi.fn()} />, { locale: c.locale });
    const draft = screen.getByTestId("kt-draft-btn");
    check(c, draft, "→", "end", catalogText(c.locale, "kt.wizard.draft_btn"));

    fireEvent.change(container.querySelector("textarea") as HTMLTextAreaElement, { target: { value: "contracts" } });
    fireEvent.click(draft);
    const back = await screen.findByRole("button", { name: new RegExp(catalogText(c.locale, "kt.wizard.back_describe")) });
    check(c, back, "←", "start", catalogText(c.locale, "kt.wizard.back_describe"));
  });
});
