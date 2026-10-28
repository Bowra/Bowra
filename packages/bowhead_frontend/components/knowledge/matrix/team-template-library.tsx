"use client";

/**
 * kb-matrix-sharing M5 — presentational team-template library (matrix-m5.jsx verbatim translation).
 *
 * Discovery surface (TeamLibraryView + TeamTplCard), publish/withdraw/name-conflict dialogs, copy-success Toast,
 * published badge/changed flag, read-only mobile list. Stateful host (tab switching + fetching + workflows)
 * is template-manager.tsx; this file provides only stateless UI + a11y hooks.
 */

import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";
import type { TeamTemplateDTO } from "@/lib/kb-matrix/team-template-api";

import { TYPE_GLYPH } from "./matrix-grid";
import { IcCheck, IcGrid, IcRefresh, IcShare, IcSpin, IcWarn, IcX } from "./matrix-icons";

/* ─── field preview chips (+N hover/focus tooltip) ─── */
export function FieldChips({ fields, max = 4 }: { fields: Array<{ name: string; type: string }>; max?: number }) {
  const t = useTranslations("kbMatrix");
  const shown = fields.slice(0, max);
  const rest = fields.slice(max);
  return (
    <div className="mx-chips kb-otpl-fields">
      {shown.map((f, i) => (
        <span className="chip" key={i}>
          <span className="g">{TYPE_GLYPH[f.type] ?? "Aa"}</span>
          {f.name}
        </span>
      ))}
      {rest.length > 0 && (
        <span
          className="chip more kb-otpl-morewrap"
          tabIndex={0}
          aria-label={t("otpl_more", { n: rest.length, list: rest.map((f) => f.name).join("、") })}
        >
          +{rest.length}
          <span className="kb-otpl-tip" role="tooltip">
            {rest.map((f, i) => (
              <span className="tl" key={i}>
                <span className="g">{TYPE_GLYPH[f.type] ?? "Aa"}</span>
                {f.name}
              </span>
            ))}
          </span>
        </span>
      )}
    </div>
  );
}

export function PublishedBadge() {
  const t = useTranslations("kbMatrix");
  return (
    <span className="kb-otpl-mybadge">
      <IcCheck s={11} />
      {t("tpl_published_badge")}
    </span>
  );
}

export function DriftFlag() {
  const t = useTranslations("kbMatrix");
  return (
    <span className="kb-otpl-drift" title={t("tpl_drift_tip")}>
      <span className="dot" />
      {t("tpl_drift_flag")}
    </span>
  );
}

/* ─── team-library discovery card ─── */
type TeamCardState = "copyable" | "copied" | "own" | "viewer";

function cardState(e: TeamTemplateDTO, canCopy: boolean): TeamCardState {
  if (e.is_own_space) return "own";
  if (e.already_copied) return "copied";
  if (!canCopy) return "viewer";
  return "copyable";
}

