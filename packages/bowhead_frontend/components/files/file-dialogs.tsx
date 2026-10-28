"use client";

/**
 * file-management-v2 P1/P2 — deletion confirmation, KB guidance overlays, and Toast.
 *
 * Replace old page's direct `confirm()` / `alert()` calls (M1 PRD promised Toast+undo, never delivered).
 * Three deletion paths (PRD §4.3.1):
 *   ordinary → lite confirmation; KB file → **guide navigation, no deletion escape hatch** (D2); bulk → explain skipped count.
 */
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import {
  Library as LucideLibrary,
  TriangleAlert as LucideTriangleAlert,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIcon } from "@/components/char-icon";
import { KbIngestionNotice } from "@/components/knowledge/kb-ingestion-notice";
import { useKbIngestionReadiness } from "@/lib/kb-ingestion-readiness";

export type KbScope = "personal" | "space" | "team";

export type FilesDialog =
  | { kind: "del"; name: string; onConfirm: () => void }
  | { kind: "del-ref"; name: string; onConfirm: () => void }
  | { kind: "del-kb"; name: string; onGoto: () => void }
  | { kind: "del-bulk"; count: number; blocked: number; onConfirm: () => void }
  | { kind: "restore-bulk"; count: number; onConfirm: () => void }
  | { kind: "purge"; count: number; blocked: number; onConfirm: () => void }
  | { kind: "kb-add"; name: string; onConfirm: (scope: KbScope) => void }
  | { kind: "kb-remove"; name: string; onConfirm: () => void };

function WarnIcon() {
  return (
    <LucideGlyph icon={LucideTriangleAlert} size={15} aria-hidden />
  );
}

function BookIcon() {
  return (
    <LucideGlyph icon={LucideLibrary} size={15} aria-hidden />
  );
}

/** `.files-main .scrim` is absolute, covering only Files main area; restore it over FloatingOverlay's inline fixed style. */
const SCRIM_STYLE = { position: "absolute" } as const;

