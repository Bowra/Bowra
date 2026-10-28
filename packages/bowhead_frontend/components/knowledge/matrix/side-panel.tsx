"use client";

/**
 * kb-matrix M1 — citation side panel (layer 2 of the three-layer source interaction).
 * Verbatim translation of design r2 matrix-panels.jsx SidePanel, M1 subset:
 * value + status badges, highlighted quote + source meta + "查看原文", multi
 * source cards, collapsible "抽取信息" (confidence lives ONLY here), and the
 * [编辑值]/[标记已核] actions backed by the existing records endpoints.
 * [重跑此格]/[按原话保留为文本] arrive with M2 (hidden here).
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { useKbChunkContent } from "@/lib/canvas-pm-adapter/kb-chunk-cache";
import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "@/lib/kb-matrix/types";
import { cellChoiceList } from "@/lib/kb-matrix/types";

import { locateExcerpt } from "./cite-popover";
import { IcCheck, IcChevR, IcClock, IcDoc, IcPencil, IcRefresh, IcWarn, IcX } from "./matrix-icons";
import { KbSourceDrawer } from "../kb-source-drawer";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { CharIconText } from "@/components/char-icon";

export interface SidePanelProps {
  row: MatrixRowVM;
  col: MatrixColumnVM;
  cell: MatrixCellVM;
  /** viewer role → server rejects writes; hide the entry points too (FR-34). */
  canEdit: boolean;
  saving: boolean;
  onClose: () => void;
  /** wave-2: choice columns submit arrays/yes-no values/option strings; allow unknown (backend normalize_matrix_value normalizes). */
  onSaveValue: (newValue: unknown) => void;
  onMarkVerified: () => void;
  /** M2: rerun one cell (FR-12b runs in place without a dialog; failed state uses primary [重试此格], FR-11). */
  onRerunCell?: () => void;
  /** M2: keep verbatim as text (FR-10, shown only in needs_review). */
  onKeepAsText?: () => void;
  /** M3: undo verification (FR-12 r5; replaces [标记已核] when verified, also available in the failed branch). */
  onUnverify?: () => void;
  /** M3: undo edited marker (FR-9 r5; shown when edited, allowing later runs to overwrite it). */
  onRevertEdited?: () => void;
}

function typedInput(type: string, isReview: boolean): "text" | "number" | "date" {
  // A needs_review cell holds verbatim text; number input would sanitize it to blank,
  // while users need to see the original as they edit (impl review FE P3-2).
  if (isReview) return "text";
  if (type === "number" || type === "currency") return "number";
  if (type === "date") return "date";
  return "text";
}

/** wave-2: select/multi_select/boolean use choice editing/display controls. */
function isChoiceCol(type: string): boolean {
  return type === "select" || type === "multi_select" || type === "boolean";
}

/** Initial choice value on edit entry (token/join display value → editor-required shape).
 * boolean tokens "true"/"false" → 是/否 (backend normalize casefold accepts 是/否/true/false);
 * multi_select → option array; select → option string; empty → null. */
export function choiceInitial(col: MatrixColumnVM, cell: MatrixCellVM): unknown {
  if (col.type === "boolean") return cell.value === "true" ? "是" : cell.value === "false" ? "否" : null;
  // Filter multi_select to valid options (wave-2 review F1): needs_review cells have
  // {raw_string} as primary with no choices; cellChoiceList's verbatim-text fallback can include invalid fragments. The editor renders only
  // valid-option checkboxes, leaving invalid fragments invisible/unremovable and saving back to needs_review. Filtering lets review
  // cells recover by selecting valid options; done cells have choices⊆options, so this is a no-op.
  if (col.type === "multi_select") return cellChoiceList(cell).filter((x) => (col.options ?? []).includes(x));
  return cell.value || null;
}

/** Side-panel choice editor (verbatim design-wave2/matrix-wave2b.jsx ChoiceEdit):
 * select radio buttons / multi_select checkboxes / boolean three-state selector (是/否/清空). Controlled:
 * lift values into panel `val`; bottom [保存]/[取消] submits (reuse existing foot without duplicate buttons). */
