// preset-directory.tsx — external-capability-onboarding §3.1
// Preset connector directory, embedded atop the CreateToolCard source step: common connector cards,
// click one → onPick(preset) (parent seeds source fields + reuses existing preview/oauth flow).
// Translated verbatim from Claude Design bowhead-v10/connector-directory.jsx; presentation only,
// parent supplies presets (backend entitlement filtering → render nothing when empty, naturally gated).
//
// Visuals: reuse global app canonical/semantic --c-* tokens + bh-mono.
// Copy uses next-intl (create_tool namespace, zh-CN/en-US); retain literal MCP/REST protocol names.
"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import type { LucideIcon } from "lucide-react";
import {
  ArrowUpRight as LucideArrowUpRight,
  Ban as LucideBan,
  Braces as LucideBraces,
  Cloud as LucideCloud,
  CreditCard as LucideCreditCard,
  FileText as LucideFileText,
  GitBranch as LucideGitBranch,
  Hash as LucideHash,
  Layers as LucideLayers,
  Lock as LucideLock,
  Plug as LucidePlug,
  Search as LucideSearch,
  ShieldCheck as LucideShieldCheck,
  X as LucideX,
} from "lucide-react";
import { renderRefreshSvg } from "@/components/icon-refresh";


export type PresetAuth = "none" | "bearer" | "api_key" | "basic" | "oauth";
export type PresetKind = "openapi" | "mcp";

export type ConnectorPreset = {
  id: string;
  name: string;
  /** icon key in PD_ICONS */
  icon: string;
  kind: PresetKind;
  auth: PresetAuth;
  blurb?: string;
  /** openapi: spec document URL */
  spec_url?: string;
  /** mcp: remote MCP endpoint */
  mcp_url?: string;
};

export type DirectoryState = "default" | "loading" | "empty";

// ── brand / category line icons ──
/** P2 batch 2: Lucide icon per name (see `components/icon-refresh.tsx`). */
const PD_ICONS: Readonly<Record<string, LucideIcon>> = {
  git: LucideGitBranch,
  doc: LucideFileText,
  hash: LucideHash,
  card: LucideCreditCard,
  layers: LucideLayers,
  cloud: LucideCloud,
  plug: LucidePlug,
  lock: LucideLock,
  braces: LucideBraces,
  slash: LucideBan,
  arrowUR: LucideArrowUpRight,
  shield: LucideShieldCheck,
  search: LucideSearch,
  x: LucideX,
};

function PDIcon({ name, s = 18, style }: { name: string; s?: number; style?: React.CSSProperties }) {
  const Next = PD_ICONS[name];
  return Next ? renderRefreshSvg(Next, s, s, { "aria-hidden": true, style }) : null;
}

// ── pills ─────────────────────────────────────────────────────────────────────
function KindPill({ kind }: { kind: PresetKind }) {
  const mcp = kind === "mcp";
  return (
    <span className={`bh-mono legacy-preset-pill legacy-preset-kind ${mcp ? "is-mcp" : "is-rest"}`}>
      <PDIcon name={mcp ? "plug" : "braces"} s={10} />
      {mcp ? "MCP" : "REST"}
    </span>
  );
}

function AuthPill({ auth }: { auth: PresetAuth }) {
  const t = useTranslations("create_tool");
  if (auth === "oauth") {
    return (
      <span className="legacy-preset-pill legacy-preset-auth is-oauth">
        <PDIcon name="shield" s={10} />
        {t("preset_pill_auth_oauth")}
      </span>
    );
  }
  if (auth === "none") {
    return (
      <span className="legacy-preset-pill legacy-preset-auth is-none">
        <PDIcon name="slash" s={10} />
        {t("preset_pill_auth_none")}
      </span>
    );
  }
  return (
    <span className="legacy-preset-pill legacy-preset-auth is-key">
      <PDIcon name="lock" s={10} />
      {t("preset_pill_auth_key")}
    </span>
  );
}