function TeamTplCard({
  e,
  canCopy,
  copying,
  onCopy,
  onViewMine,
}: {
  e: TeamTemplateDTO;
  canCopy: boolean;
  copying: boolean;
  onCopy: (e: TeamTemplateDTO) => void;
  onViewMine: () => void;
}) {
  const t = useTranslations("kbMatrix");
  const state = cardState(e, canCopy);
  return (
    <div className={"kb-otpl-card" + (state === "copyable" ? " copyable" : "")} data-testid={`kb-otpl-card-${e.entry_id}`}>
      {state === "copied" && <span className="kb-otpl-tag copied">{t("otpl_copied_badge")}</span>}
      {state === "own" && <span className="kb-otpl-tag own">{t("otpl_own_badge")}</span>}
      <div className="top">
        <span className="nm">{e.name}</span>
      </div>
      {e.description && <div className="desc">{e.description}</div>}
      <FieldChips fields={e.field_preview} max={4} />
      <div className="kb-otpl-act">
        {state === "copyable" && (
          <button
            type="button"
            className="kb-btn kb-btn-secondary kb-btn-sm"
            disabled={copying}
            data-testid={`kb-otpl-copy-${e.entry_id}`}
            onClick={() => onCopy(e)}
          >
            {copying ? (
              <>
                <IcSpin s={13} />
                {t("otpl_copying")}
              </>
            ) : (
              t("otpl_copy")
            )}
          </button>
        )}
        {state === "copied" && (
          <>
            <span className="kb-otpl-copied">
              <IcCheck s={13} />
              {t("otpl_copied_badge")}
            </span>
            <button type="button" className="kb-otpl-link" onClick={onViewMine}>
              {t("otpl_copied_view")}
            </button>
          </>
        )}
        {state === "own" && (
          <>
            <span className="kb-otpl-own">{t("otpl_own_badge")}</span>
            <button type="button" className="kb-otpl-link" onClick={onViewMine}>
              {t("otpl_own_manage")}
            </button>
          </>
        )}
        {state === "viewer" && (
          <button type="button" className="kb-btn kb-btn-secondary kb-btn-sm" disabled title={t("otpl_viewer_tip")}>
            {t("otpl_copy")}
          </button>
        )}
      </div>
      <div className="kb-otpl-src">
        {t("otpl_source_space", { name: e.source_workspace_name ?? e.source_workspace_id })}
        <br />
        {t("otpl_publisher_at", { name: e.published_by_name ?? e.published_by, date: e.published_at.slice(0, 10) })}
      </div>
    </div>
  );
}

function SkeletonCard() {
  return (
    <div className="kb-otpl-skel">
      <div className="bar" style={{ width: "52%", marginBottom: 12 }} />
      <div className="bar" style={{ width: "88%", height: 9, marginBottom: 7 }} />
      <div className="bar" style={{ width: "64%", height: 9 }} />
      <div className="chips">
        <div className="bar" style={{ width: 68 }} />
        <div className="bar" style={{ width: 54 }} />
        <div className="bar" style={{ width: 60 }} />
      </div>
      <div className="bar" style={{ width: "46%", height: 9, marginTop: 4 }} />
    </div>
  );
}

