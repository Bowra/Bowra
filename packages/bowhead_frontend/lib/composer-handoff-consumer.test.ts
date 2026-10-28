/**
 * file-management-v2 Use in New Chat—guard **consumer placement** for the one-shot channel.
 *
 * 0.87.0 production incident: `useEffect` consuming `bowhead:composer-attach` lived in
 * `chat-workbench.tsx`, mounted only when `runner.started || showingReportOnly`.
 * Navigation from Files lands on empty home (HOME_V9 HomeWorkSurface),
 * so ChatWorkbench never mounts → key never consumed. Two consequences:
 *   1. Silent failure (users cannot bring files, with no error)
 *   2. Residual key is picked up by **some later actual chat mount**, attaching files
 *      to a session the user did not intend—worse than ineffectiveness
 *
 * Why old tests passed: unit tests mounted ChatWorkbench directly (consumer always mounted),
 * e2e only asserted key **written**. No coverage of the seam between writer and mounting consumer.
 *
 * Enforce the conclusion itself: **consumer must live in an unconditionally mounted component**.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
const KEY = "bowhead:composer-attach";
const read = (rel: string) => readFileSync(join(ROOT, rel), "utf8");

describe("composer handoff 一次性通道", () => {
  it("消费点在页面（无条件挂载），不在条件挂载的 ChatWorkbench 里", () => {
    const page = read("app/(app)/page.tsx");
    const workbench = read("components/chat-workbench.tsx");

    expect(page).toContain(`sessionStorage.getItem("${KEY}")`);
    expect(page).toContain(`sessionStorage.removeItem("${KEY}")`);

    // ChatWorkbench renders only when runner.started || showingReportOnly;
    // moving the read back there reproduces the 0.87.0 gap.
    expect(workbench).not.toContain(`sessionStorage.getItem("${KEY}")`);
  });

  it("读走即删——不能只读不删（残留 key 会被后续会话误捡）", () => {
    const page = read("app/(app)/page.tsx");
    const get = page.indexOf(`sessionStorage.getItem("${KEY}")`);
    const remove = page.indexOf(`sessionStorage.removeItem("${KEY}")`);
    expect(get).toBeGreaterThan(-1);
    expect(remove).toBeGreaterThan(get);
    // Remove before parsing—bad payloads must not leave keys triggering repeatedly.
    expect(page.indexOf("JSON.parse", get)).toBeGreaterThan(remove);
  });

  it("uses the ordinary-draft attachment schema instead of trusting storage", () => {
    const page = read("app/(app)/page.tsx");
    expect(page).toContain("canonicalizeComposerAttachmentRefs(JSON.parse(raw))");
    expect(page).not.toContain("parsed.every((a) => a && a.fileId)");
  });

  it("预挂附件既传给 workbench，也在首页直接发送时随第一条消息带走", () => {
    const page = read("app/(app)/page.tsx");
    // Wire both destinations: existing session → workbench; empty home → home V2Composer.
    // Wiring only the former leaves Files-to-home users without any chip (incomplete 0.87.1 state).
    const wired = page.match(
      /prefillAttachments=\{\s*recoveredChatAction\?\.attachments\s*\?\?\s*recoveredSubmission\?\.attachments\s*\?\?\s*handoffAttach\s*\}/g,
    ) ?? [];
    expect(wired.length).toBe(2);
    // Empty home → home prompt supplies no attachments; handleSend must add them,
    // or first user message omits the file, making the feature ineffective.
    expect(page).toMatch(/attachments === undefined && handoffAttach/);
  });
});
