"use client";

/**
 * The Settings tab of one knowledge base (FEATURE_KNOWLEDGE_BASES, PRD §4.2): name and description, owner and
 * transfer, and the danger zone "Delete knowledge base…". Only managers see the tab.
 *
 * - Saving sends the revision the form was read from; a revision conflict says so and offers a reload.
 * - Default knowledge bases keep a fixed name (technical design C6): the panel shows it with the reason.
 * - Ownership goes only to someone who already manages the knowledge base (an automatic manager or a person on the
 *   list at "Can manage"); defaults and My knowledge bases have no owner to transfer (`kb.can.transfer`).
 * - Deleting needs the owner or an automatic manager (`kb.can.delete`); the page opens the confirmation.
 */

import { useEffect, useId, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { SectionHeading } from "@/components/ui/section-heading";
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { kbFileError } from "@/lib/kb-file-error";
import { isKnowledgeBaseOwner } from "@/lib/knowledge-base-display";
import {
  getKnowledgeBaseMembers,
  notifyKnowledgeBasesChanged,
  transferKnowledgeBase,
  updateKnowledgeBase,
  type KnowledgeBaseDTO,
  type KnowledgeBaseMembers,
  type KnowledgeBasePatch,
} from "@/lib/knowledge-bases-api";
import { notify } from "@/lib/use-toast";

import { useKnowledgeBaseNamer } from "./use-knowledge-bases";

/** Limits of `PATCH /kb/knowledge-bases/{id}` (gateway `KnowledgeBasePatch`). */
const NAME_MAX = 120;
const DESCRIPTION_MAX = 1000;

export interface KbSettingsPanelProps {
  kb: KnowledgeBaseDTO;
  /** A save or transfer returned the knowledge base as it is now. */
  onUpdated: (kb: KnowledgeBaseDTO) => void;
  /** Read the knowledge base again (after a revision conflict). */
  onReload: () => void;
  /** Open the delete confirmation (owned by the page). */
  onRequestDelete: () => void;
  /** Go to the Members tab (to give someone "Can manage" before a transfer). */
  onOpenMembers?: () => void;
}

export interface TransferCandidate {
  user_id: string;
  name: string | null;
  reason: "space_admin" | "team_admin" | "member";
}

/** People who already manage the knowledge base, other than its owner: automatic managers, then listed managers. */
export function transferCandidates(members: KnowledgeBaseMembers, ownerId: string | null): TransferCandidate[] {
  const out = new Map<string, TransferCandidate>();
  for (const manager of members.automatic_managers) {
    if (manager.user_id === ownerId || out.has(manager.user_id)) continue;
    out.set(manager.user_id, { user_id: manager.user_id, name: manager.name, reason: manager.reason });
  }
  for (const entry of members.entries) {
    if (entry.principal_type !== "user" || entry.level !== "manage" || entry.state !== "active") continue;
    if (entry.principal_id === ownerId || out.has(entry.principal_id)) continue;
    out.set(entry.principal_id, { user_id: entry.principal_id, name: entry.label, reason: "member" });
  }
  return [...out.values()];
}

type SaveMessage = (error: unknown) => string;

function useSaveMessage(fallbackKey: "save_failed" | "transfer_failed" | "candidates_failed"): SaveMessage {
  const ts = useTranslations("knowledge_bases.settings");
  const tKb = useTranslations("knowledge_base");
  return (error) => {
    // kbFileError knows every knowledge_base_* code; an unknown failure gets this action's own sentence.
    const key = kbFileError(error, "load");
    return key === "errors.load_failed" ? ts(fallbackKey) : tKb(key);
  };
}

function isRevisionConflict(error: unknown): boolean {
  return error instanceof ApiError && error.code === "knowledge_base_revision_conflict";
}

export function KbSettingsPanel({ kb, onUpdated, onReload, onRequestDelete, onOpenMembers }: KbSettingsPanelProps) {
  const ts = useTranslations("knowledge_bases.settings");
  const namer = useKnowledgeBaseNamer();
  const userId = useOptionalAuthSession()?.me?.id ?? null;
  const [transferring, setTransferring] = useState(false);
  const ownerName = kb.owner?.name || ts("person_unknown");
  const ownedByMe = isKnowledgeBaseOwner(kb, userId);
  const fixedWho = kb.home === "space" ? "space_admin" : kb.home === "team" ? "team_admin" : "other";

  return (
    <div className="kbp-settings" data-testid="kb-settings">
      <section className="kbp-settings__section" aria-labelledby="kbp-settings-details">
        <SectionHeading id="kbp-settings-details" title={ts("details_title")} />
        {kb.is_default ? (
          // C6: a default's name comes from its home (My / the Space / the Team) and cannot change.
          <div className="kbp-settings__fixed" data-testid="kb-settings-fixed">
            <div className="kbp-settings__row">
              <span className="kbp-settings__label">{ts("name_label")}</span>
              <span className="kbp-settings__value">{namer(kb)}</span>
            </div>
            <p className="kbp-settings__hint">{ts("default_fixed_reason")}</p>
          </div>
        ) : (
          <KbDetailsForm kb={kb} onUpdated={onUpdated} onReload={onReload} />
        )}
      </section>

      <section className="kbp-settings__section" aria-labelledby="kbp-settings-owner">
        <SectionHeading id="kbp-settings-owner" title={ts("owner_title")} />
        {kb.home === "personal" ? (
          <p className="kbp-settings__hint" data-testid="kb-settings-owner-personal">{ts("owner_personal")}</p>
        ) : kb.is_default ? (
          <p className="kbp-settings__hint" data-testid="kb-settings-owner-default">{ts("owner_default", { who: fixedWho })}</p>
        ) : (
          <div className="kbp-settings__owner">
            <div className="kbp-settings__row">
              <span className="kbp-settings__value" data-testid="kb-settings-owner-name">
                {ownedByMe ? ts("name_you", { name: ownerName }) : ownerName}
              </span>
              {kb.can.transfer ? (
                <Button size="sm" data-testid="kb-settings-transfer" onClick={() => setTransferring(true)}>{ts("transfer")}</Button>
              ) : null}
            </div>
            {kb.can.transfer ? null : <p className="kbp-settings__hint">{ts("transfer_not_allowed")}</p>}
          </div>
        )}
      </section>

      {kb.is_default ? (
        <section className="kbp-settings__section" aria-labelledby="kbp-settings-danger">
          <SectionHeading id="kbp-settings-danger" title={ts("danger_title")} />
          <p className="kbp-settings__hint">{ts("delete_default")}</p>
        </section>
      ) : (
        <section className="kbp-settings__section kbp-settings__danger" aria-labelledby="kbp-settings-danger">
          <SectionHeading id="kbp-settings-danger" title={ts("danger_title")} />
          <div className="kbp-settings__row">
            <p className="kbp-settings__hint">
              {kb.can.delete ? ts("delete_body", { n: kb.file_count }) : ts("delete_not_allowed")}
            </p>
            {kb.can.delete ? (
              <Button variant="danger" size="sm" data-testid="kb-settings-delete" onClick={onRequestDelete}>
                {ts("delete_label")}
              </Button>
            ) : null}
          </div>
        </section>
      )}

      {transferring ? (
        <KbTransferDialog
          kb={kb}
          ownedByMe={ownedByMe}
          onClose={() => setTransferring(false)}
          onOpenMembers={onOpenMembers ? () => { setTransferring(false); onOpenMembers(); } : undefined}
          onTransferred={(next) => { setTransferring(false); onUpdated(next); }}
        />
      ) : null}
    </div>
  );
}

function KbDetailsForm({ kb, onUpdated, onReload }: Pick<KbSettingsPanelProps, "kb" | "onUpdated" | "onReload">) {
  const ts = useTranslations("knowledge_bases.settings");
  const tc = useTranslations("knowledge_bases.common");
  const saveMessage = useSaveMessage("save_failed");
  const nameId = useId();
  const descriptionId = useId();
  // What the form was read from; the save sends this revision so a change made meanwhile is never overwritten.
  const [base, setBase] = useState({ name: kb.name ?? "", description: kb.description ?? "", revision: kb.revision });
  const [name, setName] = useState(base.name);
  const [description, setDescription] = useState(base.description);
  const [nameError, setNameError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const dirty = name !== base.name || description !== base.description;

  // A newer revision (saved elsewhere, then reloaded) resets an untouched form, or the form after a conflict.
  useEffect(() => {
    if (kb.revision === base.revision) return;
    if (dirty && !conflict) return;
    const next = { name: kb.name ?? "", description: kb.description ?? "", revision: kb.revision };
    setBase(next);
    setName(next.name);
    setDescription(next.description);
    setConflict(false);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kb.revision, kb.name, kb.description]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const cleanName = name.trim();
    if (!cleanName) { setNameError(ts("name_required")); return; }
    setNameError(null);
    const body: KnowledgeBasePatch = { revision: base.revision };
    if (cleanName !== base.name) body.name = cleanName;
    const cleanDescription = description.trim();
    if (cleanDescription !== base.description.trim()) body.description = cleanDescription || null;
    setSaving(true);
    setError(null);
    setConflict(false);
    try {
      const next = await updateKnowledgeBase(kb.id, body);
      const nextBase = { name: next.name ?? "", description: next.description ?? "", revision: next.revision };
      setBase(nextBase);
      setName(nextBase.name);
      setDescription(nextBase.description);
      onUpdated(next);
      notifyKnowledgeBasesChanged();
      notify("success", ts("saved"));
    } catch (caught) {
      setConflict(isRevisionConflict(caught));
      setError(saveMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="kbp-settings__form" onSubmit={save} noValidate>
      <Field id={nameId} label={ts("name_label")} error={nameError}>
        <input
          id={nameId}
          className="settings-input"
          value={name}
          maxLength={NAME_MAX}
          aria-invalid={nameError ? true : undefined}
          aria-describedby={fieldDescribedBy(nameId, { error: Boolean(nameError) })}
          onChange={(event) => setName(event.target.value)}
        />
      </Field>
      <Field id={descriptionId} label={ts("description_label")} hint={ts("description_hint")}>
        <textarea
          id={descriptionId}
          className="settings-textarea"
          rows={3}
          value={description}
          maxLength={DESCRIPTION_MAX}
          aria-describedby={fieldDescribedBy(descriptionId, { hint: true })}
          onChange={(event) => setDescription(event.target.value)}
        />
      </Field>
      {error ? (
        <Notice tone="error" className="kbp-settings__notice">
          <span>{error}</span>
          {conflict ? (
            <Button size="sm" variant="secondary" data-testid="kb-settings-reload" onClick={onReload}>{ts("reload")}</Button>
          ) : null}
        </Notice>
      ) : null}
      <div className="kbp-settings__actions">
        <Button type="submit" variant="primary" loading={saving} disabled={saving || !dirty} data-testid="kb-settings-save">
          {saving ? tc("saving") : tc("save")}
        </Button>
      </div>
    </form>
  );
}

function KbTransferDialog({
  kb,
  ownedByMe,
  onClose,
  onOpenMembers,
  onTransferred,
}: {
  kb: KnowledgeBaseDTO;
  ownedByMe: boolean;
  onClose: () => void;
  onOpenMembers?: () => void;
  onTransferred: (kb: KnowledgeBaseDTO) => void;
}) {
  const ts = useTranslations("knowledge_bases.settings");
  const tc = useTranslations("knowledge_bases.common");
  const loadMessage = useSaveMessage("candidates_failed");
  const transferMessage = useSaveMessage("transfer_failed");
  const ruleId = useId();
  const [members, setMembers] = useState<KnowledgeBaseMembers | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [choice, setChoice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    getKnowledgeBaseMembers(kb.id).then(
      (next) => { if (!cancelled) setMembers(next); },
      (caught) => { if (!cancelled) setLoadError(loadMessage(caught)); },
    );
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kb.id, attempt]);

  const candidates = members ? transferCandidates(members, kb.owner?.user_id ?? null) : [];
  const chosen = candidates.find((candidate) => candidate.user_id === choice) ?? null;
  const personName = (candidate: TransferCandidate) => candidate.name || ts("person_unknown");

  const confirm = async () => {
    if (!chosen || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await transferKnowledgeBase(kb.id, { user_id: chosen.user_id, revision: kb.revision });
      notifyKnowledgeBasesChanged();
      notify("success", ts("transfer_done", { name: personName(chosen) }));
      onTransferred(next);
    } catch (caught) {
      setError(transferMessage(caught));
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={ts("transfer_title")}
      onClose={onClose}
      busy={busy}
      describedBy={ruleId}
      presentation="auto"
      footer={(
        <>
          <Button onClick={onClose} disabled={busy}>{tc("cancel")}</Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!chosen || busy}
            data-testid="kb-transfer-confirm"
            onClick={() => void confirm()}
          >
            {ts("transfer_confirm")}
          </Button>
        </>
      )}
    >
      <div className="kbp-transfer">
        <p id={ruleId} className="kbp-settings__hint">{ts("transfer_rule")}</p>
        {loadError ? (
          <Notice tone="error" className="kbp-settings__notice">
            <span>{loadError}</span>
            <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>{tc("retry")}</Button>
          </Notice>
        ) : members === null ? (
          <LoadingSkeleton rows={3} label={ts("candidates_loading")} />
        ) : candidates.length === 0 ? (
          <div className="kbp-transfer__empty" data-testid="kb-transfer-empty">
            <p>{ts("transfer_empty")}</p>
            {onOpenMembers ? <Button size="sm" onClick={onOpenMembers}>{ts("open_members")}</Button> : null}
          </div>
        ) : (
          <RadioGroup
            className="kbp-transfer__list"
            orientation="vertical"
            aria-label={ts("candidates_label")}
            value={choice}
            onValueChange={setChoice}
            busy={busy}
          >
            {candidates.map((candidate) => (
              <Radio key={candidate.user_id} value={candidate.user_id} className="kbp-transfer__option" aria-label={personName(candidate)}>
                <strong>{personName(candidate)}</strong>
                <span>{ts(`candidate_${candidate.reason}`)}</span>
              </Radio>
            ))}
          </RadioGroup>
        )}
        {ownedByMe ? <p className="kbp-settings__hint">{ts("transfer_keep")}</p> : null}
        {error ? <Notice tone="error" className="kbp-settings__notice">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}
