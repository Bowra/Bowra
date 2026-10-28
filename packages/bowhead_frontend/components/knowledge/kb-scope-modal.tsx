"use client";

/**
 * kb-management M5 · scope-change modal (single + bulk shared).
 * Flow: select → confirm → migrating → result (partial-failure + retry).
 * team scope gated by real is_team_admin. Wired to bulkChangeKbFilesScope
 * (works for 1 or N files; per-item results drive the failed list).
 */
import { useTranslations } from "next-intl";
import { useState } from "react";

import {
  kbFileError,
  kbFileErrorFromCode,
} from "@/lib/kb-file-error";
import { DialogShell } from "@/components/ui/dialog";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { type KbScope, bulkChangeKbFilesScope } from "@/lib/kb-api";

import { IconSpin, IconX } from "./kb-icons";
import { type KbMeContext, scopeIdFor } from "./kb-me";
import type { NotifyFn } from "@/lib/use-toast";
import { CharIconText } from "@/components/char-icon";

export interface ScopeTarget {
  file_id: string;
  filename: string;
  scope: KbScope;
}

interface Props {
  targets: ScopeTarget[];
  bulk: boolean;
  me: KbMeContext;
  onClose: () => void;
  onMigrated: (failedIds: string[]) => void;
  toast: NotifyFn;
}

// scope label reused from knowledge_base.scope_*; descriptions in scope_modal.desc.*
type Step = "select" | "confirm" | "migrating" | "result";
interface FailedItem { file_id: string; name: string; reason: string }

