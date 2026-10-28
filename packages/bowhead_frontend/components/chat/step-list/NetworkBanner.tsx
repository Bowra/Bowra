"use client";

import { useTranslations } from "next-intl";
import { CharIcon } from "@/components/char-icon";


export type NetworkBannerStatus = "disconnected" | "reconnecting" | "restored" | "failed";

export interface NetworkBannerProps {
  status: NetworkBannerStatus;
  onDismiss?: () => void;
}

const STATUS_KEY: Record<NetworkBannerStatus, string> = {
  disconnected: "network_disconnected",
  reconnecting: "network_reconnecting",
  restored: "network_restored",
  failed: "network_failed",
};

export function NetworkBanner({ status, onDismiss }: NetworkBannerProps) {
  const tStep = useTranslations("step_list");
  const tCommon = useTranslations("common");
  const isRestored = status === "restored";
  // #9 (dingsiyan 2026-07-07): "failed" = terminal give-up (FE stopped reconnecting),
  // distinct from the actively-reconnecting "disconnected". Error styling + ✕, no
  // blinking dots (nothing is retrying); the copy tells the user to refresh — the
  // report is usually done server-side, so F5 hydrates it.
  const isFailed = status === "failed";
  const showDots = !isRestored && !isFailed;
  const color = isRestored ? "var(--c-ok)" : isFailed ? "var(--c-err)" : "var(--c-warn)";
  const bg = isRestored ? "var(--c-ok-soft)" : isFailed ? "var(--c-err-soft)" : "var(--c-warn-soft)";

  return (
    <div
      role="alert"
      aria-live="polite"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        background: bg,
        borderBottom: `1px solid ${color}`,
        padding: "8px 16px",
        fontFamily: "var(--f-sans)",
        fontSize: "0.8125rem",
        color: "var(--c-ink-2)",
        fontWeight: 500,
      }}
    >
      <span aria-hidden style={{ fontSize: "0.875rem" }}>
        <CharIcon ch={isRestored ? "✓" : isFailed ? "✕" : "⚠"} aria-hidden />
      </span>
      <span>{tStep(STATUS_KEY[status])}</span>
      {showDots ? (
        <span style={{ display: "inline-flex", gap: 3, marginLeft: 4 }} aria-hidden>
          <Dot delay={0} />
          <Dot delay={150} />
          <Dot delay={300} />
        </span>
      ) : null}
      <span style={{ flex: 1 }} />
      {onDismiss && isRestored ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={tCommon("close")}
          style={{
            background: "transparent",
            border: 0,
            padding: 4,
            cursor: "pointer",
            fontSize: "0.8125rem",
            color: "var(--c-ink-4)",
            lineHeight: 1,
          }}
        >
          <CharIcon ch="×" />
        </button>
      ) : null}
    </div>
  );
}

function Dot({ delay }: { delay: number }) {
  return (
    <span
      style={{
        width: 4,
        height: 4,
        borderRadius: "50%",
        background: "var(--c-warn)",
        opacity: 0.5,
        animation: "bh-blink 1s steps(2) infinite",
        animationDelay: `${delay}ms`,
      }}
    />
  );
}
