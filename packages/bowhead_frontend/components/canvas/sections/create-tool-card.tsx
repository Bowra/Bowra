"use client";

/**
 * CCA P2 (C2) — CreateToolCard.
 *
 * Inline HITL "新建工具 / Create Tool" card: the agent fetches+parses the OpenAPI spec
 * (machine work), the human supplies the human decisions — which ops to expose, the
 * per-op descriptions (LLM-drafted, editable), the source name, and the secret credential
 * (the trust centrepiece, which rides the create POST → server-side Fernet, never chat).
 *
 * Translated from Claude Design v10 (create-tool-card.jsx + icons.jsx) + the r4 model
 * corrections:
 *   (a) "N tools from 1 source" wording (not a singular tool)
 *   (b) per-op LLM-proposed + editable DESCRIPTION (the registered NAME is the fixed identity)
 *   (c) group-by-tag + tool-count warning when the API is large
 *   (d) input triage: a B-type paste (MCP server / repo / setup doc) is politely routed away
 *
 * Wired to lib/team-tool-sources-api.ts (the shipped P1+r4 endpoints). Visual tokens (--c-*)
 * live in app/globals.css.
 */

import { isNetworkFailure } from "@/lib/transport-error";
import { useTranslations } from "next-intl";
import type { LucideIcon } from "lucide-react";
import {
  ArrowRight as LucideArrowRight,
  Ban as LucideBan,
  Brain as LucideBrain,
  Check as LucideCheck,
  ChevronLeft as LucideChevronLeft,
  Clock as LucideClock,
  Eye as LucideEye,
  EyeOff as LucideEyeOff,
  Info as LucideInfo,
  Lock as LucideLock,
  Plug as LucidePlug,
  Plus as LucidePlus,
  RotateCw as LucideRotateCw,
  Search as LucideSearch,
  ShieldCheck as LucideShieldCheck,
  SlidersVertical as LucideSlidersVertical,
  Sparkles as LucideSparkles,
  TriangleAlert as LucideTriangleAlert,
  Wand as LucideWand,
  X as LucideX,
} from "lucide-react";
import { renderRefreshSvg } from "@/components/icon-refresh";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import {
  authorizeSource,
  createSource,
  previewSource,
  proposeDescriptions,
  type OpSummary,
  type PreviewResult,
  type AuthType,
} from "@/lib/team-tool-sources-api";

import { Tab as UiTab, TabList, Tabs } from "@/components/ui/tabs";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import PresetDirectory, { type ConnectorPreset } from "./preset-directory";
import { CharIcon } from "@/components/char-icon";

type TFn = (key: string, values?: Record<string, string | number>) => string;

// ── props ────────────────────────────────────────────────────────────────────

export interface CreateToolCardProps {
  /** scrubbed spec-URL hint from the cca.tool_create_open event (prefills the source step). */
  specUrlHint?: string | null;
  /** The Lead's classification of `specUrlHint` (from the cca.tool_create_open event). Selects the
   *  starting input mode so an MCP endpoint does not open under the OpenAPI field. Null = default. */
  kindHint?: "openapi" | "mcp" | null;
  /**
   * external-capability-onboarding §3.1 — curated connector presets shown as a
   * one-click gallery on top of the source step. Supplied by the parent (backend
   * entitlement-filtered). Empty (default) → the directory + divider are omitted,
   * so the card is byte-identical to today until presets are wired.
   */
  presets?: ConnectorPreset[];
  /** Settings modal only: move focus to the primary control when a step changes. */
  manageStepFocus?: boolean;
  /** close the card (clears the local open state in the mount). */
  onClose: () => void;
  /** called after a successful create — the mount may drop a local non-turn confirmation line. */
  onCreated?: (sourceName: string, opCount: number) => void;
}

type Step = "source" | "configure" | "success";
const LARGE_SPEC_OPS = 12; // mirror the backend group-by-tag / empty-guard threshold
const MAX_DESC = 200;
/** Internal sentinel for ops without a tag; rendered via t("no_tag"). */
const NO_TAG = "__no_tag__";

// ── line icons ──

/** P2 batch 2: Lucide icon per name (see `components/icon-refresh.tsx`). */
const ICONS: Readonly<Record<string, LucideIcon>> = {
  plug: LucidePlug,
  lock: LucideLock,
  eye: LucideEye,
  eyeOff: LucideEyeOff,
  ban: LucideBan,
  arrowRight: LucideArrowRight,
  wand: LucideWand,
  sliders: LucideSlidersVertical,
  check: LucideCheck,
  x: LucideX,
  brain: LucideBrain,
  search: LucideSearch,
  shieldCheck: LucideShieldCheck,
  rotateCw: LucideRotateCw,
  alertTriangle: LucideTriangleAlert,
  chevronLeft: LucideChevronLeft,
  plus: LucidePlus,
  info: LucideInfo,
  clock: LucideClock,
  sparkles: LucideSparkles,
};

function Icon({ name, s = 18, style }: { name: string; s?: number; style?: React.CSSProperties }) {
  const Next = ICONS[name];
  return Next ? renderRefreshSvg(Next, s, s, { "aria-hidden": true, style }) : null;
}

// ── small helpers ────────────────────────────────────────────────────────────

function methodStyle(m: string): { bg: string; bd: string; fg: string } {
  const mm = m.toUpperCase();
  if (mm === "GET") return { bg: "var(--c-pos-soft)", bd: "var(--c-pos-line)", fg: "var(--c-pos-ink)" };
  if (mm === "DELETE") return { bg: "var(--c-err-soft)", bd: "var(--c-err-line)", fg: "var(--c-err-ink)" };
  return { bg: "var(--c-accent-soft)", bd: "var(--c-accent-line)", fg: "var(--c-accent)" }; // write verbs
}

function MethodBadge({ m }: { m: string }) {
  const c = methodStyle(m);
  return (
    <span
      className="bh-mono"
      style={{
        fontSize: "0.625rem", fontWeight: 700, letterSpacing: ".04em", color: c.fg, background: c.bg,
        border: `1px solid ${c.bd}`, borderRadius: 5, padding: "2px 6px", lineHeight: 1.2,
        flex: "0 0 auto", minWidth: 42, textAlign: "center",
      }}
    >
      {m.toUpperCase()}
    </span>
  );
}