export function FilesDialogs({
  dlg,
  onClose,
  canPromoteToTeam,
  spaceName,
}: {
  dlg: FilesDialog | null;
  onClose: () => void;
  /** null = identity not yet returned: placeholder team card, submission disabled; never flash “仅团队管理员” first. */
  canPromoteToTeam: boolean | null;
  /** Current space name; null = no available space (disable space card). */
  spaceName: string | null;
}) {
  const t = useTranslations("files_page");
  const [scope, setScope] = useState<KbScope>("personal");
  const readiness = useKbIngestionReadiness(dlg?.kind === "kb-add");
  useEffect(() => {
    if (!dlg) setScope("personal");
  }, [dlg]);
  if (!dlg) return null;

  if (dlg.kind === "kb-add") {
    // Design 07·B: scope cards match KB upload dialog; Space includes its name.
    const identityLoading = canPromoteToTeam === null;
    const card = (k: KbScope, title: string, desc: string, disabled: boolean, lock?: string) => (
      <Radio
        key={k}
        value={k}
        className={`kb-scope-card${scope === k ? " on" : ""}${disabled ? " disabled" : ""}`}
        data-testid={`kb-scope-${k}`}
        disabled={disabled}
      >
        <span className="radio" aria-hidden />
        <span className="txt">
          <span className="title">{title}</span>
          <span className="desc">{desc}</span>
        </span>
        {lock ? <span className="lock">{lock}</span> : null}
      </Radio>
    );
    return (
      <DialogShell
        onClose={onClose}
        mount="inline"
        labelledBy="kb-add-title"
        backdropClassName="scrim"
        backdropStyle={SCRIM_STYLE}
        panelClassName="md"
      >
        <div className="md-hd">
          <div className="legacy-files-dialog-headtext">
            <h3 id="kb-add-title">{t("kb.kb_add")}</h3>
            <div className="sub">{t("kb.kb_add_sub", { name: dlg.name })}</div>
          </div>
          <button type="button" className="md-x" aria-label={t("drawer.close")} onClick={onClose}><CharIcon ch="✕" aria-hidden /></button>
        </div>
        <div className="md-bd">
          <KbIngestionNotice state={readiness.state} canConfigure={canPromoteToTeam === true} onRetry={readiness.retry} />
          <div className="md-label" id="kb-add-scope-label">{t("kb.kb_scope_title")}</div>
          <RadioGroup<KbScope>
            className="kb-scope-radios"
            orientation="both"
            aria-labelledby="kb-add-scope-label"
            value={scope}
            onValueChange={setScope}
          >
          {card("personal", t("kb.kb_scope_personal"), t("kb.kb_scope_personal_desc"), false)}
          {card(
            "space",
            spaceName ? t("kb.kb_scope_space_named", { name: spaceName }) : t("kb.kb_scope_space"),
            spaceName ? t("kb.kb_scope_space_desc") : t("kb.kb_scope_no_space"),
            !spaceName,
          )}
          {identityLoading ? (
            <div className="kb-scope-card disabled" data-testid="kb-scope-team-loading" aria-busy>
              <span className="radio" aria-hidden />
              <span className="txt"><span className="skelline legacy-files-skelline--scope" /></span>
            </div>
          ) : (
            card(
              "team",
              t("kb.kb_scope_team"),
              t("kb.kb_scope_team_desc"),
              !canPromoteToTeam,
              canPromoteToTeam ? undefined : t("kb.kb_scope_team_lock"),
            )
          )}
          </RadioGroup>
        </div>
        <div className="md-ft">
          <button type="button" className="btn ghost" onClick={onClose}>{t("dialog.cancel")}</button>
          <button
            type="button"
            className="btn pri"
            data-testid="kb-add-confirm"
            disabled={identityLoading || readiness.state !== "ready"}
            onClick={() => { if (readiness.state === "ready") dlg.onConfirm(scope); }}
          >{t("kb.kb_add_confirm")}</button>
        </div>
      </DialogShell>
    );
  }

  let body: React.ReactNode = null;

  if (dlg.kind === "del") {
    body = (
      <>
        <h3>{t("dialog.del_title")}</h3>
        <p>{t("dialog.del_body", { name: dlg.name })}</p>
        <div className="mrow">
          <button type="button" className="btn ghost" data-dialog-initial-focus onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn danger" onClick={dlg.onConfirm}>{t("dialog.confirm_delete")}</button>
        </div>
      </>
    );
  } else if (dlg.kind === "del-ref") {
    // Report references permit deletion, but disclose those references will break: informed consent, not blocking.
    body = (
      <>
        <h3>{t("dialog.ref_title")}</h3>
        <p>{t("dialog.ref_body", { name: dlg.name })}</p>
        <div className="warnbox">
          <WarnIcon />
          <div>{t("dialog.ref_warn")}</div>
        </div>
        <div className="mrow">
          <button type="button" className="btn ghost" data-dialog-initial-focus onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn danger" onClick={dlg.onConfirm}>{t("dialog.ref_confirm")}</button>
        </div>
      </>
    );
  } else if (dlg.kind === "del-kb") {
    body = (
      <>
        <h3>{t("dialog.kb_title")}</h3>
        <p>{t("dialog.kb_body", { name: dlg.name })}</p>
        <div className="kbbox">
          <BookIcon />
          <div>{t("dialog.kb_hint")}</div>
        </div>
        <div className="mrow">
          <button type="button" className="btn ghost" onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn pri" onClick={dlg.onGoto}>{t("dialog.kb_cta")}</button>
        </div>
      </>
    );
  } else if (dlg.kind === "del-bulk") {
    body = (
      <>
        <h3>{t("dialog.del_bulk_title", { count: dlg.count })}</h3>
        <p>
          {t("dialog.del_bulk_body")}
          {dlg.blocked > 0 ? ` ${t("dialog.del_bulk_kb_note", { count: dlg.blocked })}` : ""}
        </p>
        <div className="mrow">
          <button type="button" className="btn ghost" data-dialog-initial-focus onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn danger" onClick={dlg.onConfirm}>{t("dialog.confirm_delete")}</button>
        </div>
      </>
    );
  } else if (dlg.kind === "purge") {
    // P4 permanent deletion: **no recovery window**. Other deletions enter recoverable trash;
    // this actually deletes blob. Explicitly say “不可恢复” and require second confirmation.
    body = (
      <>
        <h3>{t("dialog.purge_title", { count: dlg.count })}</h3>
        <p>{t("dialog.purge_body")}</p>
        <div className="warnbox">
          <WarnIcon />
          <div>{t("dialog.purge_warn")}</div>
        </div>
        {dlg.blocked > 0 ? (
          // Server blocks KB-referenced files; never silently skip, first explain how many will remain.
          <div className="kbbox">
            <BookIcon />
            <div>{t("dialog.purge_kb_note", { count: dlg.blocked })}</div>
          </div>
        ) : null}
        <div className="mrow">
          <button type="button" className="btn ghost" data-dialog-initial-focus onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn danger" data-testid="purge-confirm" onClick={dlg.onConfirm}>
            {t("dialog.purge_confirm")}
          </button>
        </div>
      </>
    );
  } else if (dlg.kind === "kb-remove") {
    body = (
      <>
        <h3>{t("kb.kb_remove_title")}</h3>
        <p>{t("kb.kb_remove_body")}</p>
        <div className="mrow">
          <button type="button" className="btn ghost" onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn sec" onClick={dlg.onConfirm}>{t("kb.remove")}</button>
        </div>
      </>
    );
  } else {
    body = (
      <>
        <h3>{t("dialog.restore_bulk_title", { count: dlg.count })}</h3>
        <p>{t("dialog.restore_bulk_body")}</p>
        <div className="mrow">
          <button type="button" className="btn ghost" onClick={onClose}>{t("dialog.cancel")}</button>
          <button type="button" className="btn pri" onClick={dlg.onConfirm}>{t("bulk.restore")}</button>
        </div>
      </>
    );
  }

  return (
    <DialogShell
      onClose={onClose}
      mount="inline"
      backdropClassName="scrim"
      backdropStyle={SCRIM_STYLE}
      panelClassName="modal"
    >
      {body}
    </DialogShell>
  );
}

export type FilesToast = {
  id: string;
  text: string;
  /** Navigation action (e.g. View processing progress). */
  action?: { label: string; href: string };
};

export function FilesToasts({ list }: { list: FilesToast[] }) {
  if (!list.length) return null;
  return (
    <div className="toasts">
      {list.map((x) => (
        <div className="toast" key={x.id}>
          <span>{x.text}</span>
          {x.action ? (
            <>
              <span className="vsep" aria-hidden />
              <a className="tact" href={x.action.href}>{x.action.label}</a>
            </>
          ) : null}
        </div>
      ))}
    </div>
  );
}
