// components/share/report-unavailable.tsx — unavailable-report placeholder (content-permissions S7/wave2).
// Rendered when GET /canvas/by-session 403s with {code: report_unavailable} — a shared
// report the viewer can't access. With access requests on, the shared request panel shows the
// server-backed request state; otherwise an informational no-access state. Friendly, not an error page.

"use client";

import { useTranslations } from "next-intl";

import { AccessRequestPanel } from "@/components/access/access-request-panel";

import { ShBtn, ShIcon, T } from "./share-ui";

function PlaceholderArt({ scale = 1 }: { scale?: number }) {
  const s = (n: number) => n * scale;
  return (
    <div style={{ position: "relative", width: s(104), height: s(104), marginBottom: s(22) }}>
      <div style={{ position: "absolute", inset: 0, borderRadius: "50%", background: T.s2 }} />
      <div style={{ position: "absolute", inset: s(-14), borderRadius: "50%", border: `1.5px dashed ${T.line}` }} />
      <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: T.ink4 }}>
        <ShIcon name="doc" size={s(40)} />
      </div>
      <div style={{
        position: "absolute", right: s(-4), bottom: s(-2), width: s(34), height: s(34), borderRadius: "50%",
        background: T.surface, border: `1px solid ${T.line}`, boxShadow: "0 2px 8px rgba(22,19,16,0.08)",
        display: "grid", placeItems: "center", color: T.accent,
      }}><ShIcon name="lock" size={s(15)} /></div>
    </div>
  );
}

export function ReportUnavailable({
  mobile, sessionId, onBack, canRequest = true, onOpen,
}: {
  mobile?: boolean;
  sessionId?: string;
  onBack?: () => void;
  /** flag off → no request panel, only the info state. */
  canRequest?: boolean;
  /** Approved: open the report (defaults to reloading the current link). */
  onOpen?: () => void;
}) {
  const t = useTranslations("share");

  if (canRequest && sessionId) {
    return (
      <div className="acr-report-page" data-mobile={mobile ? "1" : undefined}>
        <AccessRequestPanel
          kind="report"
          resourceId={sessionId}
          onBack={onBack}
          onOpen={onOpen ?? (() => window.location.reload())}
        />
      </div>
    );
  }

  return (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      textAlign: "center", padding: mobile ? "0 36px 70px" : "0 0 60px", minHeight: mobile ? "60vh" : 420,
      background: T.bg, color: T.ink, fontFamily: "var(--font-v2-sans)",
    }}>
      <PlaceholderArt scale={mobile ? 0.92 : 1} />
      <div style={{ fontFamily: "var(--font-v2-serif)", fontSize: mobile ? "1.3125rem" : "1.5625rem", fontWeight: 600, color: T.ink, letterSpacing: 0.2 }}>
        {t("unavailable_title_no_access")}
      </div>
      <div style={{ fontSize: mobile ? "0.84375rem" : "0.875rem", color: T.ink3, lineHeight: 1.7, marginTop: 10, maxWidth: 360 }}>
        {t("unavailable_sub_no_access")}
      </div>
      <div style={{ marginTop: 26, display: "flex", gap: 10, flexWrap: "wrap", justifyContent: "center" }}>
        <ShBtn kind="primary" h={mobile ? 44 : 38} fs={13.5} onClick={onBack}
               style={mobile ? { padding: "0 30px" } : undefined}>
          <ShIcon name="back" size={13} />{t("back")}
        </ShBtn>
      </div>
    </div>
  );
}
