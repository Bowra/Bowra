"use client";

/**
 * One-shot sessionStorage keys for cross-page composer handoff.
 *
 * ⚠ Recurring trap in this key family (feedback-triage-2026-08-24 was the second occurrence): **consume on an
 * unconditionally rendered page**. `ChatWorkbench` mounts only with `runner.started || showingReportOnly`,
 * while these handoffs usually open the empty-state home page; consuming in workbench leaves the key unread forever.
 * Worse, a leftover key gets picked up when an actual conversation later mounts, inserting content into a session where the user
 * did not intend it.
 *
 * This has happened twice:
 *   · file-management-v2 P2 Use in New Chat attachment prestaging (caught by 0.87.0 prod dogfood);
 *   · skill-drawer Edit in Chat prefill (customer: “切到已有对话，输入框出现帮我修改技能 XXX”).
 *
 * Keep key names/readers here and lock consumer location with `composer-handoff.test.ts`'s drift guard.
 */

import { useCallback, useEffect, useState } from "react";

import { appPath } from "@/lib/base-path";
import { COACH_AGENT_ID } from "@/lib/coach-alignment-continue";
import type { AttachmentRef } from "@/lib/types";

export const COMPOSER_PREFILL_KEY = "bowhead:composer-prefill";
export const COMPOSER_ATTACH_KEY = "bowhead:composer-attach";
export const COMPOSER_AGENT_USE_KEY = "bowhead:composer-agent-use";

/** `text` is an optional request staged after the Agent reference; nothing is sent until the user sends it. */
export type ComposerAgentUse = { agentId: string; label: string; text?: string };

/** Longest staged request accepted from the handoff key. */
const MAX_AGENT_USE_TEXT = 8000;

/**
 * Whether the home composer may bind a pending Agent handoff as an @ reference.
 *
 * Every handoff needs structured reference input. Only "Start use" of an Agent is also behind its own
 * `agent_start_use` switch: the Coach handoffs (Ask Coach to change or fix this) are Coach entries that
 * bind @Coach exactly like the alignment card's continue message, so that switch must not strand them
 * as plain text with the "cannot select this Agent" notice.
 */
export function composerAgentHandoffAdmitted(
  agentId: string,
  composer: { reference_input?: boolean; agent_start_use?: boolean } | null | undefined,
): boolean {
  if (composer?.reference_input !== true) return false;
  return agentId === COACH_AGENT_ID || composer.agent_start_use === true;
}

/** Open a fresh chat with a pending Agent identity (a Team or system Agent), resolved to an exact
 * authorized reference by the destination page before it reaches Composer. */
export function handoffAgentToNewChat(agent: ComposerAgentUse): void {
  try {
    sessionStorage.setItem(COMPOSER_AGENT_USE_KEY, JSON.stringify(agent));
  } catch {
    /* The destination will still open; it cannot bind the Agent without storage. */
  }
  window.location.assign(appPath("/"));
}

/**
 * The pending Agent handoff, **without consuming it**: the key stays until the destination has staged it
 * (`clearComposerAgentUse`). Reading it destructively on mount lost the handoff whenever the page mounted
 * twice before staging (React Strict Mode's double effects in development, or a remount while sign-in
 * resolves), so "Ask Coach to change this" landed on an empty composer. An invalid value is removed.
 */
export function peekComposerAgentUse(): ComposerAgentUse | null {
  try {
    const raw = sessionStorage.getItem(COMPOSER_AGENT_USE_KEY);
    if (raw === null) return null;
    const agent = parseComposerAgentUse(raw);
    if (agent === null) sessionStorage.removeItem(COMPOSER_AGENT_USE_KEY);
    return agent;
  } catch {
    return null;
  }
}

export function clearComposerAgentUse(): void {
  try {
    sessionStorage.removeItem(COMPOSER_AGENT_USE_KEY);
  } catch {
    /* Storage unavailable: nothing was stored. */
  }
}

export function takeComposerAgentUse(): ComposerAgentUse | null {
  const agent = peekComposerAgentUse();
  clearComposerAgentUse();
  return agent;
}

/**
 * The page-level consumer of the Agent handoff: `pending` is read once per mount (repeatable, so a second
 * mount sees the same request) and `settle()` consumes it once the reference or its fallback text is staged.
 */
export function useComposerAgentUseHandoff(): { pending: ComposerAgentUse | null; settle: () => void } {
  const [pending, setPending] = useState<ComposerAgentUse | null>(null);
  useEffect(() => {
    const agent = peekComposerAgentUse();
    if (agent) setPending(agent);
  }, []);
  const settle = useCallback(() => {
    clearComposerAgentUse();
    setPending(null);
  }, []);
  return { pending, settle };
}

function parseComposerAgentUse(raw: string): ComposerAgentUse | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const agent = value as Record<string, unknown>;
    if (typeof agent.agentId !== "string" || !/^[A-Za-z0-9_.:-]+$/.test(agent.agentId)) return null;
    if (typeof agent.label !== "string" || !agent.label.trim() || agent.label.length > 160) return null;
    if (agent.text !== undefined && (typeof agent.text !== "string" || agent.text.length > MAX_AGENT_USE_TEXT)) return null;
    const text = typeof agent.text === "string" && agent.text.trim() ? agent.text : undefined;
    return text === undefined ? { agentId: agent.agentId, label: agent.label } : { agentId: agent.agentId, label: agent.label, text };
  } catch {
    return null;
  }
}

/**
 * Use in New Chat: write a one-shot attachment key, then navigate home for that page to consume it and prestage composer attachments.
 *
 * Behavior matches the same-named existing action in Library › Files drawer exactly (app/(app)/files/page.tsx
 * useInNewChat); knowledge-base drawer's Use in Chat goes through this helper rather than adding a second file-to-chat path.
 * If storage is unavailable, still navigate, simply without prestaging.
 */
export function handoffAttachmentsToNewChat(refs: AttachmentRef[]): void {
  try {
    sessionStorage.setItem(COMPOSER_ATTACH_KEY, JSON.stringify(refs));
  } catch {
    /* Storage unavailable → still navigate, without prestaging. */
  }
  window.location.assign(appPath("/"));
}

/**
 * Failure card "Continue in a new session" (chat turn feedback D5, e.g. `llm_context_overflow`):
 * open a fresh chat with the failed question, and its attachments, staged in the composer for review.
 * Uses the same one-shot keys as the other handoffs; the home page consumes them.
 */
export function handoffQuestionToNewChat(text: string, attachments: AttachmentRef[] = []): void {
  try {
    if (text.trim()) sessionStorage.setItem(COMPOSER_PREFILL_KEY, text);
    if (attachments.length > 0) sessionStorage.setItem(COMPOSER_ATTACH_KEY, JSON.stringify(attachments));
  } catch {
    /* Storage unavailable → still open the new session, without staging. */
  }
  window.location.assign(appPath("/"));
}

/**
 * Consume and delete one-shot prefill text.
 *
 * Delete immediately on read (even if the caller never uses it); leaving it contaminates the next session, exactly the original bug.
 * Treat blank text as absent. Unavailable storage (privacy mode, etc.) silently returns null.
 */
export function takeComposerPrefill(): string | null {
  try {
    const pending = sessionStorage.getItem(COMPOSER_PREFILL_KEY);
    if (pending === null) return null;
    sessionStorage.removeItem(COMPOSER_PREFILL_KEY);
    return pending.trim() ? pending : null;
  } catch {
    return null;
  }
}
