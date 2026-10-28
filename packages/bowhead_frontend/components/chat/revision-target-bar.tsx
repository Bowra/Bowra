"use client";

/**
 * Composer revision target bar (Chat Cards Spec §9): "Revising {title} · draft {n}" above the text, with a 28
 * remove button. The title is the draft's file name from the Canvas handoff — never a task ID — and opens that
 * draft in the Canvas. Handoffs saved before the name was carried keep the generic "Document draft".
 */

import { SquarePen, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { LucideGlyph } from "@/components/icon-refresh";
import type { ComposerDocumentRevision } from "@/lib/composer-document-revision";
import { requestDocumentCanvas } from "@/lib/document-canvas-requests";

export function RevisionTargetBar({
  revision,
  removeDisabled,
  onRemove,
}: {
  revision: ComposerDocumentRevision;
  removeDisabled: boolean;
  onRemove: () => void;
}) {
  const t = useTranslations("composer");
  const title = revision.display?.title ?? t("document_revision_draft");
  const version = revision.display?.version;
  const open = revision.display
    ? () => {
        requestDocumentCanvas({ sessionId: revision.target.session_id, jobId: revision.target.job_id });
      }
    : undefined;
  return (
    <div className="ui-card-target" data-testid="composer-document-revision">
      <LucideGlyph icon={SquarePen} size={14} aria-hidden="true" />
      <span className="ui-card-target__label">{t("document_revision_target")}</span>
      {open ? (
        <button
          type="button"
          className="ui-card-target__name ui-card-target__name--link"
          title={t("document_revision_open_named", { title })}
          aria-label={t("document_revision_open_named", { title })}
          onClick={open}
        >
          {title}
        </button>
      ) : (
        <span className="ui-card-target__name">{title}</span>
      )}
      {version ? <span className="ui-card-target__version">{t("document_revision_version", { n: version })}</span> : null}
      <span className="ui-card-target__space" />
      <button
        type="button"
        className="ui-card-target__remove"
        disabled={removeDisabled}
        aria-label={t("document_revision_remove_named", { title })}
        title={t("document_revision_remove_named", { title })}
        onClick={onRemove}
      >
        <LucideGlyph icon={X} size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