export function KbScopeModal({ targets, bulk, me, onClose, onMigrated, toast }: Props) {
  const t = useTranslations("knowledge_base");
  const scopeLabel = (sc: KbScope) => t(`scope_${sc}`);
  const [step, setStep] = useState<Step>("select");
  const [chosen, setChosen] = useState<KbScope | null>(null);
  const [failed, setFailed] = useState<FailedItem[]>([]);

  const ids = targets.map((x) => x.file_id);
  const fromScopes = [...new Set(targets.map((x) => x.scope))];
  const fromLabel = fromScopes.length === 1 ? scopeLabel(fromScopes[0]) : t("scope_modal.n_scopes", { n: fromScopes.length });
  const nextDisabled = !chosen || (fromScopes.length === 1 && chosen === fromScopes[0]);

  async function runMigrate(workIds: string[]) {
    if (!chosen) return;
    setStep("migrating");
    try {
      const res = await bulkChangeKbFilesScope(workIds, {
        new_scope: chosen,
        new_scope_id: scopeIdFor(chosen, me),
      });
      const failedItems: FailedItem[] = res.results
        .filter((r) => !r.ok)
        .map((r) => ({
          file_id: r.file_id,
          name: targets.find((x) => x.file_id === r.file_id)?.filename ?? r.file_id,
          reason: t(kbFileErrorFromCode(r.error_code, "scope")),
        }));
      setFailed(failedItems);
      setStep("result");
      const okN = workIds.length - failedItems.length;
      if (failedItems.length) toast("error", t("scope_modal.result_counts", { ok: okN, failed: failedItems.length }));
      else toast("success", t.rich("scope_modal.migrated_ok", { n: okN, scope: scopeLabel(chosen), b: (c) => <b>{c}</b> }));
    } catch (e) {
      const reason = t(kbFileError(e, "scope"));
      setFailed(workIds.map((id) => ({
        file_id: id,
        name: targets.find((x) => x.file_id === id)?.filename ?? id,
        reason,
      })));
      setStep("result");
      toast("error", t("scope_modal.migrate_failed_err", { error: reason }));
    }
  }

  const label = step === "select"
    ? (bulk ? t("scope_modal.select_title_bulk", { n: ids.length }) : t("scope_modal.select_title"))
    : step === "confirm"
      ? t("scope_modal.confirm_title")
      : step === "migrating"
        ? t("scope_modal.migrating")
        : failed.length ? t("scope_modal.result_title_partial") : t("scope_modal.result_title");

  return (
    <DialogShell
      onClose={onClose}
      busy={step === "migrating"}
      label={label}
      backdropClassName="kb-overlay"
      panelClassName="kb-modal"
    >
      {step === "select" && (
        <>
          <div className="kb-modal-head">
            <h3 id="kb-scope-modal-title">{bulk ? t("scope_modal.select_title_bulk", { n: ids.length }) : t("scope_modal.select_title")}</h3>
            <div className="sub">{t("scope_modal.select_sub", { from: fromLabel })}</div>
          </div>
          {/* Cards are radio options: arrows and Home/End move and select, skip the disabled Team option; Tab only stops on the selected option (component unification P1, step 6).
              This step is unmounted during migration, so busy is unnecessary. */}
          <RadioGroup<KbScope>
            className="kb-modal-body"
            orientation="both"
            aria-labelledby="kb-scope-modal-title"
            value={chosen}
            onValueChange={setChosen}
          >
            {(["personal", "space", "team"] as KbScope[]).map((sc) => {
              const isTeam = sc === "team";
              const disabled = isTeam && !me.is_team_admin;
              return (
                <Radio
                  key={sc}
                  value={sc}
                  disabled={disabled}
                  data-testid={`kb-scope-modal-${sc}`}
                  className={`kb-scope-card ${chosen === sc ? "on" : ""} ${disabled ? "disabled" : ""}`}
                >
                  <span className="radio" />
                  <span className="kb-scope-card-text">
                    <span className="title">{scopeLabel(sc)}</span>
                    <span className="desc">{t(`scope_modal.desc.${sc}`)}{disabled ? t("scope_modal.non_admin_suffix") : ""}</span>
                  </span>
                  {isTeam && <span className="lock">{me.is_team_admin ? t("scope_modal.admin") : <CharIconText ch="🔒">{t("scope_modal.lock")}</CharIconText>}</span>}
                </Radio>
              );
            })}
          </RadioGroup>
          <div className="kb-modal-foot">
            <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>{t("scope_modal.cancel")}</button>
            <button type="button" className="kb-btn kb-btn-primary" disabled={nextDisabled} onClick={() => setStep("confirm")}>{t("scope_modal.next")}</button>
          </div>
        </>
      )}

      {step === "confirm" && chosen && (
        <>
          <div className="kb-modal-head">
            <h3>{t("scope_modal.confirm_title")}</h3>
            <div className="sub">{t("scope_modal.confirm_sub")}</div>
          </div>
          <div className="kb-modal-body">
            <div className="kb-notice">
              {t.rich("scope_modal.confirm_body", {
                subject: bulk ? t("scope_modal.n_files", { n: ids.length }) : targets[0].filename,
                from: fromLabel,
                to: scopeLabel(chosen),
                subj: (c) => <b className="kb-scope-subj">{c}</b>,
                hl: (c) => <b className="kb-scope-to">{c}</b>,
                br: () => <br />,
              })}
            </div>
          </div>
          <div className="kb-modal-foot">
            <button type="button" className="kb-btn kb-btn-ghost" onClick={() => setStep("select")}><CharIconText ch="←">{t("scope_modal.back")}</CharIconText></button>
            <button type="button" className="kb-btn kb-btn-primary" onClick={() => runMigrate(ids)}>{t("scope_modal.confirm_migrate")}</button>
          </div>
        </>
      )}

      {step === "migrating" && chosen && (
        <div className="kb-modal-body kb-scope-progress">
          <div className="kb-scope-progress-spin">
            <IconSpin size={26} />
          </div>
          <div className="kb-scope-progress-title">{t("scope_modal.migrating")}</div>
          <div className="kb-scope-progress-sub">
            {bulk ? t("scope_modal.n_files", { n: ids.length }) : t("scope_modal.one_file")} → {scopeLabel(chosen)}
          </div>
        </div>
      )}

      {step === "result" && chosen && (
        <>
          <div className="kb-modal-head">
            <h3>{failed.length ? t("scope_modal.result_title_partial") : t("scope_modal.result_title")}</h3>
            <div className="sub">{failed.length ? t("scope_modal.result_sub_partial", { ok: ids.length - failed.length, failed: failed.length }) : t("scope_modal.result_sub", { ok: ids.length - failed.length })}</div>
          </div>
          <div className="kb-modal-body">
            <div className={`kb-notice ${failed.length ? "" : "ok"}`}>
              {ids.length - failed.length > 0
                ? t.rich("scope_modal.result_body", { n: ids.length - failed.length, scope: scopeLabel(chosen), b: (c) => <b>{c}</b> })
                : t("scope_modal.result_none")}
            </div>
            {failed.length > 0 && (
              <div className="kb-scope-failed">
                <div className="kb-pop-label kb-scope-failed-label">{t("scope_modal.failed_label")}</div>
                {failed.map((f) => (
                  <div className="kb-up-row" key={f.file_id}>
                    <span className="nm">{f.name}</span>
                    <span className="kb-up-state failed"><IconX /> {f.reason}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="kb-modal-foot">
            {failed.length > 0 && (
              <button type="button" className="kb-btn kb-btn-secondary" onClick={() => runMigrate(failed.map((f) => f.file_id))}>{t("scope_modal.retry_failed")}</button>
            )}
            <button type="button" className="kb-btn kb-btn-primary" onClick={() => { onMigrated(failed.map((f) => f.file_id)); }}>{t("scope_modal.done")}</button>
          </div>
        </>
      )}
    </DialogShell>
  );
}