function Callout({
  tone, icon, children, action,
}: {
  tone: "warn" | "err" | "info";
  icon: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  const map = {
    warn: { bg: "var(--c-warn-soft)", bd: "var(--c-warn-line)", fg: "var(--c-warn-ink)", ic: "var(--c-warn)" },
    err: { bg: "var(--c-err-soft)", bd: "var(--c-err-line)", fg: "var(--c-err-ink)", ic: "var(--c-err)" },
    info: { bg: "var(--c-surface-2)", bd: "var(--c-line)", fg: "var(--c-ink-3)", ic: "var(--c-ink-4)" },
  }[tone];
  return (
    <div
      style={{
        display: "flex", gap: 10, alignItems: "flex-start", padding: "11px 13px",
        background: map.bg, border: `1px solid ${map.bd}`, borderRadius: "var(--r-2)",
      }}
    >
      <span style={{ flex: "0 0 auto", color: map.ic, marginTop: 1 }}>
        <Icon name={icon} s={17} />
      </span>
      <div style={{ flex: 1, fontSize: "0.78125rem", lineHeight: 1.55, color: map.fg }}>{children}</div>
      {action}
    </div>
  );
}

function Check({ on, label, onToggle }: { on: boolean; label: string; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={on}
      aria-label={label}
      onClick={onToggle}
      style={{
        width: 19, height: 19, flex: "0 0 19px", borderRadius: 6, cursor: "pointer",
        border: `1.5px solid ${on ? "var(--c-accent)" : "var(--c-line)"}`,
        background: on ? "var(--c-accent)" : "var(--c-surface)", color: "var(--c-accent-ink)",
        display: "grid", placeItems: "center", padding: 0,
      }}
    >
      {on ? <Icon name="check" s={12} /> : null}
    </button>
  );
}

function fieldInput(err?: boolean): React.CSSProperties {
  return {
    width: "100%", border: `1.5px solid ${err ? "var(--c-err)" : "var(--c-line)"}`,
    borderRadius: "var(--r-1)", padding: "10px 12px", fontSize: "0.84375rem", color: "var(--c-ink)",
    background: "var(--c-surface)", outline: "none", fontFamily: "var(--f-sans)",
  };
}

// Legacy primary/secondary button appearance (old inline-style functions) lives in styles/ui-controls-legacy.css legacy-ctool-btn--*;
// primary disabled appearance follows disabled (formerly inline parameter, matching disabled at call sites).
const BTN_PRIMARY = "legacy-ctool-btn legacy-ctool-btn--primary";
const BTN_GHOST = "legacy-ctool-btn legacy-ctool-btn--ghost";

/** derive a base_url from the parsed spec's servers[0].url, falling back to the spec-URL origin. */
function deriveBaseUrl(specJson: string, specUrl?: string | null): string {
  try {
    const spec = JSON.parse(specJson) as { servers?: Array<{ url?: string }> };
    const s = spec.servers?.[0]?.url;
    if (typeof s === "string" && /^https?:\/\//.test(s)) return s;
  } catch {
    /* not JSON-parseable here — fall through */
  }
  if (specUrl) {
    try {
      return new URL(specUrl).origin;
    } catch {
      /* ignore */
    }
  }
  return "";
}

// ── operation row (with the per-op editable description — change b) ───────────

function OpRow({
  op, on, onToggle, desc, onDesc, t,
}: {
  op: OpSummary;
  on: boolean;
  onToggle: () => void;
  desc: string;
  onDesc: (v: string) => void;
  t: TFn;
}) {
  return (
    <div
      style={{
        display: "flex", gap: 11, alignItems: "flex-start", padding: "10px 12px",
        borderRadius: "var(--r-2)", background: on ? "var(--c-accent-tint)" : "transparent",
        border: `1px solid ${on ? "var(--c-accent-line)" : "transparent"}`,
      }}
    >
      <div style={{ marginTop: 1 }}>
        <Check on={on} label={`${t("select_ops")}: ${op.operation_id}`} onToggle={onToggle} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
          <MethodBadge m={op.method} />
          <code className="bh-mono" style={{ fontSize: "0.78125rem", color: "var(--c-ink)", fontWeight: 500 }}>
            {op.path}
          </code>
          {op.is_write && (
            <span
              style={{
                fontSize: "0.625rem", fontWeight: 600,
                color: op.approval_executable === true ? "var(--c-accent)" : "var(--c-warn-ink)",
                background: op.approval_executable === true ? "var(--c-accent-tint)" : "var(--c-warn-soft)",
                border: `1px solid ${op.approval_executable === true ? "var(--c-accent-line)" : "var(--c-warn-line)"}`,
                borderRadius: 5, padding: "1px 6px",
                display: "inline-flex", alignItems: "center", gap: 3,
              }}
            >
              <Icon name={op.approval_executable === true ? "lock" : "alertTriangle"} s={10} />
              {t(op.approval_executable === true ? "write_approval_badge" : "write_unavailable_badge")}
            </span>
          )}
        </div>
        {/* change (b): per-op LLM-proposed + editable DESCRIPTION (advertised to the AI). */}
        {on ? (
          <input
            name={`tool_description_${op.operation_id}`}
            autoComplete="off"
            aria-label={`${t("op_desc_placeholder")}: ${op.operation_id}`}
            value={desc}
            maxLength={MAX_DESC}
            onChange={(e) => onDesc(e.target.value)}
            placeholder={op.summary || t("op_desc_placeholder")}
            style={{ ...fieldInput(), marginTop: 6, fontSize: "0.78125rem", padding: "7px 10px" }}
          />
        ) : (
          <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 3 }}>
            {op.summary || `${op.method.toUpperCase()} ${op.path}`}
          </div>
        )}
      </div>
    </div>
  );
}

// ── the trust centrepiece — vault panel + severed-branch-to-AI diagram ────────

function TrustDiagram({ t }: { t: TFn }) {
  const node = (
    label: string,
    sub: string,
    opts: { icon?: React.ReactNode; bg?: string; bd?: string; ic?: string; fg?: string; glow?: boolean } = {},
  ) => (
    <div
      style={{
        display: "flex", flexDirection: "column", alignItems: "center", gap: 3, padding: "8px 11px",
        borderRadius: 10, textAlign: "center", minWidth: 78,
        background: opts.bg ?? "var(--c-surface)", border: `1px solid ${opts.bd ?? "var(--c-line)"}`,
        boxShadow: opts.glow ? "0 0 0 3px color-mix(in srgb, var(--c-info) 12%, transparent)" : "none",
      }}
    >
      {opts.icon ? <span style={{ color: opts.ic ?? "var(--c-info)" }}>{opts.icon}</span> : null}
      <span style={{ fontSize: "0.71875rem", fontWeight: 600, color: opts.fg ?? "var(--c-ink-2)" }}>{label}</span>
      <span className="bh-mono" style={{ fontSize: "0.5625rem", letterSpacing: ".02em", color: "var(--c-ink-4)" }}>{sub}</span>
    </div>
  );
  const arrow = (
    <span style={{ color: "var(--c-info)", flex: "0 0 auto", opacity: 0.8 }}>
      <Icon name="arrowRight" s={16} />
    </span>
  );
  return (
    <div style={{ position: "relative", padding: "14px 12px 16px", background: "var(--c-surface-2)", border: "1px dashed var(--c-info-line)", borderRadius: "var(--r-2)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 7 }}>
        {node(t("trust_you"), t("trust_you_sub"))}
        {arrow}
        {node(t("trust_vault"), "FERNET", {
          icon: <Icon name="lock" s={15} />, bg: "var(--c-info-soft)", bd: "var(--c-info-line)", ic: "var(--c-info-ink)",
          fg: "var(--c-ink)", glow: true,
        })}
        {arrow}
        {node(t("trust_tool_call"), t("trust_tool_call_sub"), { icon: <Icon name="plug" s={15} />, ic: "var(--c-info)" })}
      </div>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", marginTop: 2 }}>
        <div style={{ position: "relative", height: 30, width: 0, borderLeft: "2px dashed var(--c-err-line)" }}>
          <span
            style={{
              position: "absolute", top: "50%", left: "50%", transform: "translate(-50%,-50%)",
              width: 22, height: 22, borderRadius: "50%", background: "var(--c-err-soft)",
              border: "1.5px solid var(--c-err)", color: "var(--c-err)", display: "grid", placeItems: "center",
            }}
          >
            <Icon name="ban" s={13} />
          </span>
        </div>
        <div
          style={{
            display: "flex", alignItems: "center", gap: 7, padding: "6px 11px", borderRadius: 10,
            background: "var(--c-surface)", border: "1px dashed var(--c-err-line)",
          }}
        >
          <span style={{ color: "var(--c-ink-4)" }}>
            <Icon name="brain" s={15} />
          </span>
          <span style={{ fontSize: "0.71875rem", fontWeight: 600, color: "var(--c-ink-3)" }}>{t("trust_ai_model")}</span>
          <span style={{ fontSize: "0.6875rem", color: "var(--c-err)", fontWeight: 600 }}>{t("trust_never_sees")}</span>
        </div>
      </div>
    </div>
  );
}

function VaultPanel({
  value, show, onToggleShow, onChange, method, inputId, t,
}: {
  value: string;
  show: boolean;
  onToggleShow: () => void;
  onChange: (v: string) => void;
  method: AuthType;
  inputId: string;
  t: TFn;
}) {
  const label = method === "api_key" ? "API Key" : method === "bearer" ? "Bearer Token" : t("credential");
  return (
    <div
      style={{
        borderRadius: "var(--r-3)", padding: "15px 16px 16px",
        background: "color-mix(in srgb, var(--c-info-soft) 58%, var(--c-surface))", border: "1px solid var(--c-info-line)",
        boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--c-ink) 8%, transparent)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 12 }}>
        <span
          style={{
            width: 30, height: 30, flex: "0 0 30px", borderRadius: 9, background: "var(--c-info)",
            color: "var(--c-bg)", display: "grid", placeItems: "center", boxShadow: "0 2px 6px var(--c-info-line)",
          }}
        >
          <Icon name="lock" s={16} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: "0.84375rem", fontWeight: 600, color: "var(--c-ink)" }}>{t("vault_title")}</div>
          <div style={{ fontSize: "0.71875rem", color: "var(--c-info-ink)" }}>{t("vault_sub")}</div>
        </div>
        <span
          className="bh-mono"
          style={{
            fontSize: "0.59375rem", fontWeight: 600, letterSpacing: ".04em", color: "var(--c-info-ink)",
            background: "var(--c-info-soft)", border: "1px solid var(--c-info-line)", borderRadius: 999, padding: "3px 8px",
            display: "inline-flex", alignItems: "center", gap: 4,
          }}
        >
          <Icon name="lock" s={11} />{t("encrypted_badge")}
        </span>
      </div>
      <label htmlFor={inputId} style={{ display: "block", fontSize: "0.75rem", fontWeight: 600, color: "var(--c-ink-2)", marginBottom: 6 }}>
        {label}
      </label>
      <div
        className="create-tool-compound-field"
        style={{
          display: "flex", alignItems: "center", gap: 8, background: "var(--c-surface)",
          border: "1.5px solid var(--c-info-line)", borderRadius: "var(--r-1)", padding: "0 6px 0 11px",
          boxShadow: "0 0 0 3px color-mix(in srgb, var(--c-info) 6%, transparent)",
        }}
      >
        <span style={{ color: "var(--c-info)", flex: "0 0 auto" }}>
          <Icon name="lock" s={14} />
        </span>
        <input
          id={inputId}
          name="tool_credential"
          autoComplete="new-password"
          type={show ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={t("secret_placeholder")}
          className="bh-mono"
          style={{
            flex: 1, border: "none", outline: "none", background: "transparent", padding: "10px 0",
            fontSize: "0.8125rem", color: "var(--c-ink)", letterSpacing: show ? 0 : ".12em",
          }}
        />
        <button
          type="button"
          aria-label={show ? t("hide_secret") : t("show_secret")}
          aria-pressed={show}
          onClick={onToggleShow}
          style={{
            width: 30, height: 30, flex: "0 0 30px", border: "none", background: "transparent",
            color: "var(--c-ink-4)", cursor: "pointer", borderRadius: 7, display: "grid", placeItems: "center",
          }}
        >
          <Icon name={show ? "eyeOff" : "eye"} s={16} />
        </button>
      </div>
      <div style={{ marginTop: 14 }}>
        <TrustDiagram t={t} />
      </div>
      <p
        style={{
          display: "flex", gap: 8, alignItems: "flex-start", margin: "13px 0 0", fontSize: "0.71875rem",
          lineHeight: 1.6, color: "var(--c-ink-3)",
        }}
      >
        <span style={{ color: "var(--c-info)", flex: "0 0 auto", marginTop: 2 }}>
          <Icon name="lock" s={13} />
        </span>
        <span>
          {t("trust_note_pre")}
          <b style={{ color: "var(--c-ink-2)" }}>{t("trust_note_encrypted")}</b>
          {t("trust_note_mid")}
          <b style={{ color: "var(--c-ink-2)" }}>{t("trust_note_use")}</b>
          {t("trust_note_post")}
        </span>
      </p>
    </div>
  );
}

