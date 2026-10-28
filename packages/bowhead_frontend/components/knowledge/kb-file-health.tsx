"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { getKbFileHealth, restoreKbSource, rebuildKbSource, type KbHealthObservation } from "@/lib/kb-api";

export function KbFileHealth({ fileId, version, allowActions = false }: { fileId: string; version: number; allowActions?: boolean }) {
  const capabilities = useOptionalAuthSession()?.me?.capabilities;
  const enabled = capabilities?.kb_health_observation === true;
  const guarded = capabilities?.kb_use_guard === true;
  const t = useTranslations("knowledge_base.health");
  const [health, setHealth] = useState<KbHealthObservation | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const request = useRef(0);
  const upload = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    request.current++;
    setHealth(null);
    setNotice(null);
    setFailed(false);
    setBusy(false);
    return () => {
      request.current++;
    };
  }, [fileId, version, enabled]);

  if (!enabled) return null;

  const check = async () => {
    const id = ++request.current;
    setBusy(true);
    setFailed(false);
    setNotice(null);
    setHealth(null);
    try {
      const result = await getKbFileHealth(fileId, guarded);
      if (request.current === id) {
        if (result.file_id === fileId && (guarded || result.file_version === version)) setHealth(result);
        else setFailed(true);
      }
    } catch {
      if (request.current === id) setFailed(true);
    } finally {
      if (request.current === id) setBusy(false);
    }
  };

  const restore = async (file: File) => {
    const id = ++request.current;
    setBusy(true);
    setNotice(null);
    setFailed(false);
    try {
      const result = await restoreKbSource(fileId, file);
      if (request.current !== id) return;
      if (result.outcome === "same_content_restored") await check();
      else setNotice("different_content");
    } catch {
      if (request.current === id) setFailed(true);
    } finally {
      if (request.current === id) setBusy(false);
    }
  };

  const rebuild = async () => {
    const id = ++request.current;
    setBusy(true);
    setFailed(false);
    try {
      await rebuildKbSource(fileId);
      if (request.current === id) { setNotice("rebuild_queued"); setHealth(null); }
    } catch {
      if (request.current === id) setFailed(true);
    } finally {
      if (request.current === id) setBusy(false);
    }
  };

  const indexText = health?.index.reason === "legacy_chunks_readable"
    ? "index_readable"
    : health?.index.state === "processing"
      ? "index_processing"
      : health?.index.state === "missing_or_corrupt"
        ? "index_problem"
        : "index_unknown";

  return (
    <section className="dr-chunks kb-health" data-testid="kb-file-health" aria-busy={busy}>
      <div className="sub-t">{t("title")}</div>
      <p className="why">{t(guarded ? "guard_intro" : "intro")}</p>
      <button type="button" className="kb-btn kb-btn-secondary" disabled={busy} onClick={check}>
        {t(busy ? "checking" : health ? "check_again" : "check")}
      </button>
      {allowActions && health?.primary_action === "restore" && <>
        <input ref={upload} type="file" hidden aria-label={t("restore")} onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void restore(file); }} />
        <button type="button" className="kb-btn kb-btn-primary" disabled={busy} onClick={() => upload.current?.click()}>{t("restore")}</button>
      </>}
      {allowActions && health?.primary_action === "rebuild" && <p className="why">{t("guard_cost")}</p>}
      {allowActions && health?.primary_action === "rebuild" && <button type="button" className="kb-btn kb-btn-primary" disabled={busy} onClick={rebuild}>{t("rebuild")}</button>}
      <div role="status" aria-live="polite">
        {notice && <p className="why">{t(notice)}</p>}
        {failed && <p className="why">{t("failed")}</p>}
        {health && <>
          <dl>
            <dt>{t("source")}</dt><dd>{t(`source_${health.source_health}`)}</dd>
            <dt>{t("index")}</dt><dd>{t(indexText)}</dd>
            <dt>{t("binding")}</dt><dd>{t(health.binding_state === "verified" ? "binding_verified" : "binding_unknown")}</dd>
          </dl>
          {health.use_state && <p className="why">{t(`use_${health.use_state}`)}</p>}
          <p className="why">{t(guarded ? "evidence_guarded" : "evidence_unknown")}</p>
          <p className="why">{t("checked_at", { time: new Date(health.checked_at).toLocaleString() })}</p>
        </>}
      </div>
    </section>
  );
}
