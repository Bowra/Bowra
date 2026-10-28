"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  forkAgent,
  previewAgentFork,
  type AgentForkPreview,
  type AgentForkResult,
} from "@/lib/agents-api";
import { DialogShell } from "@/components/ui/dialog";
import { ApiError } from "@/lib/api-client";
import { randomUuid } from "@/lib/random-id";
import { CharIcon } from "@/components/char-icon";

interface AgentForkDialogProps {
  agentId: string;
  onClose: () => void;
  onComplete: (result: AgentForkResult) => void;
}

function idempotencyKey(): string {
  return `agent-fork-${randomUuid()}`;
}

export function AgentForkDialog({ agentId, onClose, onComplete }: AgentForkDialogProps) {
  const t = useTranslations("agents_marketplace");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [preview, setPreview] = useState<AgentForkPreview | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [state, setState] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [requestKey] = useState(idempotencyKey);
  useEffect(() => {
    let active = true;
    void previewAgentFork(agentId)
      .then((value) => {
        if (!active) return;
        setPreview(value);
        setDisplayName(value.suggested_display_name);
        setState("ready");
      })
      .catch(() => {
        if (!active) return;
        setError(t("fork_preview_failed"));
        setState("error");
      });
    return () => { active = false; };
  }, [agentId, t]);

  useEffect(() => {
    if (state !== "ready") return;
    const focusFrame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(focusFrame);
  }, [state]);

  const submit = async () => {
    if (!preview || !displayName.trim() || state === "saving") return;
    setState("saving");
    setError(null);
    try {
      const result = await forkAgent(agentId, {
        sourceRevisionRef: preview.source.revision_ref,
        displayName: displayName.trim(),
        idempotencyKey: requestKey,
      });
      onComplete(result);
    } catch (cause) {
      const code = cause instanceof ApiError ? cause.code : null;
      setError(
        code === "display_name_unavailable"
          ? t("fork_name_unavailable")
          : code === "agent_fork_source_changed" || code === "agent_fork_policy_changed"
            ? t("fork_source_changed")
            : t("fork_create_failed"),
      );
      setState("ready");
    }
  };

  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      busy={state === "saving"}
      mount="inline"
      labelledBy="agent-fork-title"
      backdropClassName="am-fork-scrim"
      panelClassName="am-fork-dialog"
      panelProps={{ "aria-describedby": preview ? "agent-fork-description" : undefined }}
    >
      <header>
        <div>
          <span>{t("fork_eyebrow")}</span>
          <h2 id="agent-fork-title">{t("fork_title")}</h2>
        </div>
        <button type="button" aria-label={t("close")} disabled={state === "saving"} onClick={onClose}><CharIcon ch="×" aria-hidden /></button>
      </header>

      {state === "loading" ? <div className="am-fork-loading" aria-live="polite">{t("fork_loading")}</div> : null}
      {state === "error" && !preview ? (
        <div className="am-fork-error" role="alert">
          <p>{error}</p>
          <button type="button" onClick={onClose}>{t("close")}</button>
        </div>
      ) : null}
      {preview ? (
        <>
          <p id="agent-fork-description" className="am-fork-intro">
            {t("fork_intro", { name: preview.source.display_name })}
          </p>
          <section className="am-fork-source">
            <div><strong>{preview.source.display_name}</strong><small>{t("fork_exact_version", { version: preview.source.revision_ref.slice(-8) })}</small></div>
            <span>{t("fork_private_badge")}</span>
          </section>
          <section className="am-fork-copy-grid" aria-label={t("fork_copy_summary")}>
            <div><b>{preview.copied.tool_count}</b><span>{t("fork_tools")}</span></div>
            <div><b>{preview.copied.skill_count}</b><span>{t("fork_skills")}</span></div>
            <div><b>1</b><span>{t("fork_prompt")}</span></div>
            <div><b>{preview.copied.output_contract ? <CharIcon ch="✓" /> : "—"}</b><span>{t("fork_output_contract")}</span></div>
          </section>
          {preview.dependency_gaps.length ? (
            <section className="am-fork-gaps" role="status">
              <strong>{t("fork_gaps_title", { count: preview.dependency_gaps.length })}</strong>
              <p>{t("fork_gaps_body")}</p>
              <ul>{preview.dependency_gaps.map((gap) => (
                <li key={`${gap.kind}:${gap.source_ref_digest}`}>{t(`fork_gap_${gap.kind}` as never)}</li>
              ))}</ul>
            </section>
          ) : (
            <section className="am-fork-ready"><strong><CharIcon ch="✓" /> {t("fork_dependencies_ready")}</strong><p>{t("fork_dependencies_ready_body")}</p></section>
          )}
          <label className="am-fork-name">
            <span id="agent-fork-name-label">{t("fork_name_label")}</span>
            <input
              ref={inputRef}
              aria-labelledby="agent-fork-name-label"
              aria-describedby="agent-fork-name-help"
              value={displayName}
              maxLength={40}
              disabled={state === "saving"}
              onChange={(event) => setDisplayName(event.target.value)}
            />
            <small id="agent-fork-name-help">{t("fork_name_help")}</small>
          </label>
          <details className="am-fork-excluded">
            <summary>{t("fork_not_copied")}</summary>
            <ul>{preview.excluded.map((item) => <li key={item}>{t(`fork_excluded_${item}` as never)}</li>)}</ul>
          </details>
          {error ? <p className="am-fork-inline-error" role="alert">{error}</p> : null}
          <footer>
            <button type="button" disabled={state === "saving"} onClick={onClose}>{t("cancel")}</button>
            <button
              type="button"
              className="am-action-primary"
              disabled={state === "saving" || !displayName.trim()}
              onClick={() => void submit()}
            >
              {state === "saving" ? t("fork_creating") : t("fork_create_private")}
            </button>
          </footer>
        </>
      ) : null}
    </DialogShell>
  );
}