// ── stepper ──────────────────────────────────────────────────────────────────

function Stepper({ active, t }: { active: 0 | 1 | 2; t: TFn }) {
  const steps: Array<[string, string]> = [["①", t("step_source")], ["②", t("step_configure")], ["✓", t("step_done")]];
  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 8, padding: "11px 16px",
        borderTop: "1px solid var(--c-line-2)", borderBottom: "1px solid var(--c-line-2)",
        background: "var(--c-bg)",
      }}
    >
      {steps.map(([sym, lbl], i) => {
        const done = i < active;
        const cur = i === active;
        const ring = done || cur ? "var(--c-accent)" : "var(--c-line)";
        const fill = done ? "var(--c-accent)" : cur ? "var(--c-accent-tint)" : "transparent";
        const fg = done ? "var(--c-accent-ink)" : cur ? "var(--c-accent)" : "var(--c-ink-3)";
        return (
          <span key={i} style={{ display: "flex", alignItems: "center", gap: 8, flex: i < 2 ? 1 : "0 0 auto" }}>
            <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <span
                style={{
                  width: 20, height: 20, borderRadius: "50%", border: `1.5px solid ${ring}`,
                  background: fill, color: fg, display: "grid", placeItems: "center", fontSize: "0.6875rem", fontWeight: 700,
                }}
              >
                {done ? <Icon name="check" s={12} /> : <CharIcon ch={sym} />}
              </span>
              <span
                style={{
                  fontSize: "0.75rem", fontWeight: cur ? 600 : 500,
                  color: cur ? "var(--c-ink)" : "var(--c-ink-3)",
                }}
              >
                {lbl}
              </span>
            </span>
            {i < 2 ? (
              <span style={{ flex: 1, height: 1, minWidth: 18, background: i < active ? "var(--c-accent)" : "var(--c-line)", opacity: i < active ? 0.5 : 1 }} />
            ) : null}
          </span>
        );
      })}
    </div>
  );
}

// ── main card ────────────────────────────────────────────────────────────────


/** Create-tool card failure → explanation key under `create_tool.*` (selected by step/status, no raw API text). */
export function createToolFailureKey(step: "parse" | "authorize" | "create", cause: unknown): string {
  const status =
    cause && typeof cause === "object" && typeof (cause as { status?: unknown }).status === "number"
      ? (cause as { status: number }).status
      : undefined;
  if (status === undefined && isNetworkFailure(cause)) return "failed_network";
  if (status === 403) return "failed_forbidden";
  if (step === "authorize") {
    // Permissions, conflicts, and invalid parameters have distinct handling; only others suggest checking provider OAuth settings.
    if (status === 409) return "create_failed_conflict";
    if (status === 422 || status === 400) return "create_failed_invalid";
    return "authorize_failed_hint";
  }
  if (step === "parse") {
    if (status === 400 || status === 422) return "parse_failed_invalid";
    if (status === 502 || status === 504 || status === 408) return "parse_failed_unreachable";
    return "parse_failed_retry";
  }
  if (status === 409) return "create_failed_conflict";
  if (status === 400 || status === 422) return "create_failed_invalid";
  return "create_failed_retry";
}

