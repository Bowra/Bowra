/**
 * Licensing FE slice A (§13.2) — <EntitlementBanner/> renders the right severity variant per
 * entitlement state (driven by the entitlement-store), and is silent when active.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { EntitlementBanner } from "./entitlement-banner";
import {
  _resetEntitlement,
  applyEntitlementDenial,
  applyEntitlementHeaders,
} from "@/lib/entitlement-store";

function resp(headers: Record<string, string>): { headers: Headers } {
  return { headers: new Headers(headers) };
}

beforeEach(() => _resetEntitlement());
afterEach(() => {
  cleanup();
  _resetEntitlement();
});

describe("EntitlementBanner", () => {
  it("active → renders nothing (healthy is silent)", () => {
    const { container } = renderIntl(<EntitlementBanner />);
    expect(container.querySelector("[data-testid='ent-banner']")).toBeNull();
  });

  it("grace → amber status banner with mono deadline + dismissible ✕", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "grace", "X-Entitlement-Grace-Until": "2026-06-30" })));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn).not.toBeNull();
    expect(bn.getAttribute("data-state")).toBe("grace");
    expect(bn.className).toContain("ent-banner--status");
    expect(bn.getAttribute("role")).toBe("status");
    expect(container.querySelector(".date")?.textContent).toBe("2026-06-30");
    const x = container.querySelector(".ent-banner__x") as HTMLButtonElement;
    expect(x).not.toBeNull();
    act(() => x.click());
    expect(container.querySelector("[data-testid='ent-banner']")).toBeNull(); // dismissed
  });

  it("over_grace → red alert, read-only badge, renewal CTA, NOT dismissible", () => {
    act(() => applyEntitlementDenial("over_grace"));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn.getAttribute("data-state")).toBe("over_grace");
    expect(bn.className).toContain("ent-banner--error");
    expect(bn.getAttribute("role")).toBe("alert");
    expect(container.querySelector(".ent-banner__ro")).not.toBeNull();
    expect(container.querySelector(".ent-banner__cta")?.textContent).toContain("立即续期");
    expect(container.querySelector(".ent-banner__x")).toBeNull(); // persistent
  });

  it("suspended → red alert, admin-directed, no self-serve CTA", () => {
    act(() => applyEntitlementDenial("suspended"));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn.getAttribute("data-state")).toBe("suspended");
    expect(bn.className).toContain("ent-banner--error");
    expect(container.querySelector(".ent-banner__link")?.textContent).toContain("查看暂停原因");
    expect(container.querySelector(".ent-banner__cta")).toBeNull();
  });

  it("needs_license → neutral terracotta with upload CTA", () => {
    act(() => applyEntitlementDenial("needs_license"));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn.getAttribute("data-state")).toBe("needs_license");
    expect(bn.className).toContain("ent-banner--neutral");
    expect(container.querySelector(".ent-banner__cta")?.textContent).toContain("上传许可证");
  });

  it("tamper → critical with error code", () => {
    act(() => applyEntitlementDenial("tamper"));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn.getAttribute("data-state")).toBe("tamper");
    expect(bn.className).toContain("ent-banner--crit");
    expect(container.querySelector(".ent-banner__code")?.textContent).toContain("E-LIC-403");
  });

  it("grace + degraded → amber banner carries the degraded ride-along tag", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "grace;degraded", "X-Entitlement-Grace-Until": "2026-07-01" })));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn.getAttribute("data-state")).toBe("grace");
    expect(bn.className).toContain("ent-banner--status");
    expect(container.querySelector(".ent-banner__degraded")).not.toBeNull();
    expect(container.querySelector(".ent-banner__x")).not.toBeNull(); // still dismissible
  });

  it("active + degraded → quiet muted standalone strip", () => {
    act(() => applyEntitlementHeaders(resp({ "X-Entitlement-Health": "active;degraded" })));
    const { container } = renderIntl(<EntitlementBanner />);
    const bn = container.querySelector("[data-testid='ent-banner']") as HTMLElement;
    expect(bn.getAttribute("data-state")).toBe("degraded");
    expect(bn.className).toContain("ent-banner--muted");
  });
});
