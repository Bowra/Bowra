"use client";

/**
 * Licensing FE slice A (§13.2 / §3.7) — entitlement health store.
 *
 * Module-level subscription store (mirrors lib/use-toast.ts; Zustand not in tree).
 * The `X-Entitlement-Health` / `X-Entitlement-Grace-Until` headers ride the **200**
 * turn response (chat/send §3.3①, resume_turn §3.3②); a 402/403 deny carries the
 * nested-dict `{error:{code}}`. There is NO shared fetch wrapper, so BOTH fetch
 * sites publish here. The <EntitlementBanner/> mounted in the workspace shell + the
 * composer read-only treatment subscribe via useEntitlement().
 *
 * Shadow-mode safety (§1.1 — the 101 no-op guarantee): in shadow (ENFORCE=0) a
 * would-be-denied turn still RUNS (200), but `assert_can_run_turn` returns the
 * would-deny snapshot and the gate stamps that health onto the 200 header. So the
 * SUCCESS path honors ONLY `active`/`grace` (+`degraded`) — any deny-state arriving
 * on a 200 is a shadow leak and is treated as `active` (no banner). Deny states
 * reach the banner EXCLUSIVELY via the 402/403 `detail.error.code` path. This keeps
 * shadow invisible regardless of the backend (which also guards _stamp_health).
 */

import { useEffect, useState } from "react";

export type EntitlementHealth =
  | "active"
  | "grace"
  | "over_grace"
  | "suspended"
  | "needs_license"
  | "tamper";

/** Deny-states surfaced by the 402/403 gate (entitlements.py `_DENY_BY_HEALTH`). */
const DENY_HEALTHS = new Set<string>(["over_grace", "suspended", "needs_license", "tamper"]);
/** Allow-states a SUCCESS-path header may carry (anything else on a 200 = shadow leak). */
const ALLOW_HEALTHS = new Set<string>(["active", "grace"]);

export interface EntitlementState {
  health: EntitlementHealth;
  /** ISO date from `X-Entitlement-Grace-Until` (grace only). */
  graceUntil: string | null;
  /** Fail-open to last-known-good (header `;degraded` suffix, §6.4). */
  degraded: boolean;
}

const INITIAL: EntitlementState = { health: "active", graceUntil: null, degraded: false };

let _state: EntitlementState = { ...INITIAL };
// Session-scoped dismissal: the banner is hidden while the CURRENT bannerKey matches
// `_dismissedKey`. A state change re-shows it; a full page reload (a new session) resets
// the module, so a dismissed grace banner "re-appears next session" (design intent).
let _dismissedKey: string | null = null;
const _listeners: Set<() => void> = new Set();

function _notify(): void {
  for (const l of _listeners) {
    try {
      l();
    } catch {
      /* noop */
    }
  }
}

/** Identifies a distinct banner instance — a state change re-shows after a dismiss. */
function _bannerKey(s: EntitlementState): string {
  return `${s.health}:${s.graceUntil ?? ""}:${s.degraded ? "d" : ""}`;
}

function _set(next: EntitlementState): void {
  const changed = _bannerKey(next) !== _bannerKey(_state);
  _state = next;
  if (changed) _dismissedKey = null; // a new/changed banner clears any prior dismissal
  _notify();
}

/** Parse the `X-Entitlement-Health` value: `"grace"` | `"active;degraded"` | … */
export function parseHealthHeader(raw: string | null): { health: string; degraded: boolean } | null {
  if (!raw) return null;
  const parts = raw.split(";").map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return null;
  return { health: parts[0], degraded: parts.includes("degraded") };
}

/**
 * SUCCESS path (200 from a gated turn endpoint). Honors ONLY `active`/`grace`
 * (+`degraded`); a deny-state here is a shadow-mode leak and is treated as `active`
 * (clear). A 200 with no header also means allowed → clear to `active`.
 */
export function applyEntitlementHeaders(resp: { headers: Headers }): void {
  const parsed = parseHealthHeader(resp.headers.get("X-Entitlement-Health"));
  const graceUntil = resp.headers.get("X-Entitlement-Grace-Until");
  const health: EntitlementHealth =
    parsed && ALLOW_HEALTHS.has(parsed.health)
      ? (parsed.health as EntitlementHealth)
      : "active"; // absent header OR shadow deny-leak → allowed
  _set({
    health,
    graceUntil: health === "grace" ? graceUntil : null,
    degraded: parsed?.degraded ?? false,
  });
}

/**
 * DENY path (402/403). `code` = `detail.error.code`. Maps the four banner deny-states;
 * non-banner reasons (`seat_exceeded`/`feature_unlicensed`/`clock_tamper`/
 * `entitlement_unavailable`) are left to the existing 8a error toast — the banner
 * state is unchanged.
 */
export function applyEntitlementDenial(code: string | null | undefined): void {
  if (code && DENY_HEALTHS.has(code)) {
    _set({ health: code as EntitlementHealth, graceUntil: null, degraded: false });
  }
}

/** Dismiss the current (dismissible) banner for this session. */
export function dismissEntitlementBanner(): void {
  _dismissedKey = _bannerKey(_state);
  _notify();
}

/** Test helper — reset to the initial active/clear state. */
export function _resetEntitlement(): void {
  _state = { ...INITIAL };
  _dismissedKey = null;
  _notify();
}

export interface EntitlementView extends EntitlementState {
  /** Composer read-only treatment (design: over_grace / suspended). */
  readOnly: boolean;
  /** The banner should render (not active-and-healthy, and not dismissed). */
  visible: boolean;
  /** Dismissible severity (grace, or a degraded-standalone strip on an otherwise active team). */
  dismissible: boolean;
  dismiss: () => void;
}

function _view(): EntitlementView {
  const s = _state;
  const isActive = s.health === "active";
  const dismissible = s.health === "grace" || (isActive && s.degraded);
  const dismissed = _dismissedKey !== null && _dismissedKey === _bannerKey(s);
  const hasBanner = !isActive || s.degraded; // active+degraded still shows the quiet strip
  return {
    ...s,
    readOnly: s.health === "over_grace" || s.health === "suspended",
    dismissible,
    visible: hasBanner && !dismissed,
    dismiss: dismissEntitlementBanner,
  };
}

/** Subscribe a component to the entitlement store. */
export function useEntitlement(): EntitlementView {
  const [view, setView] = useState<EntitlementView>(() => _view());
  useEffect(() => {
    const cb = () => setView(_view());
    _listeners.add(cb);
    cb(); // initial sync — state may have been published before mount
    return () => {
      _listeners.delete(cb);
    };
  }, []);
  return view;
}
