"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { fetchLlmCatalog } from "@/lib/llm-config-api";
import { useAppRouter } from "@/lib/use-app-router";

type IndexState = "idle" | "running" | "failed";

export function KbIndexNotice({ canManage }: { canManage: boolean }) {
  const t = useTranslations("knowledge_base.index_status");
  const router = useAppRouter();
  const [state, setState] = useState<IndexState>("idle");

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const catalog = await fetchLlmCatalog();
        if (cancelled) return;
        const pending = catalog.kb?.reindex?.pending_embed;
        const next: IndexState = pending?.state === "failed"
          ? "failed"
          : pending && !["succeeded", "superseded", "cancelled"].includes(pending.state || "")
            ? "running"
            : "idle";
        setState(next);
        if (next === "running") timer = setTimeout(() => void refresh(), 3_000);
      } catch {
        // The file manager has its own error state. An unavailable catalog must not block it.
      }
    };
    void refresh();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, []);

  if (state === "idle") return null;
  return (
    <div className="kb-v2-banner" role={state === "failed" ? "alert" : "status"} data-testid="kb-index-notice">
      <span>{t(state)}</span>
      {state === "failed" ? canManage ? (
        <button type="button" className="kb-v2-bannerlink" onClick={() => router.push("/knowledge/index/")}>{t("review_retry")}</button>
      ) : <span className="muted">{t("needs_admin")}</span> : null}
    </div>
  );
}
