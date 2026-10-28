/**
 * Chat workbench and report canvas · batch 1: compare every branch against the original logic after replacing state-dependent inline styles with modifier classes.
 * styles/workbench-legacy.test.ts locks class declaration values; this file only locks which state attaches which class.
 */
import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { Citation } from "@/lib/types";

import { FailureAckCard } from "./FailureAckCard";
import { CitationsProvider, LeadNarrative } from "./LeadNarrative";
import { TurnRunningProvider } from "./turn-running";

afterEach(cleanup);

describe("FailureAckCard activity-dependent spacing", () => {
  it.each([
    ["transient", false, "bh-step-failure", "bh-step-failure__msg bh-step-failure__msg--hint", true],
    ["transient", true, "bh-step-failure bh-step-failure--inset", "bh-step-failure__msg bh-step-failure__msg--hint", true],
    ["permanent", false, "bh-step-failure", "bh-step-failure__msg", false],
    ["permanent", true, "bh-step-failure bh-step-failure--inset", "bh-step-failure__msg", false],
  ] as const)("kind=%s inset=%s", (kind, inset, outer, msg, hint) => {
    const { container } = renderIntl(
      <TurnRunningProvider running>
        <FailureAckCard kind={kind} message="上游超时" inset={inset} />
      </TurnRunningProvider>,
    );
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toBe(outer);
    expect(root.getAttribute("style")).toBeNull();
    const message = root.firstElementChild as HTMLElement;
    expect(message.className).toBe(msg);
    expect(message.textContent).toBe("上游超时");
    const hintEl = root.querySelector(".bh-step-failure__hint");
    expect(Boolean(hintEl)).toBe(hint);
  });
});

function chipFor(citation: Citation) {
  const { container } = renderIntl(
    <CitationsProvider citations={[citation]}>
      <LeadNarrative text="见 [cite:1]" />
    </CitationsProvider>,
  );
  return container.querySelector(".bh-artifact-chip") as HTMLElement;
}

describe("引用角标：原 opacity: deniedState ? 0.55 : undefined", () => {
  it.each([
    [undefined, false],
    ["linked_accessible", false],
    ["linked_labeled", false],
    ["linked_restricted", true],
    ["linked_missing", true],
    ["unsupported", true],
  ] as const)("accessState=%s → 淡化=%s", (accessState, denied) => {
    const chip = chipFor({ seq: 1, artifactId: "sec_a", label: "图 A", kind: "chart_section", accessState });
    expect(chip.classList.contains("legacy-cite-chip")).toBe(true);
    expect(chip.classList.contains("is-denied")).toBe(denied);
    expect(chip.getAttribute("style")).toBeNull();
  });

  it("知识库引用的文档小图标用角标档尺寸", () => {
    const chip = chipFor({ seq: 1, artifactId: "art_kb", label: "制度", kind: "kb_chunk", accessState: "linked_missing" });
    const icon = chip.querySelector(".bh-kb-doc-icon") as HTMLElement;
    expect(icon.className).toBe("bh-kb-doc-icon legacy-kb-doc-icon legacy-kb-doc-icon--chip");
    expect(icon.getAttribute("style")).toBeNull();
  });
});
