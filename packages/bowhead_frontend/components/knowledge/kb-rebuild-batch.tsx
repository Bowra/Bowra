"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { controlKbRebuild, fetchKbRebuild, latestKbRebuild, previewKbRebuild, type KbRebuildPreview, type KbRebuildProgress } from "@/lib/kb-api";

/** A single quiet status area; frozen selection is shown before paid processing. */
export function KbRebuildBatch({ scopeKey, selected, canManage, onChanged }: { scopeKey: string; selected: string[]; canManage: boolean; onChanged: () => unknown }) {
  const enabled = useOptionalAuthSession()?.me?.capabilities?.kb_use_guard === true;
  const t = useTranslations("knowledge_base.rebuild_batch");
  const [jobId, setJobId] = useState<string | null>(null);
  const [pollRevision, setPollRevision] = useState(0);
  const [job, setJob] = useState<KbRebuildProgress | null>(null);
  const [preview, setPreview] = useState<{ ids: string[]; facts: KbRebuildPreview } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const changed = useRef(onChanged);
  changed.current = onChanged;
  useEffect(() => {
    let stopped = false;
    if (enabled) void latestKbRebuild().then(id => { if (!stopped) setJobId(current => current ?? id); }).catch(() => { if (!stopped) setError(true); });
    return () => { stopped = true; };
  }, [enabled, scopeKey]);
  useEffect(() => {
    if (!enabled || !jobId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await fetchKbRebuild(jobId);
        if (stopped) return;
        setJob(next);
        setError(false);
        if (next.state === "running" || next.state === "cancelling") timer = setTimeout(poll, 5000);
        else changed.current();
      } catch {
        if (!stopped) { setError(true); setJob(null); }
      }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [enabled, jobId, pollRevision]);
  if (!enabled) return null;
  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError(false);
    try { await action(); } catch { setError(true); } finally { setBusy(false); }
  };
  const start = async () => {
    if (!preview) return;
    const result = await previewKbRebuild(preview.ids, true);
    if (result.job_id) {
      setJobId(result.job_id); setJob(null); setPreview(null);
    }
  };
  const control = async (action: "resume" | "cancel") => {
    if (!jobId) return;
    await controlKbRebuild(jobId, action);
    const next = await fetchKbRebuild(jobId);
    setJob(next);
    // Re-arm polling after an explicit resume without reusing a settled job.
    if (action === "resume") setPollRevision(n => n + 1);
    changed.current();
  };
  return <section className="kb-rebuild-batch" aria-label={t("title")} data-testid="kb-rebuild-batch">
    {selected.length > 0 && !preview && job?.state !== "running" && job?.state !== "cancelling" && <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" disabled={busy || selected.length > 20 || !canManage} title={!canManage ? t("permission") : undefined} onClick={() => void perform(async () => { const ids = [...selected]; setPreview({ ids, facts: await previewKbRebuild(ids) }); })}>{t("title")}</button>}
    {selected.length > 20 && <p>{t("limit")}</p>}
    {preview && <div>
      <p>{t("preview", { count: preview.facts.files, mb: (preview.facts.size_bytes / 1024 / 1024).toFixed(1) })}</p>
      <p>{t("cost")}</p>
      <button type="button" className="kb-btn kb-btn-primary kb-btn-sm" disabled={busy} onClick={() => void perform(start)}>{t("confirm")}</button>
      <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" disabled={busy} onClick={() => setPreview(null)}>{t("back")}</button>
    </div>}
    {job && <div role="status">
      <span>{t(`state_${job.state}`)} · {job.processed}/{job.total}</span>
      {(job.failed + job.changed > 0) && <span> · {t("not_completed", { count: job.failed + job.changed })}</span>}
      {job.state === "paused" && <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" disabled={busy} onClick={() => void perform(() => control("resume"))}>{t("resume")}</button>}
      {(job.state === "running" || job.state === "paused") && <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" disabled={busy} onClick={() => void perform(() => control("cancel"))}>{t("stop")}</button>}
      {job.state === "paused" && <p>{t("paused_detail")}</p>}
    </div>}
    {error && <p role="status">{t("error")}</p>}
  </section>;
}