function ChoiceEditControl({
  type,
  options,
  value,
  onChange,
}: {
  type: string;
  options: string[];
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const t = useTranslations("kbMatrix");
  const isMulti = type === "multi_select";
  const arr = Array.isArray(value) ? (value as string[]) : [];
  const has = (o: string) => (isMulti ? arr.includes(o) : value === o);
  const toggle = (o: string) => {
    if (isMulti) onChange(arr.includes(o) ? arr.filter((x) => x !== o) : [...arr, o]);
    else onChange(o);
  };

  if (type === "boolean") {
    // Always send "是"/"否"/null (not localized labels; backend recognizes only 是/否/true/false).
    const btns: Array<{ v: string | null; label: string }> = [
      { v: "是", label: t("bool_true") },
      { v: "否", label: t("bool_false") },
      { v: null, label: t("choice_clear_short") },
    ];
    // The "清空" segment means null; segmented controls require string values, so use a sentinel.
    const CLEAR = "__clear__";
    const segValue = value === null ? CLEAR : typeof value === "string" ? value : null;
    return (
      <SegmentedControl
        className="mx-triseg"
        data-testid="kb-matrix-choice-edit"
        value={segValue}
        onValueChange={(v) => onChange(v === CLEAR ? null : v)}
      >
        {btns.map((b, i) => (
          <Segment key={i} value={b.v ?? CLEAR} className={value === b.v ? "on" : ""}>
            {b.label}
          </Segment>
        ))}
      </SegmentedControl>
    );
  }

  return (
    <div data-testid="kb-matrix-choice-edit">
      {isMulti ? (
        // Multiselect: each checkbox is a Tab stop, matching native checkboxes.
        <div className="mx-choicelist" role="group">
          {options.map((o) => (
            // Accessible semantics (wave-2 design review #4): real button + role/aria-checked, keyboard reachable
            // (the design used div onClick; translation implements accessible controls).
            <button
              key={o}
              type="button"
              role="checkbox"
              aria-checked={has(o)}
              className={"mx-choiceopt" + (has(o) ? " on" : "")}
              onClick={() => toggle(o)}
            >
              <span className="box">{has(o) && <IcCheck s={11} />}</span>
              {o}
            </button>
          ))}
        </div>
      ) : (
        // Single selection: arrows/Home/End move and select; Tab stops only at selected item (component unification P1 step 6).
        <RadioGroup
          appearance="none"
          className="mx-choicelist"
          orientation="both"
          value={typeof value === "string" && options.includes(value) ? value : null}
          onValueChange={(o) => toggle(o)}
        >
          {options.map((o) => (
            <Radio key={o} value={o} className={"mx-choiceopt round" + (has(o) ? " on" : "")}>
              <span className="box">{has(o) && <IcCheck s={11} />}</span>
              {o}
            </Radio>
          ))}
        </RadioGroup>
      )}
      <button type="button" className="mx-clearval" onClick={() => onChange(isMulti ? [] : null)}>
        <IcX s={12} />
        {t("choice_clear")}
      </button>
    </div>
  );
}

export function SidePanel({
  row,
  col,
  cell,
  canEdit,
  saving,
  onClose,
  onSaveValue,
  onMarkVerified,
  onRerunCell,
  onKeepAsText,
  onUnverify,
  onRevertEdited,
}: SidePanelProps) {
  const t = useTranslations("kbMatrix");
  const [edit, setEdit] = useState(false);
  const [val, setVal] = useState<unknown>(cell.value);
  const [infoOpen, setInfoOpen] = useState(false);
  // "查看原文" overlays file details in place and locates the source (no longer navigates to "文件" view, discovery #1).
  const [viewingSource, setViewingSource] = useState(false);
  useEffect(() => {
    setVal(cell.value);
    setEdit(false);
  }, [cell]);

  const chunk = useKbChunkContent(row.fileId, cell.chunkId, undefined, { single: true });
  const located = chunk.status === "ready" ? locateExcerpt(chunk.content, cell.sourceExcerpt) : null;
  const review = cell.status === "needs_review";
  const failed = cell.status === "failed";
  const isChoice = isChoiceCol(col.type);
  // Only done + choice uses chip/boolval display (review shows verbatim text through mx-valbig).
  const showChoiceVal = isChoice && !review;

  // Choice display (done): boolean yes/no badge / select single chip / multi_select chips / empty→clear.
  const renderChoiceValue = () => {
    if (col.type === "boolean") {
      if (cell.value !== "true" && cell.value !== "false") return <span className="mx-nullval">{t("choice_clear")}</span>;
      const yes = cell.value === "true";
      return (
        <span className={"mx-boolval " + (yes ? "yes" : "no")} style={{ fontSize: "1.125rem" }}>
          <span className="dot" />
          {yes ? t("bool_true") : t("bool_false")}
        </span>
      );
    }
    if (col.type === "multi_select") {
      const list = cellChoiceList(cell);
      if (!list.length) return <span className="mx-nullval">{t("choice_clear")}</span>;
      return (
        <span className="val chips" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {list.map((x, i) => (
            <span key={i} className="mx-chipval" style={{ height: 24, fontSize: "0.78125rem" }}>{x}</span>
          ))}
        </span>
      );
    }
    if (!cell.value) return <span className="mx-nullval">{t("choice_clear")}</span>;
    return <span className="mx-chipval" style={{ height: 26, fontSize: "0.84375rem" }}>{cell.value}</span>;
  };

  return (
    <div className="mx-panel" data-testid="kb-matrix-panel" onClick={(e) => e.stopPropagation()}>
      <div className="mx-panel-head">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="col">{col.question || col.name}</div>
          <div className="file">{row.missing ? t("file_missing") : row.fileName}</div>
        </div>
        <button type="button" className="mx-back" aria-label={t("close")} onClick={onClose}>
          <IcX s={16} />
        </button>
      </div>

      {(cell.sourceCheckState === "source_temporarily_unavailable" || (cell.currentSourceState && cell.currentSourceState !== "eligible")) && <p className="mx-inlineerr" role="note">{t("current_source_warning")}</p>}
      {failed ? (
        <>
          <div className="mx-panel-body">
            <div className="mx-inlineerr" style={{ marginTop: 0 }} data-testid="kb-matrix-panel-failed">
              {t("fail_prefix")}
              {t(`fail_${cell.failReason ?? "llm_invoke_failed"}` as never)}
            </div>
          </div>
          <div className="mx-panel-foot">
            {canEdit && onRerunCell && (
              <button type="button" className="kb-btn kb-btn-primary" style={{ flex: 1 }} disabled={saving} onClick={onRerunCell}>
                <IcRefresh s={14} />
                {t("retry_cell")}
              </button>
            )}
            {/* failed+verified is reachable (force/stale rerun fails on verified cells, G-P2-3); provide the undo entry. */}
            {canEdit && cell.verified && onUnverify && (
              <button type="button" className="kb-btn kb-btn-secondary" disabled={saving} onClick={onUnverify}>
                {t("unverify")}
              </button>
            )}
          </div>
        </>
      ) : (
        <>
      <div className="mx-panel-body">
        {edit && isChoice ? (
          <ChoiceEditControl type={col.type} options={col.options ?? []} value={val} onChange={setVal} />
        ) : (
          <div className="mx-valrow">
            {edit ? (
              <input
                className="mx-inp"
                type={typedInput(col.type, review)}
                value={typeof val === "string" ? val : ""}
                autoFocus
                style={{ fontSize: "0.9375rem" }}
                onChange={(e) => setVal(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    // §4.8: Esc exits editing only (discard unsaved input); stopPropagation prevents
                    // document-level Esc from closing the entire side panel.
                    e.stopPropagation();
                    setVal(cell.value);
                    setEdit(false);
                  }
                }}
              />
            ) : cell.status === "done" && !cell.value ? (
              <span className="mx-nullval">{t("cell_no_value")}</span>
            ) : showChoiceVal ? (
              renderChoiceValue()
            ) : (
              <span className={"mx-valbig" + (col.type === "currency" || col.type === "number" ? " mono" : "")}>
                {cell.value || "—"}
              </span>
            )}
            {!edit && cell.status === "done" && !cell.value && <p className="mx-empty-explanation">{t("cell_no_value_detail")}</p>}
            {!edit && cell.verified && (
              <span className="mx-vbadge verified"><IcCheck s={12} />{t("badge_verified")}</span>
            )}
            {!edit && cell.edited && (
              <span className="mx-vbadge edited"><IcPencil s={11} />{t("badge_edited")}</span>
            )}
            {!edit && review && (
              <span className="mx-vbadge review"><IcWarn s={11} />{t("badge_review")}</span>
            )}
            {!edit && cell.stale && (
              <span className="mx-vbadge stale"><IcClock s={11} />{t("badge_stale")}</span>
            )}
          </div>
        )}

        {review && !edit && (
          <div className="mx-fallback" style={{ color: "var(--c-warn)", fontStyle: "normal", marginTop: 8 }}>
            {/* wave-2: choice columns use "不在选项中"/"无法判定为是/否"; others retain review_note. */}
            {isChoice
              ? col.type === "boolean"
                ? t("review_not_boolean")
                : t("review_not_in_options", { name: col.name })
              : t("review_note", { type: t(`type_${col.type}` as never) })}
          </div>
        )}

        {!edit && (
          <>
            <div className="mx-seclabel">{t("sec_citation")}</div>
            <div className="mx-quote">
              {located ? (
                <>
                  {located.before}
                  <span className="mx-hl">{located.hit}</span>
                  {located.after}
                </>
              ) : chunk.status !== "error" && cell.sourceExcerpt ? (
                <span className="mx-hl">{cell.sourceExcerpt}</span>
              ) : (
                <span className="mx-quote-unlocated">{t("cite_unlocated")}</span>
              )}
            </div>
            <div className="mx-srcmeta">
              <IcDoc s={13} />
              <span>
                {row.missing ? t("file_missing") : row.fileName}
                {cell.page != null ? ` · ${t("page_n", { n: cell.page })}` : ""}
              </span>
            </div>
            {!located && cell.sourceExcerpt && <div className="mx-fallback">{t("cite_fallback")}</div>}
            {!row.missing && (
              <button type="button" className="mx-viewsrc" onClick={() => setViewingSource(true)}>
                {t("view_source")} <IcChevR s={13} />
              </button>
            )}
          </>
        )}

        {!edit && cell.variants.length > 1 && (
          <>
            <div className="mx-seclabel">{t("sec_sources", { n: cell.variants.length })}</div>
            {cell.variants.map((v, i) => (
              <div key={i} className={"mx-srccard" + (v.is_primary ? " main" : "")}>
                <div className="sv">
                  {String(
                    v.normalized && "amount" in v.normalized
                      ? v.normalized.amount
                      : v.normalized && "value" in v.normalized
                        ? v.normalized.value
                        : v.normalized && "raw_string" in v.normalized
                          ? v.normalized.raw_string
                          : // After keep_as_text, normalized=null and raw holds verbatim text (FE-P2-6: same priority as primary value).
                            ((v as { raw?: unknown }).raw ?? "—"),
                  )}
                  {v.is_primary && <span className="mtag">{t("primary_value")}</span>}
                </div>
                <div className="smeta">
                  {v.extracted_by}
                  {v.page != null ? ` · ${t("page_n", { n: v.page })}` : ""}
                </div>
              </div>
            ))}
          </>
        )}

        <div className={"mx-collapse" + (infoOpen ? " open" : "")}>
          <div
            className="mx-collapse-head"
            role="button"
            tabIndex={0}
            onClick={() => setInfoOpen((v) => !v)}
            onKeyDown={(e) => e.key === "Enter" && setInfoOpen((v) => !v)}
          >
            <span className="chev"><IcChevR s={13} /></span>
            {t("sec_extract_info")}
          </div>
          <div className="mx-collapse-body">
            {cell.confidence != null && (
              <>
                {t("confidence_row", { n: Math.round(cell.confidence * 100) })}
                <br />
              </>
            )}
            {/* M3 §4.7: cross-check agreement rate (presentation-only; agree≤0.5 → suggest manual verification). */}
            {cell.selfConsistencyAgree != null && (
              <>
                {cell.selfConsistencyAgree <= 0.5 ? <CharIconText ch="⚠" at="end">{t("cv_agree_bad")}</CharIconText> : <CharIconText ch="✓" at="end">{t("cv_agree_ok")}</CharIconText>}
                <br />
              </>
            )}
            {cell.extractedAt && t("extracted_at_row", { at: cell.extractedAt.slice(0, 16).replace("T", " ") })}
          </div>
        </div>
      </div>

      <div className="mx-panel-foot">
        {edit ? (
          <>
            <button
              type="button"
              className="kb-btn kb-btn-primary"
              disabled={saving}
              // Empty multi array → null (wave-2 review F2): clearing/deselecting all with [] makes backend persist
              // a {"raw_string":[]} sentinel, unlike select's null-on-clear contract; empty means not applicable to this document.
              onClick={() => onSaveValue(col.type === "multi_select" && Array.isArray(val) && val.length === 0 ? null : val)}
            >
              {t("save")}
            </button>
            <button
              type="button"
              className="kb-btn kb-btn-ghost"
              onClick={() => {
                setVal(cell.value);
                setEdit(false);
              }}
            >
              {t("cancel")}
            </button>
          </>
        ) : (
          canEdit && (
            <>
              <button
                type="button"
                className="kb-btn kb-btn-secondary"
                onClick={() => {
                  // Choice edit entry uses editor-required initial shape (token/join → array/yes-no value/option string).
                  setVal(isChoice ? choiceInitial(col, cell) : cell.value);
                  setEdit(true);
                }}
              >
                <IcPencil s={13} />
                {t("edit_value")}
              </button>
              {/* Mark verified only for done cells (backend set_cell_verified requires status=done; verifying
                  needs_review would confirm a value that fails even the column type, wave-2
                  design review #1). Review cells instead use [编辑值] (valid choice→done) or [保留为文本]. */}
              {cell.status === "done" && !cell.verified && (
                <button type="button" className="kb-btn kb-btn-secondary" disabled={saving} onClick={onMarkVerified}>
                  <IcCheck s={14} />
                  {t("mark_verified")}
                </button>
              )}
              {cell.verified && onUnverify && (
                <button type="button" className="kb-btn kb-btn-secondary" disabled={saving} onClick={onUnverify}>
                  {t("unverify")}
                </button>
              )}
              {cell.edited && onRevertEdited && (
                <button type="button" className="kb-btn kb-btn-secondary" disabled={saving} onClick={onRevertEdited}>
                  {t("revert_edited")}
                </button>
              )}
              {onRerunCell && (
                <button type="button" className="kb-btn kb-btn-secondary" disabled={saving} onClick={onRerunCell}>
                  <IcRefresh s={13} />
                  {t("rerun_cell")}
                </button>
              )}
              {review && onKeepAsText && (
                <button type="button" className="kb-btn kb-btn-secondary" disabled={saving} onClick={onKeepAsText}>
                  {t("keep_as_text")}
                </button>
              )}
            </>
          )
        )}
      </div>
        </>
      )}
      {viewingSource && (
        <KbSourceDrawer
          fileId={row.fileId}
          chunkId={cell.chunkId}
          highlight={located?.hit}
          onClose={() => setViewingSource(false)}
        />
      )}
    </div>
  );
}