/* ─── discover view (loading / error / empty / grid) ─── */
export function TeamLibraryView({
  entries,
  loading,
  error,
  canCopy,
  copyingId,
  onCopy,
  onViewMine,
  onRetry,
  inlineEmpty,
}: {
  entries: TeamTemplateDTO[];
  loading: boolean;
  error: boolean;
  canCopy: boolean;
  copyingId: string | null;
  onCopy: (e: TeamTemplateDTO) => void;
  onViewMine: () => void;
  onRetry: () => void;
  /** Flat groups in knowledge-base left-navigation Templates view: caller renders group headers, omit subtitle; empty state is a single dashed explanatory row. */
  inlineEmpty?: string;
}) {
  const t = useTranslations("kbMatrix");
  const inline = inlineEmpty !== undefined;
  return (
    <div style={inline ? undefined : { paddingBottom: 24 }} data-testid="kb-otpl-view">
      {!inline && <div className="kb-otpl-sub">{t("otpl_sub")}</div>}
      {loading ? (
        <div className="kb-otpl-grid">
          {Array.from({ length: 6 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      ) : error ? (
        <div className="kb-otpl-error" data-testid="kb-otpl-error">
          <div className="eic">
            <IcWarn s={22} />
          </div>
          <div className="msg">{t("otpl_error")}</div>
          <button type="button" className="kb-btn kb-btn-ghost" onClick={onRetry}>
            <IcRefresh s={13} />
            {t("otpl_retry")}
          </button>
        </div>
      ) : entries.length === 0 && inline ? (
        <div className="emptyline" data-testid="kb-otpl-empty">
          {inlineEmpty}
        </div>
      ) : entries.length === 0 ? (
        <div className="kb-tpl-empty" data-testid="kb-otpl-empty">
          <div className="illus">
            <IcGrid s={34} />
          </div>
          <h3>{t("otpl_empty_title")}</h3>
          <div className="p">{t("otpl_empty_body")}</div>
        </div>
      ) : (
        <div className="kb-otpl-grid">
          {entries.map((e) => (
            <TeamTplCard key={e.entry_id} e={e} canCopy={canCopy} copying={copyingId === e.entry_id} onCopy={onCopy} onViewMine={onViewMine} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ─── dialogs ─── */
export function PublishConfirmDialog({
  mode,
  fieldCount,
  submitting,
  onClose,
  onConfirm,
}: {
  mode: "publish" | "republish";
  fieldCount: number;
  submitting: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const t = useTranslations("kbMatrix");
  const republish = mode === "republish";
  const hid = useId();
  return (
    <DialogShell
      onClose={onClose}
      busy={submitting}
      labelledBy={hid}
      backdropClassName="kb-overlay"
      panelClassName="kb-modal"
      panelProps={{ "data-testid": "kb-otpl-publish-dialog" } as Record<string, string>}
    >
      <div className="kb-modal-head">
        <h3 id={hid}>{republish ? t("tpl_republish_confirm_title") : t("tpl_publish_confirm_title")}</h3>
      </div>
      <div className="kb-modal-body">
        <p className="otpl-body-lead">{t("tpl_publish_confirm_body")}</p>
        {republish && (
          <p className="otpl-body-lead otpl-body-note">
            {t("tpl_republish_confirm_body")}
          </p>
        )}
        <div className="otpl-travels yes">
          <div className="h">
            <IcShare s={13} />
            {t("tpl_publish_travels_head")}
          </div>
          <div className="otpl-trow">
            <span className="ic">
              <IcCheck s={15} />
            </span>
            {t("tpl_publish_travels_1")}
          </div>
          <div className="otpl-trow">
            <span className="ic">
              <IcCheck s={15} />
            </span>
            <span>{t.rich("tpl_publish_travels_2", { n: fieldCount, b: (c) => <span className="n">{c}</span> })}</span>
          </div>
        </div>
        <div className="otpl-travels no">
          <div className="h">
            <IcX s={13} />
            {t("tpl_publish_notravels_head")}
          </div>
          <div className="otpl-trow">
            <span className="ic">
              <IcX s={15} />
            </span>
            {t("tpl_publish_notravels_1")}
          </div>
        </div>
        <div className="otpl-scope">{t("tpl_publish_scope")}</div>
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
          {t("cancel")}
        </button>
        <button type="button" className="kb-btn kb-btn-primary" disabled={submitting} data-testid="kb-otpl-publish-confirm" onClick={onConfirm}>
          {submitting ? (
            <>
              <IcSpin s={14} />
              {t("tpl_publishing")}
            </>
          ) : republish ? (
            t("tpl_republish")
          ) : (
            t("tpl_publish_cta")
          )}
        </button>
      </div>
    </DialogShell>
  );
}

export function UnpublishConfirmDialog({ name, submitting, onClose, onConfirm }: { name: string; submitting: boolean; onClose: () => void; onConfirm: () => void }) {
  const t = useTranslations("kbMatrix");
  const hid = useId();
  return (
    <DialogShell onClose={onClose} busy={submitting} labelledBy={hid} backdropClassName="kb-overlay" panelClassName="kb-modal">
      <div className="kb-modal-head">
        <h3 id={hid}>{t("tpl_unpublish_title", { name })}</h3>
      </div>
      <div className="kb-modal-body">
        <p className="otpl-body-lead">{t("tpl_unpublish_body")}</p>
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
          {t("cancel")}
        </button>
        <button type="button" className="kb-btn kb-btn-danger-solid" disabled={submitting} data-testid="kb-otpl-unpublish-confirm" onClick={onConfirm}>
          {t("tpl_unpublish")}
        </button>
      </div>
    </DialogShell>
  );
}

export function CopyCollisionDialog({
  name,
  suggestion,
  error,
  submitting,
  onClose,
  onConfirm,
}: {
  name: string;
  suggestion: string;
  error: string | null;
  submitting: boolean;
  onClose: () => void;
  onConfirm: (name: string) => void;
}) {
  const t = useTranslations("kbMatrix");
  const [val, setVal] = useState(suggestion);
  const inputRef = useRef<HTMLInputElement>(null);
  const hid = useId();
  // DialogShell initially focuses input (data-dialog-initial-focus); another name conflict refreshes suggestion → reset input + refocus (caret at end).
  useEffect(() => {
    setVal(suggestion);
    const el = inputRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(suggestion.length, suggestion.length);
    }
  }, [suggestion]);
  return (
    <DialogShell
      onClose={onClose}
      busy={submitting}
      labelledBy={hid}
      backdropClassName="kb-overlay"
      panelClassName="kb-modal kb-otpl-collision"
      panelProps={{ "data-testid": "kb-otpl-collision-dialog" } as Record<string, string>}
    >
      <div className="kb-modal-head">
        <h3 id={hid}>{t("otpl_copy_conflict_title")}</h3>
      </div>
      <div className="kb-modal-body">
        <p className="otpl-body-lead otpl-body-lead--spaced">
          {t("otpl_copy_conflict_body", { name })}
        </p>
        <div className="mx-flabel mx-flabel--first">
          {t("otpl_copy_name_label")}
        </div>
        <input
          ref={inputRef}
          data-dialog-initial-focus
          className={`mx-inp${error ? " is-invalid" : ""}`}
          value={val}
          maxLength={200}
          onChange={(e) => setVal(e.target.value)}
        />
        {error ? (
          <div className="mx-fielderr mx-fielderr--7">
            {error}
          </div>
        ) : (
          <div className="otpl-copy-hint">{t("otpl_copy_conflict_suggest", { suggestion })}</div>
        )}
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className="kb-btn kb-btn-primary"
          disabled={submitting || !val.trim()}
          data-testid="kb-otpl-collision-confirm"
          onClick={() => onConfirm(val.trim())}
        >
          {t("otpl_copy_conflict_cta")}
        </button>
      </div>
    </DialogShell>
  );
}

/* ─── copy-success toast (with Apply deep-link CTA) ─── */
export function CopyToast({ message, cta, onCta }: { message: ReactNode; cta: string; onCta: () => void }) {
  return (
    <div className="mx-toastwrap">
      <div className="mx-toast" role="status" data-testid="kb-otpl-copy-toast">
        <span className="ic">
          <IcCheck s={15} />
        </span>
        <span className="msg">{message}</span>
        <button type="button" className="cta" onClick={onCta}>
          {cta}
        </button>
      </div>
    </div>
  );
}

/* ─── mobile 412 · team library (read-only) ─── */
export function MobileTeamLibrary({ entries }: { entries: TeamTemplateDTO[] }) {
  const t = useTranslations("kbMatrix");
  return (
    <>
      <div style={{ padding: "2px 14px 6px", fontSize: "0.6875rem", color: "var(--c-ink-3)", lineHeight: 1.5 }}>{t("otpl_sub")}</div>
      {entries.slice(0, 20).map((e) => (
        <div className="mx-otpl-mcard" key={e.entry_id}>
          <div className="top">
            <span className="nm">{e.name}</span>
            {e.already_copied && (
              <span className="kb-otpl-tag copied" style={{ position: "static" }}>
                {t("otpl_copied_badge")}
              </span>
            )}
            {e.is_own_space && (
              <span className="kb-otpl-tag own" style={{ position: "static" }}>
                {t("otpl_own_badge")}
              </span>
            )}
          </div>
          {e.description && <div className="desc">{e.description}</div>}
          <FieldChips fields={e.field_preview} max={3} />
          <div className="src">
            {t("otpl_source_space", { name: e.source_workspace_name ?? e.source_workspace_id })}
            <br />
            {t("otpl_publisher_at", { name: e.published_by_name ?? e.published_by, date: e.published_at.slice(0, 10) })}
          </div>
        </div>
      ))}
      <div style={{ height: 20 }} />
    </>
  );
}
