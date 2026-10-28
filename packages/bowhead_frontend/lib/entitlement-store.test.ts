/**
 * Licensing FE slice A (§13.2 / §3.7) — entitlement-store: header/deny → banner state.
 * Covers the shadow-mode safety invariant: a deny-state arriving on a 200 header is IGNORED
 * (treated as active → no banner), so ENFORCE=0 stays invisible.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  _resetEntitlement,
  applyEntitlementDenial,
  applyEntitlementHeaders,
  dismissEntitlementBanner,
  parseHealthHeader,
} from "./entitlement-store";

// useEntitlement() returns a derived view from module state; we assert the same projection
// the hook computes by re-applying then reading a fresh view via a tiny re-import-free probe.
import { useEntitlement } from "./entitlement-store";
import { renderHook, act } from "@testing-library/react";

function view() {
  // initial-sync render reads current module state (no act needed for a fresh mount).
  return renderHook(() => useEntitlement()).result.current;
}

function resp(headers: Record<string, string>): { headers: Headers } {
  return { headers: new Headers(headers) };
}

beforeEach(() => _resetEntitlement());
afterEach(() => _resetEntitlement());

describe("parseHealthHeader", () => {
  it("parses plain + degraded-suffixed values", () => {
    expect(parseHealthHeader("grace")).toEqual({ health: "grace", degraded: false });
    expect(parseHealthHeader("active;degraded")).toEqual({ health: "active", degraded: true });
    expect(parseHealthHeader("grace; degraded")).toEqual({ health: "grace", degraded: true });
    expect(parseHealthHeader(null)).toBeNull();
    expect(parseHealthHeader("")).toBeNull();
  });
});

describe("applyEntitlementHeaders — SUCCESS (200) path", () => {
  it("grace + grace-until → amber, dismissible, visible", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "grace", "X-Entitlement-Grace-Until": "2026-06-30" })));
    const v = view();
    expect(v.health).toBe("grace");
    expect(v.graceUntil).toBe("2026-06-30");
    expect(v.visible).toBe(true);
    expect(v.dismissible).toBe(true);
    expect(v.readOnly).toBe(false);
  });

  it("active → silent (no banner)", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "active" })));
    expect(view().visible).toBe(false);
  });

  it("active;degraded → quiet standalone strip (visible, dismissible, not red)", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "active;degraded" })));
    const v = view();
    expect(v.health).toBe("active");
    expect(v.degraded).toBe(true);
    expect(v.visible).toBe(true);
    expect(v.dismissible).toBe(true);
  });

  it("grace;degraded → grace banner carries the degraded tag", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "grace;degraded", "X-Entitlement-Grace-Until": "2026-07-01" })));
    const v = view();
    expect(v.health).toBe("grace");
    expect(v.degraded).toBe(true);
    expect(v.visible).toBe(true);
  });

  it("SHADOW LEAK: a deny-state on a 200 header is IGNORED → active, no banner", () => {
    // ENFORCE=0: assert_can_run_turn returns the would-deny snapshot; if the header carried it,
    // the FE must NOT show a read-only banner (shadow stays invisible).
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "over_grace" })));
    const v = view();
    expect(v.health).toBe("active");
    expect(v.visible).toBe(false);
    expect(v.readOnly).toBe(false);
  });

  it("absent header on a 200 → active (clear)", () => {
    act(() => applyEntitlementDenial("over_grace")); // set a deny first
    act(() => applyEntitlementHeaders(resp({}))); // a later successful turn clears it
    expect(view().health).toBe("active");
    expect(view().visible).toBe(false);
  });

  it("grace → active (user renewed): clears the deadline + hides the banner", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "grace", "X-Entitlement-Grace-Until": "2026-06-30" })));
    expect(view().visible).toBe(true);
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "active" })));
    const v = view();
    expect(v.health).toBe("active");
    expect(v.graceUntil).toBeNull();
    expect(v.visible).toBe(false);
  });
});

describe("applyEntitlementDenial — 402/403 path", () => {
  it("over_grace → read-only, persistent (not dismissible)", () => {
    act(() => applyEntitlementDenial("over_grace"));
    const v = view();
    expect(v.health).toBe("over_grace");
    expect(v.readOnly).toBe(true);
    expect(v.visible).toBe(true);
    expect(v.dismissible).toBe(false);
  });

  it("suspended → read-only", () => {
    act(() => applyEntitlementDenial("suspended"));
    expect(view().readOnly).toBe(true);
  });

  it("needs_license / tamper → visible but not read-only-composer", () => {
    act(() => applyEntitlementDenial("needs_license"));
    expect(view().health).toBe("needs_license");
    expect(view().readOnly).toBe(false);
    act(() => applyEntitlementDenial("tamper"));
    expect(view().health).toBe("tamper");
  });

  it("non-banner reasons (seat_exceeded / null / undefined) leave the banner unchanged", () => {
    act(() => applyEntitlementDenial("seat_exceeded"));
    expect(view().visible).toBe(false);
    act(() => applyEntitlementDenial(null));
    expect(view().visible).toBe(false);
    act(() => applyEntitlementDenial(undefined));
    expect(view().visible).toBe(false);
  });
});

describe("dismiss", () => {
  it("grace dismiss hides this session; a state change re-shows", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "grace", "X-Entitlement-Grace-Until": "2026-06-30" })));
    expect(view().visible).toBe(true);
    act(() => dismissEntitlementBanner());
    expect(view().visible).toBe(false);
    // escalation to over_grace must re-surface (different banner key clears the dismissal)
    act(() => applyEntitlementDenial("over_grace"));
    expect(view().visible).toBe(true);
  });
});
