"use client";

/**
 * Planning Mode composer pref — the Auto / Planning Mode selector value, mirroring how
 * the model-picker pref is set in the composer and read by postSendMessage in
 * use-turn-runner.ts. Kept in localStorage + a window event so the selector and
 * the send path agree without threading a new onSend param through page→runner.
 *
 * `/auth/me.capabilities.planning` gates visibility and submission at their
 * React call sites. This module owns only the user's Auto / Planning choice.
 */

import { useEffect, useState } from "react";

const KEY = "bowhead.planning_mode";
const EVENT = "bowhead:planning-mode-changed";

/**
 * Three states aligned value-for-value with backend `planning_trigger.should_propose_plan(mode=...)`:
 * `auto` = plan card only on keyword match (default) / `planning` = force one / `off` = never show one.
 * ⚠ Frontend exposed `off` only from 2026-08-12; previously it had two states,
 * so turning off stored `auto` and keywords still auto-produced cards (reported as a bug on 116).
 */
export type PlanningModePref = "auto" | "planning" | "off";

const VALUES: readonly PlanningModePref[] = ["auto", "planning", "off"];

function isPref(raw: string | null): raw is PlanningModePref {
  return raw !== null && (VALUES as readonly string[]).includes(raw);
}

export function getPlanningModePref(): PlanningModePref {
  if (typeof window === "undefined") return "auto";
  try {
    if (typeof window.localStorage?.getItem !== "function") return "auto";
    const raw = window.localStorage.getItem(KEY);
    // Unknown/historical values always fall back to auto; previously only "auto"/"planning" were stored,
    // but any dirty value must resolve to a valid state, rather than forwarding dirty strings onto the wire.
    return isPref(raw) ? raw : "auto";
  } catch {
    return "auto";
  }
}

export function setPlanningModePref(mode: PlanningModePref): void {
  if (typeof window === "undefined") return;
  try {
    if (typeof window.localStorage?.setItem === "function") {
      window.localStorage.setItem(KEY, mode);
    }
    window.dispatchEvent(new CustomEvent(EVENT));
  } catch {
    /* SSR / restricted ctx */
  }
}

/** React hook for the composer selector — reactive to cross-component changes. */
export function usePlanningModePref(): [PlanningModePref, (m: PlanningModePref) => void] {
  const [mode, setMode] = useState<PlanningModePref>("auto");
  useEffect(() => {
    setMode(getPlanningModePref());
    const onChange = () => setMode(getPlanningModePref());
    window.addEventListener(EVENT, onChange);
    return () => window.removeEventListener(EVENT, onChange);
  }, []);
  return [mode, setPlanningModePref];
}
