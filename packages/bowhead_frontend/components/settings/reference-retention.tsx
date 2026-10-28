"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ApiError } from "@/lib/api-client";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { teamRoleForUser } from "@/lib/settings-navigation";
import { getReferenceRetention, setReferenceRetention, type ReferenceRetentionPolicy } from "@/lib/reference-retention-api";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { SaveBar } from "@/components/ui/save-bar";

export function ReferenceRetentionSettings() {
  const { status, me } = useAuthSession();
  const t = useTranslations("settings.reference_retention");
  if (status !== "authenticated" || !me) return <Notice tone="muted">{t("loading")}</Notice>;
  const role = teamRoleForUser(me);
  if (role !== "owner" && role !== "team_admin") return <Notice tone="muted">{t("forbidden")}</Notice>;
  // Changing identity unmounts the old editor, aborts requests and drops its draft.
  return <RetentionEditor key={`${me.team_id}:${me.id}:${role}`} />;
}

function RetentionEditor() {
  const t = useTranslations("settings.reference_retention");
  const [policy, setPolicy] = useState<ReferenceRetentionPolicy | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<"load_failed" | "save_failed" | "conflict" | "forbidden" | null>(null);
  const [saved, setSaved] = useState(false);
  const [needsReload, setNeedsReload] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [reload, setReload] = useState(0);
  const request = useRef<AbortController | null>(null);
  const submitting = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    setSaved(false);
    setConfirming(false);
    void getReferenceRetention(controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      setPolicy(value);
      setDraft(String(value.days));
      setNeedsReload(false);
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) setError(caught instanceof ApiError && caught.status === 403 ? "forbidden" : "load_failed");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => { controller.abort(); request.current?.abort(); };
  }, [reload]);

  const days = /^[0-9]+$/.test(draft) ? Number(draft) : NaN;
  const valid = Number.isInteger(days) && days >= 1 && days <= 180;
  const canSave = Boolean(policy && valid && days !== policy.days && !loading && !busy && !needsReload && !error);

  const save = async () => {
    if (!canSave || !policy || submitting.current) return;
    submitting.current = true;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setSaved(false);
    try {
      const updated = await setReferenceRetention(days, policy.version, controller.signal);
      if (controller.signal.aborted) return;
      setPolicy(updated);
      setDraft(String(updated.days));
      setSaved(true);
    } catch (caught: unknown) {
      if (controller.signal.aborted) return;
      setError(caught instanceof ApiError && caught.status === 409 ? "conflict"
        : caught instanceof ApiError && caught.status === 403 ? "forbidden" : "save_failed");
      // A missing acknowledgement may already be committed. Read before retrying.
      setNeedsReload(true);
    } finally {
      submitting.current = false;
      if (!controller.signal.aborted) { setBusy(false); setConfirming(false); }
    }
  };

  const dirty = Boolean(policy && draft !== String(policy.days));
  const submit = () => {
    if (!canSave || !policy) return;
    if (days < policy.days) setConfirming(true);
    else void save();
  };
  const daysField = <Field id="reference-retention-days" label={t("days")} hint={t("range")} error={!valid ? t("invalid") : undefined}>
    <input id="reference-retention-days" className="settings-input ui-input legacy-input-retention" inputMode="numeric" autoComplete="off"
      value={draft} maxLength={3} disabled={loading || busy || needsReload || !!error}
      aria-invalid={!valid} aria-describedby={fieldDescribedBy("reference-retention-days", { hint: true, error: !valid })}
      onChange={(event) => { setDraft(event.target.value); setSaved(false); }} />
  </Field>;

  return <section>
    <h1 className="ui-page-title legacy-retention-title">{t("title")}</h1>
    <p className="ui-page-desc legacy-retention-desc">{t("description")}</p>
    <Notice tone="muted">{t("scope")}</Notice>
    {loading ? <LoadingSkeleton variant="form" rows={1} label={t("loading")} /> : null}
    {error ? <Notice tone="error">{t(error)}</Notice> : null}
    {saved ? <Notice tone="success">{t("saved")}</Notice> : null}
    {policy ? <form className="legacy-retention-form ui-group" onSubmit={(event) => {
      event.preventDefault();
      submit();
    }}>
      <p className="ui-group-desc">{t("current", { days: policy.days })}</p>
      {/* Days sit in a group card (label left, control right); the save bar saves. */}
      <div className="ui-group-card">{daysField}</div>
      <p className="legacy-retention-effect ui-group-footnote">{t("effect")}</p>
    </form> : null}
    {error ? <Button onClick={() => setReload((value) => value + 1)} disabled={busy || loading} className="legacy-retention-reload">{t("reload")}</Button> : null}
    <SaveBar dirty={dirty && !needsReload && !error} saving={busy} saveDisabled={!canSave}
      onSave={submit} onDiscard={() => { if (policy) setDraft(String(policy.days)); }} />
    {confirming && policy ? <ConfirmDialog title={t("confirm_title")}
      description={t("confirm_description", { before: policy.days, after: days })}
      confirmLabel={t("confirm")} cancelLabel={t("cancel")} busy={busy}
      onCancel={() => { if (!submitting.current) setConfirming(false); }} onConfirm={() => void save()} /> : null}
  </section>;
}