// ── connector card ─────────────────────────────────────────────────────────────
function ConnectorCard({ p, onPick }: { p: ConnectorPreset; onPick: (p: ConnectorPreset) => void }) {
  const t = useTranslations("create_tool");
  const [hover, setHover] = useState(false);
  const oauth = p.auth === "oauth";
  return (
    <button
      type="button"
      onClick={() => onPick(p)}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={t("preset_card_title", { name: p.name })}
      style={{
        position: "relative", textAlign: "left", display: "flex", flexDirection: "column", gap: 7,
        padding: "11px 11px 10px", borderRadius: "var(--r-2)", cursor: "pointer",
        background: hover ? "var(--c-surface)" : "var(--c-surface-2)",
        border: `1px solid ${hover ? "var(--c-accent)" : "var(--c-line)"}`,
        boxShadow: hover ? "var(--shadow-1)" : "none", transition: "all .14s var(--ease-v2)",
        transform: hover ? "translateY(-1px)" : "none", font: "inherit",
      }}
    >
      {oauth ? (
        <span
          title={t("preset_oauth_cue")}
          style={{
            position: "absolute", top: 8, right: 8, width: 17, height: 17, borderRadius: "50%",
            background: "var(--c-info-soft)", border: "1px solid var(--c-info-line)", color: "var(--c-info-ink)",
            display: "grid", placeItems: "center",
          }}
        >
          <PDIcon name="arrowUR" s={9} />
        </span>
      ) : null}
      <div style={{ display: "flex", alignItems: "center", gap: 8, paddingRight: oauth ? 16 : 0 }}>
        <span
          style={{
            width: 28, height: 28, flex: "0 0 28px", borderRadius: 8, background: "var(--c-surface)",
            border: "1px solid var(--c-line)", color: "var(--c-ink-2)", display: "grid", placeItems: "center",
          }}
        >
          <PDIcon name={p.icon} s={16} />
        </span>
        <span style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--c-ink)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {p.name}
        </span>
      </div>
      {p.blurb ? (
        <span style={{ fontSize: "0.6875rem", lineHeight: 1.4, color: "var(--c-ink-4)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {p.blurb}
        </span>
      ) : null}
      <span style={{ display: "flex", flexWrap: "wrap", gap: 5, marginTop: 1 }}>
        <KindPill kind={p.kind} />
        <AuthPill auth={p.auth} />
      </span>
    </button>
  );
}

function SkeletonCard() {
  const bar = (w: number | string, h: number) => (
    <span style={{ display: "block", width: w, height: h, borderRadius: 5, background: "var(--c-surface-2)" }} />
  );
  return (
    <div
      className="pd-shimmer"
      style={{
        display: "flex", flexDirection: "column", gap: 8, padding: "11px 11px 10px", borderRadius: "var(--r-2)",
        background: "var(--c-surface-2)", border: "1px solid var(--c-line)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ width: 28, height: 28, borderRadius: 8, background: "var(--c-surface-2)", flex: "0 0 28px" }} />
        {bar("56%", 11)}
      </div>
      {bar("82%", 9)}
      <div style={{ display: "flex", gap: 5 }}>
        {bar(38, 15)}
        {bar(34, 15)}
      </div>
    </div>
  );
}

const GRID: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 9 };

/**
 * Preset connector directory. Empty presets return null (parent decides whether to render separator/directory).
 * onPick(preset) delegates source field seeding + existing preview/oauth flow to the parent.
 */
export default function PresetDirectory({
  presets, onPick, state = "default",
}: {
  presets: ConnectorPreset[];
  onPick: (p: ConnectorPreset) => void;
  state?: DirectoryState;
}) {
  const t = useTranslations("create_tool");
  const [query, setQuery] = useState("");
  if (state !== "loading" && state !== "empty" && presets.length === 0) return null;

  const showSearch = presets.length > 6;
  const q = query.trim().toLowerCase();
  const filtered = q
    ? presets.filter((p) => `${p.name}${p.kind}${p.auth}${p.blurb ?? ""}`.toLowerCase().includes(q))
    : presets;

  let body: React.ReactNode;
  if (state === "loading") {
    body = (
      <div style={GRID}>
        {Array.from({ length: 6 }, (_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>
    );
  } else if (state === "empty") {
    body = (
      <div
        style={{
          display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center", gap: 8,
          padding: "22px 16px", borderRadius: "var(--r-2)", background: "var(--c-surface-2)", border: "1px dashed var(--c-line)",
        }}
      >
        <span
          style={{
            width: 34, height: 34, borderRadius: 10, background: "var(--c-surface)", border: "1px solid var(--c-line)",
            color: "var(--c-ink-4)", display: "grid", placeItems: "center",
          }}
        >
          <PDIcon name="plug" s={17} />
        </span>
        <div style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-3)" }}>{t("preset_empty_title")}</div>
        <div style={{ fontSize: "0.71875rem", lineHeight: 1.5, color: "var(--c-ink-4)", maxWidth: 300 }}>{t("preset_empty_body")}</div>
      </div>
    );
  } else if (filtered.length === 0) {
    body = (
      <div style={{ fontSize: "0.75rem", color: "var(--c-ink-4)", padding: "14px 4px", textAlign: "center" }}>
        {t("preset_no_match", { query })}
      </div>
    );
  } else {
    body = (
      <div style={GRID}>
        {filtered.map((p) => (
          <ConnectorCard key={p.id} p={p} onPick={onPick} />
        ))}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
        <span style={{ fontSize: "0.78125rem", fontWeight: 600, color: "var(--c-ink-3)" }}>{t("preset_header")}</span>
        <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-4)" }}>· {t("preset_header_sub")}</span>
        <div style={{ flex: 1 }} />
        {state === "default" ? (
          <span className="bh-mono" style={{ fontSize: "0.625rem", color: "var(--c-ink-4)" }}>{t("preset_available", { n: filtered.length })}</span>
        ) : null}
      </div>
      {showSearch ? (
        <div
          style={{
            display: "flex", alignItems: "center", gap: 8, border: "1px solid var(--c-line)", borderRadius: "var(--r-1)",
            padding: "0 10px", background: "var(--c-surface-2)",
          }}
        >
          <PDIcon name="search" s={14} style={{ color: "var(--c-ink-4)", flex: "0 0 auto" }} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("preset_search")}
            style={{ flex: 1, border: "none", outline: "none", background: "transparent", padding: "8px 0", fontSize: "0.78125rem", color: "var(--c-ink)" }}
          />
          {query ? (
            <button
              type="button"
              onClick={() => setQuery("")}
              aria-label={t("preset_clear")}
              style={{ border: "none", background: "transparent", color: "var(--c-ink-4)", cursor: "pointer", padding: 2, display: "grid", placeItems: "center" }}
            >
              <PDIcon name="x" s={14} />
            </button>
          ) : null}
        </div>
      ) : null}
      {body}
    </div>
  );
}