export function CreateToolCard({
  specUrlHint,
  kindHint = null,
  presets = [],
  manageStepFocus = false,
  onClose,
  onCreated,
}: CreateToolCardProps) {
  const t = useTranslations("create_tool");
  const formId = useId();
  const sourceUrlId = `${formId}-source-url`;
  const sourceJsonId = `${formId}-source-json`;
  const sourceNameId = `${formId}-source-name`;
  const baseUrlId = `${formId}-base-url`;
  const authMethodId = `${formId}-auth-method`;
  const credentialId = `${formId}-credential`;
  const sourceUrlTabId = `${formId}-source-url-tab`;
  const sourceMcpTabId = `${formId}-source-mcp-tab`;
  const sourceJsonTabId = `${formId}-source-json-tab`;
  const sourceMcpId = `${formId}-source-mcp`;
  const cardRef = useRef<HTMLDivElement | null>(null);
  const [step, setStep] = useState<Step>("source");
  const [srcMode, setSrcMode] = useState<"url" | "json" | "mcp">(kindHint === "mcp" ? "mcp" : "url");
  const [urlVal, setUrlVal] = useState(specUrlHint ?? "");
  const [jsonVal, setJsonVal] = useState("");
  const [busy, setBusy] = useState(false);
  // Show only issue and remedy; raw API text, status codes, and request IDs go in "复制诊断".
  const [error, setError] = useState<ShownError | null>(null);
  const showToolFailure = (step: "parse" | "authorize" | "create", cause: unknown) =>
    setError(shownError(t(createToolFailureKey(step, cause)), cause));
  const [routeAway, setRouteAway] = useState<{ kind: string; hint: string } | null>(null);

  // parsed source
  const [specJson, setSpecJson] = useState("");
  const [ops, setOps] = useState<OpSummary[]>([]);
  const [count, setCount] = useState(0);
  const [draftSourceId, setDraftSourceId] = useState<string | null>(null);
  const [draftSourceToken, setDraftSourceToken] = useState<string | null>(null);

  // human decisions
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [descs, setDescs] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [auth, setAuth] = useState<AuthType>("none");
  const [secret, setSecret] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [filter, setFilter] = useState("");
  const [createdCount, setCreatedCount] = useState(0);
  const [createdAvailableCount, setCreatedAvailableCount] = useState(0);
  const [createdUnavailableWriteCount, setCreatedUnavailableWriteCount] = useState(0);
  const [baseUrl, setBaseUrl] = useState("");
  const [writeAck, setWriteAck] = useState(false);
  // WP2: remote MCP source — kind + the enumerate cache (populated when preview detects an MCP server).
  const [kind, setKind] = useState<"openapi" | "mcp">("openapi");
  const [toolsSchema, setToolsSchema] = useState<Array<Record<string, unknown>>>([]);
  const [promptsSchema, setPromptsSchema] = useState<Array<Record<string, unknown>>>([]);
  // WP2b-3c create-before-authorize OAuth: preview detects OAuth-required MCP server → state 1 guidance callout;
  // provider = provider hint ({provider} copy, domain fallback if unknown); redirecting = state 3 transition (full-page redirect after creation).
  const [detectedOauth, setDetectedOauth] = useState<{ mcp_url: string; provider: string | null } | null>(null);
  const [provider, setProvider] = useState<string | null>(null);
  const [redirecting, setRedirecting] = useState(false);
  // review P2: if authorize fails AFTER the empty oauth source is created, remember its id so a retry
  // re-authorizes it instead of minting a duplicate empty source.
  const [createdOauthSrcId, setCreatedOauthSrcId] = useState<string | null>(null);
  const oauthMode = kind === "mcp" && auth === "oauth";
  const providerLabel = provider || baseUrl.replace(/^https?:\/\//, "").split("/")[0] || t("provider_fallback");

  useEffect(() => {
    if (!manageStepFocus) return;
    const frame = window.requestAnimationFrame(() => {
      cardRef.current
        ?.querySelector<HTMLElement>(`[data-create-step-focus="${step}"]`)
        ?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [manageStepFocus, step]);

  const isLarge = count > LARGE_SPEC_OPS;
  const selectedExecutableWriteCount = ops.filter(
    (op) => op.is_write && op.approval_executable === true && sel.has(op.operation_id),
  ).length;
  const selectedUnavailableWriteCount = ops.filter(
    (op) => op.is_write && op.approval_executable !== true && sel.has(op.operation_id),
  ).length;

  const toggle = useCallback((id: string) => {
    // Bind the acknowledgement to the exact selection it described.
    setWriteAck(false);
    setSel((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);

  // ── source step → preview ──
  // override lets a preset pick drive the preview without waiting for setUrlVal/setSrcMode to
  // commit (same-tick stale-state) — existing callers pass nothing → read state as before.
  const doPreview = async (override?: { url?: string; mode?: "url" | "json"; kind?: "mcp" }) => {
    const mode = override?.mode ?? srcMode;
    const url = (override?.url ?? urlVal).trim();
    // F2: the MCP tab takes the SAME direct branch a curated MCP preset takes — no OpenAPI-first
    // probe, because we already know what this address is. (The probe still exists as the safety
    // net for an MCP address pasted into the OpenAPI tab.)
    const wantsMcp = override?.kind === "mcp" || mode === "mcp";
    setBusy(true);
    setError(null);
    setRouteAway(null);
    setDraftSourceId(null);
    setDraftSourceToken(null);
    setToolsSchema([]);
    setPromptsSchema([]);
    setWriteAck(false);
    // review P2: reset transient config carried from a prior (OAuth) preview — else a stale auth="oauth"
    // breaks a subsequent OpenAPI/open-MCP create. Each preview re-derives kind/ops/sel in its branch.
    if ((override?.mode ?? srcMode) !== "mcp") setAuth("none"); // MCP mode may have a newly entered token; do not clear it
    setProvider(null);
    setDetectedOauth(null);
    setCreatedOauthSrcId(null);
    try {
      // external-capability-onboarding §5 — a curated non-OAuth MCP preset already knows it's MCP:
      // preview it directly as MCP. (The OpenAPI-first probe below 502s on some MCP endpoints instead
      // of route_away=mcp_server, which would dead-end this path — so a known-MCP source skips it.)
      if (wantsMcp) {
        // A token-protected MCP server (GitHub's hosted server takes a PAT) refuses an
        // unauthenticated tools/list, and that 401 is indistinguishable from "needs OAuth" —
        // so without carrying the credential HERE it gets routed into the OAuth flow and
        // dead-ends. Optional: a no-auth server previews exactly as before.
        const token = secret.trim();
        const mcp = await previewSource(
          token ? { kind: "mcp", mcp_url: url, auth_type: "bearer", auth: { value: token } } : { kind: "mcp", mcp_url: url },
        );
        if (!mcp.route_away && mcp.kind === "mcp") {
          if ("needs_oauth" in mcp) {
            // a "none" preset whose server actually requires OAuth → route into create-before-authorize.
            setDetectedOauth({ mcp_url: mcp.mcp_url, provider: mcp.provider_hint });
            return;
          }
          setKind("mcp");
          setDraftSourceId(mcp.draft_source_id ?? null);
          setDraftSourceToken(mcp.draft_source_token ?? null);
          setToolsSchema(mcp.tools_schema);
          setPromptsSchema(mcp.prompts_schema ?? []);
          setOps(mcp.operations);
          setCount(mcp.count);
          setBaseUrl(url);
          if (token) setAuth("bearer"); // carry it into configure + create (createSource sends {value})
          const mreads = mcp.operations.filter((o) => !o.is_write).map((o) => o.operation_id);
          setSel(new Set(mcp.count > LARGE_SPEC_OPS ? [] : mreads));
          setStep("configure");
          return;
        }
        // unreachable: previewSource({kind:"mcp"}) always yields kind="mcp" (or the needs_oauth
        // variant) on success and throws otherwise — no route-away variant to forward here.
        return;
      }
      const body = mode === "json" ? { openapi_spec_json: jsonVal } : { spec_url: url };
      const res: PreviewResult = await previewSource(body);
      if (res.route_away) {
        // WP2: a remote MCP server → connect + enumerate its tools instead of routing away.
        if (res.kind === "mcp_server" && mode === "url") {
          try {
            const mcp = await previewSource({ kind: "mcp", mcp_url: url });
            if (!mcp.route_away && mcp.kind === "mcp") {
              // WP2b-3c: the server REQUIRES OAuth → can't list tools unauthenticated. Route into the
              // create-before-authorize flow (state 1 detect callout) instead of failing.
              if ("needs_oauth" in mcp) {
                setDetectedOauth({ mcp_url: mcp.mcp_url, provider: mcp.provider_hint });
                return;
              }
              setKind("mcp");
              setDraftSourceId(mcp.draft_source_id ?? null);
              setDraftSourceToken(mcp.draft_source_token ?? null);
              setToolsSchema(mcp.tools_schema);
              setPromptsSchema(mcp.prompts_schema ?? []);
              setOps(mcp.operations);
              setCount(mcp.count);
              setBaseUrl(url);
              const mreads = mcp.operations.filter((o) => !o.is_write).map((o) => o.operation_id);
              setSel(new Set(mcp.count > LARGE_SPEC_OPS ? [] : mreads));
              setStep("configure");
              return;
            }
          } catch {
            /* couldn't connect as MCP → fall through to the polite route-away hint */
          }
        }
        setRouteAway({ kind: res.kind, hint: res.hint }); // change (d): polite B-type route-away
        return;
      }
      if (!("openapi_spec_json" in res)) return; // narrow: the top-level preview never returns the MCP variant
      setKind("openapi");
      setDraftSourceId(res.draft_source_id ?? null);
      setDraftSourceToken(res.draft_source_token ?? null);
      setSpecJson(res.openapi_spec_json);
      setOps(res.operations);
      setCount(res.count);
      setBaseUrl(deriveBaseUrl(res.openapi_spec_json, mode === "url" ? url : null));
      // pre-select read ops by default (change c — guide a subset; writes opt-in)
      const reads = res.operations.filter((o) => !o.is_write).map((o) => o.operation_id);
      setSel(new Set(res.count > LARGE_SPEC_OPS ? [] : reads));
      setStep("configure");
    } catch (e) {
      showToolFailure("parse", e);
    } finally {
      setBusy(false);
    }
  };

  // ── propose descriptions (change b) ──
  const doPropose = async () => {
    if (!sel.size || kind === "mcp") return; // MCP tool descriptions come from the server itself
    setBusy(true);
    try {
      const res = await proposeDescriptions({
        openapi_spec_json: specJson,
        selected_op_ids: [...sel],
      });
      setDescs((d) => ({ ...res.proposals, ...d })); // keep any edits the admin already made
    } catch {
      /* propose is best-effort — the admin can write descriptions manually */
    } finally {
      setBusy(false);
    }
  };

  // WP2b-3c: state 1 "继续（去命名）" → configuration OAuth variant (kind=mcp + auth=oauth, no tool list).
  const continueToOauthConfigure = () => {
    if (!detectedOauth) return;
    setKind("mcp");
    setAuth("oauth");
    setBaseUrl(detectedOauth.mcp_url);
    setProvider(detectedOauth.provider);
    if (!name.trim()) setName(detectedOauth.provider || detectedOauth.mcp_url.replace(/^https?:\/\//, "").split("/")[0]);
    setDetectedOauth(null);
    setStep("configure");
  };

  // external-capability-onboarding §3.1 — selecting preset connector = one-click configuration (mirrored design):
  //  · hosted-MCP + OAuth (all curated presets) → directly enter create-before-authorize configure (same as
  //    continueToOauthConfigure, without pointless preview because curated auth is known oauth);
  //  · openapi / open-mcp → prefill URL + trigger existing preview (override passes real URL to avoid same-tick stale value).
  // Pure frontend seeding without SSE (preserves §3.1 contract).
  const pickPreset = (p: ConnectorPreset) => {
    const url = p.mcp_url ?? p.spec_url ?? "";
    setRouteAway(null);
    setDetectedOauth(null);
    setError(null);
    setSrcMode("url");
    setUrlVal(url);
    setName((prev) => (prev.trim() ? prev : p.name));
    if (p.kind === "mcp" && p.auth !== "none" && p.auth !== "oauth") {
      // A static-credential preset: let user paste token first; anonymous preview must return 401.
      setSrcMode("mcp");
      setKind("mcp");
      setAuth(p.auth as AuthType);
      setBaseUrl(url);
      return;
    }
    if (p.kind === "mcp" && p.auth === "oauth") {
      setKind("mcp");
      setAuth("oauth");
      setBaseUrl(url);
      setProvider(null);
      setStep("configure");
      return;
    }
    // §5: a non-OAuth MCP preset previews directly as MCP (see doPreview); OpenAPI presets keep the
    // OpenAPI-first probe. (OAuth+MCP already returned above via the create-then-authorize shortcut.)
    void doPreview({ url, mode: "url", kind: p.kind === "mcp" ? "mcp" : undefined });
  };

  // WP2b-3c: create and authorize — empty oauth source (0 tools) → initiate authorization → full-page provider redirect (state 3 transition).
  const doCreateAndAuthorize = async () => {
    setBusy(true);
    setError(null);
    setRedirecting(true);
    // Failure attribution: before srcId exists, source creation failed; only afterward can authorization initiation fail.
    let failedStep: "create" | "authorize" = "create";
    try {
      // review P2: only mint the empty source once — a retry after an authorize failure re-authorizes
      // the SAME source instead of creating a duplicate empty "待授权" row.
      let srcId = createdOauthSrcId;
      if (!srcId) {
        const src = await createSource({
          kind: "mcp",
          name: name.trim(),
          base_url: baseUrl.trim(),
          selected_op_ids: [],
          auth_type: "oauth",
          visibility: "team_wide",
        });
        srcId = src.id;
        setCreatedOauthSrcId(srcId);
      }
      failedStep = "authorize";
      const { authorization_url } = await authorizeSource(srcId);
      window.location.href = authorization_url; // Leave entire page for provider; do not return to this component
    } catch (e) {
      // keep the modal open: show the error; the retry re-authorizes the SAME source (createdOauthSrcId),
      // no duplicate. The empty source persists as a "待授权" row and surfaces on the next list load.
      showToolFailure(failedStep, e);
      setRedirecting(false);
      setBusy(false);
    }
  };

  // ── create ──
  const canCreate = oauthMode
    ? !!name.trim() && !!baseUrl.trim()
    : sel.size >= 1 && !!name.trim() && !!baseUrl.trim() &&
      (auth === "none" || !!secret) && (selectedExecutableWriteCount === 0 || writeAck);
  const doCreate = async () => {
    setBusy(true);
    setError(null);
    try {
      const tool_overrides: Record<string, { description: string }> = {};
      for (const id of sel) {
        const d = (descs[id] ?? "").trim();
        if (d) tool_overrides[id] = { description: d };
      }
      // FE-1/CONTRACT-1: _build_auth_headers reads creds["value"] for BOTH bearer + api_key.
      const authBody = auth === "none" ? null : { value: secret };
      const previewIdentity =
        draftSourceId && draftSourceToken
          ? { draft_source_id: draftSourceId, draft_source_token: draftSourceToken }
          : {};
      const src = await createSource(
        kind === "mcp"
          ? {
              ...previewIdentity,
              kind: "mcp",
              name: name.trim(),
              base_url: baseUrl.trim(),
              tools_schema: toolsSchema,
              prompts_schema: promptsSchema,
              selected_op_ids: [...sel],
              tool_overrides,
              auth_type: auth,
              auth: authBody,
              visibility: "team_wide",
            }
          : {
              ...previewIdentity,
              name: name.trim(),
              base_url: baseUrl.trim(),
              openapi_spec_json: specJson,
              selected_op_ids: [...sel],
              tool_overrides,
              auth_type: auth,
              auth: authBody,
              visibility: "team_wide",
            },
      );
      const unavailableCount = src.approval_unavailable_count ?? selectedUnavailableWriteCount;
      setCreatedCount(src.op_count);
      setCreatedAvailableCount(src.available_op_count ?? Math.max(0, src.op_count - unavailableCount));
      setCreatedUnavailableWriteCount(unavailableCount);
      setStep("success");
      onCreated?.(src.name, src.op_count);
    } catch (e) {
      showToolFailure("create", e);
    } finally {
      setBusy(false);
    }
  };

  // group ops by tag — ONLY for large specs (change c); small specs stay a flat list (FE-8)
  const grouped = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const visible = ops.filter(
      (o) => !f || (o.path + o.operation_id + o.summary).toLowerCase().includes(f),
    );
    if (!isLarge) return new Map<string, OpSummary[]>([["", visible]]);
    const m = new Map<string, OpSummary[]>();
    for (const o of visible) {
      const key = o.tags[0] ?? NO_TAG;
      const arr = m.get(key) ?? [];
      arr.push(o);
      m.set(key, arr);
    }
    return m;
  }, [ops, filter, isLarge]);

  const header = (
    <div className="legacy-ctool-head">
      <span
        style={{
          width: 36, height: 36, flex: "0 0 36px", borderRadius: 11, background: "var(--c-accent-soft)",
          color: "var(--c-accent)", display: "grid", placeItems: "center",
        }}
      >
        <Icon name="plug" s={19} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 9, flexWrap: "wrap" }}>
          <h3 className="legacy-ctool-title">
            {t("title")}
          </h3>
          <span className="bh-mono legacy-ctool-confirm-badge">
            {t("confirm_badge")}
          </span>
        </div>
        {/* change (a): a SOURCE of N tools, not a single tool. */}
        <p className="legacy-ctool-subtitle">
          {t("subtitle")}
        </p>
      </div>
      <button
        type="button"
        aria-label={t("close_aria")}
        onClick={onClose}
        className="legacy-ctool-close"
      >
        <Icon name="x" s={17} />
      </button>
    </div>
  );

  // ── render: SOURCE ──
  function renderSource() {
    if (routeAway) {
      return (
        <div style={{ padding: "16px 18px 18px", display: "flex", flexDirection: "column", gap: 13 }}>
          <Callout tone="info" icon="info">
            <b style={{ fontWeight: 600 }}>{t("not_openapi")}</b> {routeAway.hint}
          </Callout>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button type="button" className={BTN_GHOST} onClick={() => setRouteAway(null)}>
              <Icon name="rotateCw" s={13} />{t("re_enter")}
            </button>
          </div>
        </div>
      );
    }
    // WP2b-3c state 1: preview detects OAuth-required MCP server → guide create-before-authorize rather than dead-end connection failure.
    if (detectedOauth) {
      const who = detectedOauth.provider || detectedOauth.mcp_url.replace(/^https?:\/\//, "").split("/")[0];
      return (
        <div style={{ padding: "16px 18px 18px", display: "flex", flexDirection: "column", gap: 13 }}>
          <Callout tone="info" icon="lock">
            <div>
              <b style={{ fontWeight: 600 }}>{t("detect_oauth_title", { provider: who })}</b>
              <div style={{ marginTop: 5, lineHeight: 1.55 }}>{t("detect_oauth_body")}</div>
              <div className="bh-mono" style={{ marginTop: 9, fontSize: "0.71875rem", color: "var(--c-ink-3)", wordBreak: "break-all" }}>
                {detectedOauth.mcp_url.replace(/^https?:\/\//, "")}
              </div>
            </div>
          </Callout>
          <div style={{ display: "flex", alignItems: "center" }}>
            <button type="button" className={BTN_GHOST} onClick={() => setDetectedOauth(null)}>
              <Icon name="rotateCw" s={13} />{t("re_enter")}
            </button>
            <div style={{ flex: 1 }} />
            <button type="button" className={BTN_PRIMARY} onClick={continueToOauthConfigure}>
              {t("detect_oauth_cta")}<Icon name="arrowRight" s={14} />
            </button>
          </div>
        </div>
      );
    }
    return (
      <div style={{ padding: "16px 18px 18px", display: "flex", flexDirection: "column", gap: 13 }}>
        {/* external-capability-onboarding §3.1 — one-click preset directory on top of
            the manual entry. Omitted (byte-identical to today) when no presets supplied. */}
        {presets.length > 0 ? (
          <>
            <PresetDirectory presets={presets} onPick={pickPreset} />
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span style={{ flex: 1, height: 1, background: "var(--c-line)" }} />
              <span style={{ fontSize: "0.6875rem", fontWeight: 500, color: "var(--c-ink-4)", whiteSpace: "nowrap" }}>{t("manual_divider")}</span>
              <span style={{ flex: 1, height: 1, background: "var(--c-line)" }} />
            </div>
          </>
        ) : null}
        <Tabs value={srcMode} onValueChange={setSrcMode}>
        <TabList
          aria-label={t("step_source")}
          style={{
            display: "flex", gap: 4, padding: 4, background: "var(--c-surface-2)",
            borderRadius: "var(--r-2)", border: "1px solid var(--c-line)",
          }}
        >
          {([["url", t("tab_url")], ["mcp", t("tab_mcp")], ["json", t("tab_json")]] as const).map(
            ([k, lbl]) => {
              const on = srcMode === k;
              const tabId = k === "url" ? sourceUrlTabId : k === "mcp" ? sourceMcpTabId : sourceJsonTabId;
              const panelId = k === "url" ? sourceUrlId : k === "mcp" ? sourceMcpId : sourceJsonId;
              return (
                <UiTab
                  key={k}
                  value={k}
                  id={tabId}
                  aria-controls={panelId}
                  style={{
                    flex: 1, fontSize: "0.78125rem", fontWeight: 600, padding: "8px 10px", borderRadius: 9,
                    border: `1px solid ${on ? "var(--c-line)" : "transparent"}`, cursor: "pointer",
                    background: on ? "var(--c-surface)" : "transparent",
                    color: on ? "var(--c-accent)" : "var(--c-ink-3)",
                    boxShadow: on ? "var(--shadow-1)" : "none",
                  }}
                >
                  {lbl}
                </UiTab>
              );
            },
          )}
        </TabList>
        </Tabs>
        {srcMode !== "json" ? (
          /* The panel role moved to this column wrapper so the MCP token field lives INSIDE the
             tab panel it belongs to (it used to be a sibling of it). Field styling is unchanged —
             `create-tool-compound-field` still sits on each individual field below. */
          <div
            id={srcMode === "mcp" ? sourceMcpId : sourceUrlId}
            role="tabpanel"
            aria-labelledby={srcMode === "mcp" ? sourceMcpTabId : sourceUrlTabId}
            style={{ display: "flex", flexDirection: "column", gap: 8 }}
          >
          <div
            className="create-tool-compound-field"
            style={{
              display: "flex", alignItems: "center", gap: 9, border: "1.5px solid var(--c-line)",
              borderRadius: "var(--r-1)", background: "var(--c-surface)", padding: "0 6px 0 12px",
            }}
          >
            <span style={{ color: "var(--c-ink-4)", flex: "0 0 auto" }}>
              <Icon name="plug" s={15} />
            </span>
            <input
              name="tool_source_url"
              type="url"
              autoComplete="url"
              aria-label={srcMode === "mcp" ? t("tab_mcp") : t("tab_url")}
              data-create-step-focus="source"
              className="bh-mono"
              value={urlVal}
              onChange={(e) => setUrlVal(e.target.value)}
              spellCheck={false}
              placeholder={srcMode === "mcp" ? t("mcp_placeholder") : t("url_placeholder")}
              style={{ flex: 1, border: "none", outline: "none", background: "transparent", padding: "11px 0", fontSize: "0.78125rem", color: "var(--c-ink)" }}
            />
          </div>
          {srcMode === "mcp" ? (
          /* Optional credential, collected BEFORE preview: a token-protected MCP server refuses an
             unauthenticated tools/list, and that 401 reads as "needs OAuth" — so asking for it only
             at the configure step would never get there. Same field styling as the address input. */
          <div
            className="create-tool-compound-field"
            style={{
              display: "flex", alignItems: "center", gap: 9, border: "1.5px solid var(--c-line)",
              borderRadius: "var(--r-1)", background: "var(--c-surface)", padding: "0 6px 0 12px",
            }}
          >
            <span style={{ color: "var(--c-ink-4)", flex: "0 0 auto" }}>
              <Icon name="lock" s={15} />
            </span>
            <input
              className="bh-mono"
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              placeholder={t("mcp_token_placeholder")}
              aria-label={t("mcp_token_label")}
              style={{ flex: 1, border: "none", outline: "none", background: "transparent", padding: "11px 0", fontSize: "0.78125rem", color: "var(--c-ink)" }}
            />
          </div>
          ) : null}
          </div>
        ) : null}
        {srcMode === "json" ? (
          <div id={sourceJsonId} role="tabpanel" aria-labelledby={sourceJsonTabId}>
            <textarea
              name="tool_source_json"
              autoComplete="off"
              aria-label={t("tab_json")}
              data-create-step-focus="source"
              className="bh-mono bh-scroll"
              value={jsonVal}
              onChange={(e) => setJsonVal(e.target.value)}
              spellCheck={false}
              rows={5}
              placeholder={'{\n  "openapi": "3.0.0",\n  "paths": { … }\n}'}
              style={{ ...fieldInput(), resize: "vertical", fontSize: "0.75rem", lineHeight: 1.6 }}
            />
          </div>
        ) : null}
        <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: "0.71875rem", color: "var(--c-ink-4)" }}>
          <span style={{ color: "var(--c-info)" }}>
            <Icon name="shieldCheck" s={14} />
          </span>
          {t(srcMode === "mcp" ? "mcp_safety_note" : srcMode === "json" ? "json_safety_note" : "url_safety_note")}
        </div>
        {error ? (
          <Callout tone="err" icon="alertTriangle">
            <b style={{ fontWeight: 600 }}>{t("parse_failed")}</b> {error.message}
          </Callout>
        ) : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          {(() => {
            const empty = srcMode === "json" ? !jsonVal.trim() : !urlVal.trim();
            return (
              <button type="button" disabled={busy || empty} className={BTN_PRIMARY} onClick={() => void doPreview()}>
                <Icon name="wand" s={15} />
                {busy ? t("parsing") : t("parse")}
              </button>
            );
          })()}
        </div>
      </div>
    );
  }

  // ── render: CONFIGURE ──
  function renderConfigure() {
    // WP2b-3c state 3: transition after empty oauth source creation, before full-page redirect (avoid appearing stalled).
    if (redirecting) {
      return (
        <div style={{ padding: "44px 22px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 13 }}>
          <div style={{ width: 30, height: 30, borderRadius: "50%", border: "2.5px solid var(--c-line)", borderTopColor: "var(--c-accent)", animation: "bowhead-spin 0.9s linear infinite" }} />
          <div style={{ fontSize: "0.875rem", fontWeight: 600, color: "var(--c-ink)" }}>{t("creating_redirecting_title", { provider: providerLabel })}</div>
          <div style={{ fontSize: "0.78125rem", color: "var(--c-ink-3)", lineHeight: 1.55, maxWidth: "40ch" }}>{t("creating_redirecting_body")}</div>
          <div className="bh-mono" style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)", wordBreak: "break-all" }}>{baseUrl.replace(/^https?:\/\//, "")}</div>
        </div>
      );
    }
    // WP2b-3c state 2: OAuth variant — no tool list (cannot enumerate unauthorized); replace with "授权后同步" placeholder + create/authorize.
    if (oauthMode) {
      return (
        <div className="bh-scroll" style={{ padding: "16px 18px 16px", display: "flex", flexDirection: "column", gap: 15 }}>
          <div>
            <label htmlFor={sourceNameId} style={{ display: "block", fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)", marginBottom: 6 }}>{t("source_name")}</label>
            <input id={sourceNameId} name="tool_source_name" autoComplete="organization" data-create-step-focus="configure" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} style={fieldInput()} />
            <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 6 }}>{t("oauth_name_hint")}</div>
          </div>
          <div>
            <label style={{ display: "block", fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)", marginBottom: 6 }}>{t("oauth_source_label")}</label>
            <div className="legacy-ctool-source">
              <span className="bh-mono legacy-ctool-source-kind">MCP</span>
              <span className="bh-mono" style={{ fontSize: "0.75rem", color: "var(--c-ink-2)", wordBreak: "break-all", flex: 1, minWidth: 0 }}>{baseUrl.replace(/^https?:\/\//, "")}</span>
              <span className="legacy-ctool-source-auth">
                <Icon name="lock" s={11} />{t("oauth_recap_auth")}
              </span>
            </div>
            <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 6 }}>{t("oauth_source_hint")}</div>
          </div>
          {/* review P2: the auth method here is READ-ONLY OAuth (shown in the recap above). A switchable
              select would let the user pick bearer/none on a detect-sourced flow that has no enumerated
              tools → a 0-op dead-end. To use a different auth, go Back and re-preview. */}
          <div style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "13px 14px", border: "1px dashed var(--c-line)", borderRadius: "var(--r-2)", background: "var(--c-surface)" }}>
            <span style={{ color: "var(--c-ink-3)", flex: "0 0 auto", marginTop: 1 }}><Icon name="plug" s={17} /></span>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <b style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink)" }}>{t("oauth_tools_placeholder_title")}</b>
              <span style={{ fontSize: "0.71875rem", color: "var(--c-ink-3)", lineHeight: 1.55 }}>{t("oauth_tools_placeholder_body")}</span>
            </div>
          </div>
          <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-3)", display: "inline-flex", alignItems: "center", gap: 6, lineHeight: 1.5 }}>
            <Icon name="lock" s={13} />{t("oauth_leave_note", { provider: providerLabel })}
          </div>
          {error ? (
            <Callout tone="err" icon="alertTriangle">
              <b style={{ fontWeight: 600 }}>{t("create_failed")}</b> {error.message}
            </Callout>
          ) : null}
          {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 3, paddingTop: 14, borderTop: "1px solid var(--c-line-2)" }}>
            <button type="button" className={BTN_GHOST} onClick={() => setStep("source")}>
              <Icon name="chevronLeft" s={15} />{t("back")}
            </button>
            <div style={{ flex: 1 }} />
            <button type="button" disabled={!canCreate || busy} className={BTN_PRIMARY} onClick={() => void doCreateAndAuthorize()}>
              {t("create_and_authorize")}<Icon name="arrowRight" s={15} />
            </button>
          </div>
        </div>
      );
    }
    return (
      <div className="bh-scroll" style={{ padding: "16px 18px 16px", display: "flex", flexDirection: "column", gap: 15 }}>
        <div
          style={{
            display: "flex", alignItems: "center", gap: 9, padding: "8px 11px",
            background: "var(--c-pos-soft)", border: "1px solid var(--c-pos-line)", borderRadius: "var(--r-2)",
          }}
        >
          <span style={{ color: "var(--c-pos)", flex: "0 0 auto" }}>
            <Icon name="check" s={14} />
          </span>
          <span style={{ fontSize: "0.75rem", color: "var(--c-ink-2)" }}>{t("parsed")}</span>
          <span className="bh-mono" style={{ fontSize: "0.65625rem", color: "var(--c-ink-4)", marginLeft: "auto" }}>
            {t("op_count", { count })}
          </span>
        </div>

        {/* change (c): tool-count warning + group-by-tag for large APIs */}
        {isLarge ? (
          <Callout tone="warn" icon="alertTriangle">
            {t("large_warn_pre", { count })}
            <b>{t("large_warn_emphasis")}</b>
            {t("large_warn_post")}
          </Callout>
        ) : null}

        {/* op selection — flat list (small) or grouped-by-tag (large) */}
        <div
          style={{
            border: "1px solid var(--c-line)", borderRadius: "var(--r-3)", background: "var(--c-surface)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex", alignItems: "center", gap: 10, padding: "11px 13px",
              borderBottom: "1px solid var(--c-line-2)", background: "var(--c-surface-2)",
            }}
          >
            <span style={{ color: "var(--c-ink-3)" }}>
              <Icon name="sliders" s={15} />
            </span>
            <span style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink)" }}>{t("select_ops")}</span>
            <span
              className="bh-mono"
              style={{
                fontSize: "0.65625rem", color: "var(--c-accent)", background: "var(--c-accent-tint)",
                border: "1px solid var(--c-accent-line)", borderRadius: 999, padding: "2px 8px",
              }}
            >
              {t("selected_count", { sel: sel.size, total: count })}
            </span>
            <div style={{ flex: 1 }} />
            <button
              type="button"
              onClick={() => void doPropose()}
              disabled={busy || sel.size === 0}
              style={{
                fontSize: "0.75rem", fontWeight: 600, color: "var(--c-accent)", background: "transparent",
                border: "none", cursor: sel.size ? "pointer" : "default", padding: 4,
                display: "inline-flex", alignItems: "center", gap: 4,
              }}
            >
              <Icon name="sparkles" s={13} />{t("ai_suggest_desc")}
            </button>
          </div>
          <div style={{ padding: "9px 11px", borderBottom: "1px solid var(--c-line-2)" }}>
            <div
              style={{
                display: "flex", alignItems: "center", gap: 8, border: "1px solid var(--c-line)",
                borderRadius: "var(--r-1)", padding: "0 10px", background: "var(--c-bg)",
              }}
            >
              <span style={{ color: "var(--c-ink-4)" }}>
                <Icon name="search" s={14} />
              </span>
              <input
                name="tool_operation_filter"
                autoComplete="off"
                aria-label={t("filter_ops")}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={t("filter_ops")}
                style={{ flex: 1, border: "none", outline: "none", background: "transparent", padding: "7px 0", fontSize: "0.78125rem", color: "var(--c-ink)" }}
              />
            </div>
          </div>
          <div style={{ padding: 6, display: "flex", flexDirection: "column", gap: 2 }}>
            {[...grouped.entries()].map(([tag, list]) => (
              <div key={tag}>
                {tag !== "" ? (
                  <div style={{ fontSize: "0.6875rem", fontWeight: 600, color: "var(--c-ink-4)", padding: "8px 8px 4px", letterSpacing: ".02em" }}>
                    {tag === NO_TAG ? t("no_tag") : tag} · {list.length}
                  </div>
                ) : null}
                {list.map((op) => (
                  <OpRow
                    key={op.operation_id}
                    op={op}
                    on={sel.has(op.operation_id)}
                    onToggle={() => toggle(op.operation_id)}
                    desc={descs[op.operation_id] ?? ""}
                    onDesc={(v) => setDescs((d) => ({ ...d, [op.operation_id]: v }))}
                    t={t}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>

        {selectedExecutableWriteCount > 0 || selectedUnavailableWriteCount > 0 ? (
          <Callout tone="warn" icon="alertTriangle">
            <div>
              {selectedExecutableWriteCount > 0 ? (
                <div>{t("write_executable_notice", { count: selectedExecutableWriteCount })}</div>
              ) : null}
              {selectedUnavailableWriteCount > 0 ? (
                <div style={{ marginTop: selectedExecutableWriteCount > 0 ? 6 : 0 }}>
                  {t("write_unavailable_notice", { count: selectedUnavailableWriteCount })}
                </div>
              ) : null}
              {selectedExecutableWriteCount > 0 ? (
                <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10, cursor: "pointer" }}>
                  <Check on={writeAck} label={t("write_ack")} onToggle={() => setWriteAck((a) => !a)} />
                  <span style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-warn-ink)" }}>{t("write_ack")}</span>
                </label>
              ) : null}
            </div>
          </Callout>
        ) : null}

        {/* naming + auth */}
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div>
            <label htmlFor={sourceNameId} style={{ display: "block", fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)", marginBottom: 6 }}>
              {t("source_name")}
            </label>
            <input id={sourceNameId} name="tool_source_name" autoComplete="organization" data-create-step-focus="configure" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} style={fieldInput()} />
            <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 6 }}>
              {t("source_name_hint")}
            </div>
          </div>
          {/* base_url — derived from the spec's servers[0].url (or the spec-URL origin); editable,
              and REQUIRED (a pasted JSON spec with no servers would otherwise 422). FE-5/CONTRACT-4. */}
          <div>
            <label htmlFor={baseUrlId} style={{ display: "block", fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)", marginBottom: 6 }}>
              {t("base_url_label")}
            </label>
            <input
              id={baseUrlId}
              name="tool_base_url"
              type="url"
              autoComplete="url"
              className="bh-mono"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.example.com"
              style={{ ...fieldInput(!baseUrl.trim()), fontSize: "0.78125rem" }}
            />
            <div style={{ fontSize: "0.71875rem", color: "var(--c-ink-4)", marginTop: 6 }}>
              {t("base_url_hint")}
            </div>
          </div>
          <div>
            <label htmlFor={authMethodId} style={{ display: "block", fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-2)", marginBottom: 6 }}>
              {t("auth_method")}
            </label>
            <select
              id={authMethodId}
              name="tool_auth_method"
              autoComplete="off"
              value={auth}
              onChange={(e) => setAuth(e.target.value as AuthType)}
              style={{ ...fieldInput(), cursor: "pointer" }}
            >
              <option value="none">{t("auth_none")}</option>
              <option value="bearer">Bearer Token</option>
              <option value="api_key">API Key</option>
              {/* WP2b-3c state 2b: OAuth is MCP-only (create-before-authorize flow) — selecting it switches to OAuth variant. */}
              {kind === "mcp" ? <option value="oauth">{t("auth_oauth")}</option> : null}
              {/* Basic dropped for P2: backend needs username+password (2 fields); the card has one
                  secret field, so offering it would silently fail at dispatch (FE-1/CONTRACT-2). */}
            </select>
          </div>
          {auth !== "none" ? (
            <VaultPanel
              value={secret}
              show={showKey}
              onToggleShow={() => setShowKey((s) => !s)}
              onChange={setSecret}
              method={auth}
              inputId={credentialId}
              t={t}
            />
          ) : null}
        </div>

        {error ? (
          <Callout tone="err" icon="alertTriangle">
            <b style={{ fontWeight: 600 }}>{t("create_failed")}</b> {error.message}
          </Callout>
        ) : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}

        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 3, paddingTop: 14, borderTop: "1px solid var(--c-line-2)" }}>
          <button type="button" className={BTN_GHOST} onClick={() => setStep("source")}>
            <Icon name="chevronLeft" s={15} />{t("back")}
          </button>
          <div style={{ flex: 1 }} />
          <button type="button" disabled={!canCreate || busy} className={BTN_PRIMARY} onClick={() => void doCreate()}>
            <Icon name="check" s={16} />
            {busy ? t("creating") : t("create_tool_btn")}
          </button>
        </div>
      </div>
    );
  }

  // ── render: SUCCESS (change a) ──
  function renderSuccess() {
    return (
      <div style={{ padding: "30px 22px 24px", display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center" }}>
        <div
          style={{
            width: 54, height: 54, borderRadius: "50%", background: "var(--c-pos-soft)",
            border: "1.5px solid var(--c-pos-line)", color: "var(--c-pos)", display: "grid", placeItems: "center", marginBottom: 16,
          }}
        >
          <Icon name="check" s={28} />
        </div>
        <h3 style={{ margin: 0, fontSize: "1.125rem", fontWeight: 600, color: "var(--c-ink)", letterSpacing: "-.01em" }}>
          {t("success_title", { name, count: createdCount })}
        </h3>
        <p style={{ margin: "8px 0 0", fontSize: "0.8125rem", color: "var(--c-ink-3)", lineHeight: 1.6, maxWidth: 420 }}>
          {t("success_sub", { count: createdCount, available: createdAvailableCount, auth: auth === "none" ? t("auth_none") : auth })}
        </p>
        {createdUnavailableWriteCount > 0 ? (
          <div
            style={{
              marginTop: 14, maxWidth: 420, padding: "9px 12px", borderRadius: "var(--r-1)",
              color: "var(--c-warn-ink)", background: "var(--c-warn-soft)",
              border: "1px solid var(--c-warn-line)", fontSize: "0.75rem", lineHeight: 1.55,
            }}
          >
            {t("success_unavailable", { count: createdUnavailableWriteCount })}
          </div>
        ) : null}
        {auth !== "none" ? (
          <div
            style={{
              display: "flex", alignItems: "center", gap: 8, marginTop: 16, padding: "9px 14px",
              background: "var(--c-info-soft)", border: "1px solid var(--c-info-line)", borderRadius: 999,
            }}
          >
            <span style={{ color: "var(--c-info-ink)" }}>
              <Icon name="lock" s={14} />
            </span>
            <span style={{ fontSize: "0.75rem", color: "var(--c-info-ink)", fontWeight: 500 }}>
              {t("success_encrypted")}
            </span>
          </div>
        ) : null}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 24 }}>
          <button type="button" data-create-step-focus="success" className={`${BTN_PRIMARY} legacy-ctool-btn--wide`} onClick={onClose}>
            <Icon name="check" s={16} />{t("done")}
          </button>
        </div>
      </div>
    );
  }

  const active: 0 | 1 | 2 = step === "success" ? 2 : step === "source" ? 0 : 1;

  return (
    <div ref={cardRef} className="legacy-ctool-card" data-testid="create-tool-card" data-step={step}>
      {header}
      <Stepper active={active} t={t} />
      {step === "source" ? renderSource() : step === "configure" ? renderConfigure() : renderSuccess()}
    </div>
  );
}
