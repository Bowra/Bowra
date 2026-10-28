"use client";

/**
 * kb-matrix M4 FR-38 — template management (verbatim design-m4 TemplateManager/TplCard/TplDrawer/
 * MobileTemplates) + M5 team template library (publish/discover/copy, flag-gated).
 *
 * Groups derive from server kind (built-in seed / personal graduated+template+manual); two drawer modes
 * (graduated fields read-only lockbar+dim / editable with OptionsEditor); delete confirmation (graduated
 * includes row count); source-matrix tombstone; read-only list at 412.
 * Apply routing (tech-design decision 14): column-set templates (seed/template/graduated) → new-matrix
 * flow column prefill; manual (extraction table) → existing from_template endpoint (record materialization).
 * M5 team library: kind=template cards add publish/unpublish/republish (canManageTemplate gate); discovery
 * copies into this Space (rename on conflict + [去套用] deep link).
 * Render as Knowledge sidebar's "模板" view (interaction design 2b): view header + [新建模板], flat groups
 * built-in → personal templates → team template library → extraction tables (kind=manual).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";
import { type KbDocTypeDTO, listDocTypesWithStats } from "@/lib/kb-knowledge-tables-api";
import { kbDocTypeError } from "@/lib/kb-doc-type-error";
import {
  MatrixApiError,
  deleteDocTypeTyped,
  isColumnGroupKind,
  updateDocTypeMeta,
} from "@/lib/kb-matrix/graduate-api";
import { kbMatrixMutationError } from "@/lib/kb-matrix/mutation-error";
import {
  type TeamTemplateDTO,
  type PublishState,
  copyTeamTemplate,
  indexEntriesBySource,
  listTeamTemplates,
  publishStateOf,
  publishTemplateToTeam,
  revokeTeamTemplate,
} from "@/lib/kb-matrix/team-template-api";
import { kbTeamTemplateError } from "@/lib/kb-matrix/team-template-error";
import { type MatrixListItem, listMatrices } from "@/lib/kb-matrix/snapshot-api";
import { createMatrixFromTemplate } from "@/lib/kb-matrix/run-api";
import { canManageTemplate } from "@/lib/kb-matrix/template-view";
import { useIsMobile } from "@/lib/kb-matrix/use-is-mobile";
import type { NotifyFn } from "@/lib/use-toast";

import {
  CopyCollisionDialog,
  CopyToast,
  DriftFlag,
  MobileTeamLibrary,
  TeamLibraryView,
  PublishConfirmDialog,
  PublishedBadge,
  UnpublishConfirmDialog,
} from "./team-template-library";
import { DraftWizard } from "@/components/knowledge/extraction-template-wizard";

import { ExtractionTemplateGroup, TemplatePurposeDialog } from "./extraction-templates";
import { OptionsEditor } from "./options-editor";
import { TYPE_GLYPH } from "./matrix-grid";
import { IcClock, IcDots, IcGrid, IcRefresh, IcShare, IcX } from "./matrix-icons";

type Kind = "seed" | "template" | "graduated" | "manual";

/** M5 mutually exclusive dialog state machine; deletion retains separate `del` state. */
type Dialog =
  | { kind: "publish" | "republish" | "unpublish"; dt: KbDocTypeDTO; entry?: TeamTemplateDTO }
  | { kind: "collision"; entryId: string; attempted: string; suggestion: string; error: string | null }
  | null;

export interface TemplateManagerProps {
  workspaceId: string;
  /** Non-viewer Space member may [套用] templates/create matrices/copy team templates. */
  canEdit: boolean;
  /** Current user ID (/auth/me); null = not fetched. created_by determines edit/delete/publish permission. */
  uid: string | null;
  /** team_admin ∨ ws_admin may edit/delete/publish any template (mirrors server admin-or-creator). */
  isAdmin: boolean;
  /** Apply column-set templates (seed/template/graduated) → new-matrix column prefill; post-copy [去套用] follows the same path. */
  onApplyTemplate: (dt: KbDocTypeDTO) => void;
  /** Apply manual template (from_template materialization) / source-matrix link → open matrix view. */
  onOpenMatrix: (matrixId: string) => void;
  toast: NotifyFn;
  /** Choosing "用在矩阵里" for a new template opens the new-matrix flow. */
  onNewMatrix: () => void;
  /** Extraction-table [查看提取结果] opens that table's result view. */
  onViewRecords: (dt: KbDocTypeDTO) => void;
}

function kindOf(dt: KbDocTypeDTO): Kind {
  return (dt.kind as Kind | undefined) ?? "manual";
}

export function TemplateManager({
  workspaceId,
  canEdit,
  uid,
  isAdmin,
  onApplyTemplate,
  onOpenMatrix,
  toast,
  onNewMatrix,
  onViewRecords,
}: TemplateManagerProps) {
  const t = useTranslations("kbMatrix");
  const td = useTranslations("kt");
  const tn = useTranslations("knowledge_base.nav_v2");
  const [purposeOpen, setPurposeOpen] = useState(false);
  const [wizard, setWizard] = useState<{ mode: "create" | "edit"; dt?: KbDocTypeDTO } | null>(null);
  // Wizard focus-return target: "New template" for create, the edit control (or the row menu trigger) for edit.
  const wizardTriggerRef = useRef<HTMLElement | null>(null);
  const newTemplateRef = useRef<HTMLButtonElement | null>(null);
  const isMobile = useIsMobile();
  const [items, setItems] = useState<KbDocTypeDTO[]>([]);
  const [recordCounts, setRecordCounts] = useState<Record<string, number>>({});
  // null = matrices not fetched (API failure/BE flag off); omit source row rather than fabricate a tombstone.
  const [matrices, setMatrices] = useState<MatrixListItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [drawer, setDrawer] = useState<KbDocTypeDTO | null>(null);
  const [del, setDel] = useState<KbDocTypeDTO | null>(null);
  const [busy, setBusy] = useState(false);
  // M5 team library
  const [teamEntries, setTeamEntries] = useState<TeamTemplateDTO[]>([]);
  const [teamError, setTeamError] = useState(false);
  // Initially false: reloadTeam does not run without ws, so do not leave the skeleton stuck (review #3);
  // with ws, reloadTeam immediately sets teamLoading(true) to start the skeleton.
  const [teamLoading, setTeamLoading] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [pubBusy, setPubBusy] = useState(false);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const [copyToast, setCopyToast] = useState<{ name: string; dt: KbDocTypeDTO } | null>(null);
  // Review P3: late in-flight responses from the previous Space must not replace new-Space data (generation token).
  const reloadGen = useRef(0);
  const teamGen = useRef(0);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flashCopy = useCallback((c: { name: string; dt: KbDocTypeDTO }) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setCopyToast(c);
    toastTimer.current = setTimeout(() => setCopyToast(null), 6000);
  }, []);
  // Clear Toast timers on unmount/Space changes (review P3: avoid setState-on-unmounted).
  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  const reload = useCallback(async () => {
    const gen = ++reloadGen.current;
    const [{ items: dts, stats }, mxs] = await Promise.all([
      listDocTypesWithStats(workspaceId),
      listMatrices(workspaceId).catch(() => null),
    ]);
    if (gen !== reloadGen.current) return;
    setItems(dts);
    setRecordCounts(Object.fromEntries(dts.map((d) => [d.doc_type_id, stats[d.doc_type_id]?.total ?? d.record_count])));
    setMatrices(mxs);
  }, [workspaceId]);

  // Fetch team library independently (never block personal templates; own loading skeleton + generation token).
  // Rollout resilience in both flag states: BE flag off / endpoint 404 → teamError (not whole-page failure; review P3 latency decoupling).
  // silent=true (post-action refresh): no skeleton, retain teamEntries and update in place, preventing the entire
  // grid flashing into skeletons after copying (review #2). Initial fetch / Retry uses silent=false to show the skeleton.
  const reloadTeam = useCallback(
    async (silent = false) => {
      const gen = ++teamGen.current;
      if (!silent) setTeamLoading(true);
      try {
        const team = await listTeamTemplates(workspaceId);
        if (gen !== teamGen.current) return;
        setTeamEntries(team);
        setTeamError(false);
      } catch {
        if (gen === teamGen.current) setTeamError(true);
      } finally {
        if (gen === teamGen.current) setTeamLoading(false);
      }
    },
    [workspaceId],
  );

  // Refresh silently after publish/unpublish/copy/delete; no Toast (each flow supplies success copy),
  // avoiding success Toast + failed-reload error Toast together (review P3).
  const refresh = useCallback(async () => {
    try {
      await reload();
    } catch {
      /* Page errors Toast only during initial effect; silent post-action refresh failures recover on the next fetch. */
    }
    await reloadTeam(true); // Silent: update team library in place without flashing skeletons.
  }, [reload, reloadTeam]);

  useEffect(() => {
    if (!workspaceId) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    reload()
      .catch((e) => !cancelled && toast("error", td(kbDocTypeError(e, "load"))))
      .finally(() => !cancelled && setLoading(false));
    void reloadTeam();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, reload, reloadTeam, toast, td]);

  const seeds = useMemo(() => items.filter((d) => kindOf(d) === "seed"), [items]);
  const mine = useMemo(
    () =>
      items
        .filter((d) => kindOf(d) !== "seed" && kindOf(d) !== "manual")
        .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? "")),
    [items],
  );
  // Extraction tables (kind=manual) form a separate group (interaction design 05 · C).
  const extraction = useMemo(
    () => items.filter((d) => kindOf(d) === "manual").sort((a, b) => a.name.localeCompare(b.name)),
    [items],
  );
  // source_doc_type_id → library entry (rename-proof matching, design review P1).
  const entryBySource = useMemo(() => indexEntriesBySource(teamEntries), [teamEntries]);
  const pubStateOf = useCallback(
    (dt: KbDocTypeDTO): PublishState => publishStateOf(dt, entryBySource.get(dt.doc_type_id)),
    [entryBySource],
  );

  const srcMatrixOf = useCallback(
    (dt: KbDocTypeDTO): MatrixListItem | null =>
      dt.graduated_from_matrix_id && matrices ? (matrices.find((m) => m.matrixId === dt.graduated_from_matrix_id) ?? null) : null,
    [matrices],
  );
  const matricesLoaded = matrices !== null;

  const apply = useCallback(
    async (dt: KbDocTypeDTO) => {
      if (isColumnGroupKind(kindOf(dt))) {
        onApplyTemplate(dt);
        return;
      }
      setBusy(true);
      try {
        const res = await createMatrixFromTemplate(workspaceId, dt.doc_type_id);
        onOpenMatrix(res.matrix_id);
      } catch (e) {
        const message = kbMatrixMutationError(e);
        toast("error", message.values ? t(message.key, message.values) : t(message.key));
      } finally {
        setBusy(false);
      }
    },
    [workspaceId, onApplyTemplate, onOpenMatrix, toast, t],
  );

  const confirmDelete = useCallback(async () => {
    if (!del) return;
    setBusy(true);
    try {
      await deleteDocTypeTyped(del.doc_type_id);
      setDel(null);
      setDrawer(null);
      await refresh();
      toast("success", t("tpl_toast_deleted", { name: del.name }));
    } catch (e) {
      toast("error", td(kbDocTypeError(e, "delete")));
    } finally {
      setBusy(false);
    }
  }, [del, refresh, toast, t, td]);

  // ── M5 publish / unpublish flow ──
  const confirmPublish = useCallback(
    async (dt: KbDocTypeDTO, republish: boolean) => {
      setPubBusy(true);
      try {
        await publishTemplateToTeam(workspaceId, dt.doc_type_id);
        setDialog(null);
        await refresh();
        toast("success", republish ? t("tpl_toast_republished", { name: dt.name }) : t("tpl_toast_published", { name: dt.name }));
      } catch (e) {
        toast("error", t(kbTeamTemplateError(e, "publish")));
      } finally {
        setPubBusy(false);
      }
    },
    [workspaceId, refresh, toast, t],
  );

  const confirmUnpublish = useCallback(
    async (dt: KbDocTypeDTO, entry: TeamTemplateDTO | undefined) => {
      if (!entry) return;
      setPubBusy(true);
      try {
        await revokeTeamTemplate(entry.entry_id);
        setDialog(null);
        await refresh();
        toast("success", t("tpl_toast_unpublished", { name: dt.name }));
      } catch (e) {
        toast("error", t(kbTeamTemplateError(e, "revoke")));
      } finally {
        setPubBusy(false);
      }
    },
    [refresh, toast, t],
  );

  // ── M5 copy flow (rename on conflict + [去套用] deep link) ──
  const onCopy = useCallback(
    async (e: TeamTemplateDTO) => {
      setCopyingId(e.entry_id);
      try {
        const dt = await copyTeamTemplate(workspaceId, e.entry_id);
        flashCopy({ name: dt.name ?? "", dt });
        await refresh();
      } catch (err) {
        if (err instanceof MatrixApiError && err.code === "doc_type_name_taken") {
          setDialog({ kind: "collision", entryId: e.entry_id, attempted: e.name, suggestion: String(err.payload.suggestion ?? e.name), error: null });
        } else {
          toast("error", t(kbTeamTemplateError(err, "copy")));
        }
      } finally {
        setCopyingId(null);
      }
    },
    [workspaceId, refresh, toast, t, flashCopy],
  );

  const confirmCopy = useCallback(
    async (entryId: string, name: string) => {
      setPubBusy(true);
      try {
        const dt = await copyTeamTemplate(workspaceId, entryId, name);
        setDialog(null);
        flashCopy({ name: dt.name ?? "", dt });
        await refresh();
      } catch (err) {
        if (err instanceof MatrixApiError && err.code === "doc_type_name_taken") {
          // Another name conflict: refresh suggested name + keep error in dialog.
          setDialog((d) =>
            d && d.kind === "collision"
              ? { ...d, attempted: name, suggestion: String(err.payload.suggestion ?? name), error: t("otpl_copy_conflict_reagain", { name }) }
              : d,
          );
        } else {
          setDialog(null);
          toast("error", t(kbTeamTemplateError(err, "copy")));
        }
      } finally {
        setPubBusy(false);
      }
    },
    [workspaceId, refresh, toast, t, flashCopy],
  );

  // Publication-state actions (for TplCard/TplDrawer).
  const onPublish = useCallback((dt: KbDocTypeDTO) => setDialog({ kind: "publish", dt }), []);
  const onRepublish = useCallback((dt: KbDocTypeDTO) => setDialog({ kind: "republish", dt }), []);
  const onUnpublish = useCallback((dt: KbDocTypeDTO) => setDialog({ kind: "unpublish", dt, entry: entryBySource.get(dt.doc_type_id) }), [entryBySource]);

  // ── 412 mobile: read-only list (+ M5 team-library read-only browsing) ──
  if (isMobile) {
    const groups: Array<[string, KbDocTypeDTO[]]> = [
      [t("tpl_group_seed"), seeds],
      [t("tpl_tab_mine"), mine],
    ];
    return (
      <div className="mx-mobile" data-testid="kb-matrix-tplmgr-mobile" style={{ minHeight: 640 }}>
        {loading ? (
          <div style={{ padding: 24, textAlign: "center", color: "var(--c-ink-3)" }}>{t("loading")}</div>
        ) : (
          <>
            {groups.map(([label, list], gi) => (
              <div key={gi}>
                <div
                  style={{
                    fontFamily: "var(--mono)",
                    fontSize: "0.65625rem",
                    letterSpacing: "0.06em",
                    textTransform: "uppercase",
                    color: "var(--c-ink-4)",
                    padding: "14px 18px 2px",
                  }}
                >
                  {label}
                </div>
                {list.map((dt) => {
                  const k = kindOf(dt);
                  return (
                    <div className="mx-tpl-mcard" key={dt.doc_type_id}>
                      <div className="top">
                        <span className="nm">{dt.name}</span>
                        <span className={"kb-kind " + k}>{t(`kind_${k}` as never)}</span>
                      </div>
                      <div className="meta">
                        {t("tpl_fields_n", { n: dt.fields.length })}
                        {k === "graduated" ? ` · ${t("tpl_rows_query", { n: dt.row_count ?? 0 })}` : ""}
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
            <div className="tgh">
              <span className="t">{t("tpl_tab_team")}</span>
              <span className="n">{teamEntries.length}</span>
            </div>
            <MobileTeamLibrary entries={teamEntries} />
            <div className="kb-tpl-group">
              <ExtractionTemplateGroup
                items={extraction}
                recordCounts={recordCounts}
                // Mobile at 412 is read-only: no edit/delete, matching other mobile groups on this page.
                uid={null}
                isAdmin={false}
                busy={false}
                onEdit={() => undefined}
                onDelete={() => undefined}
                onViewRecords={onViewRecords}
              />
            </div>
          </>
        )}
        <div style={{ height: 20 }} />
      </div>
    );
  }

  const dialogDt = dialog && "dt" in dialog ? dialog.dt : null;

  return (
    <div className="mx-tpl-page nav" data-testid="kb-matrix-tplmgr" style={{ position: "relative" }}>
      <div className="viewhd">
        <span className="d">{tn("templates_desc")}</span>
        {canEdit && (
          <span className="a">
            <button ref={newTemplateRef} type="button" className="kb-btn kb-btn-primary" data-testid="kb-tpl-new" onClick={() => setPurposeOpen(true)}>
              {tn("new_template")}
            </button>
          </span>
        )}
      </div>

      {loading ? (
        <div className="emptyline">{t("loading")}</div>
      ) : (
        <div data-testid="kb-tpl-flat">
          {/* Knowledge sidebar "模板" (interaction design 05 · C): flat groups built-in → personal templates → team template library → extraction templates. */}
          <section className="tgrp" data-testid="kb-tpl-group-seed">
            <GroupHead title={t("tpl_group_seed")} count={seeds.length} desc={tn("group_seed_desc")} />
            {seeds.length === 0 ? (
              <div className="emptyline">{tn("group_seed_empty")}</div>
            ) : (
              <div className="kb-tpl-grid">
                {seeds.map((dt) => (
                  <TplCard
                    key={dt.doc_type_id}
                    dt={dt}
                    records={recordCounts[dt.doc_type_id] ?? 0}
                    srcMatrix={null}
                    matricesLoaded={matricesLoaded}
                    canEdit={canEdit}
                    uid={uid}
                    isAdmin={isAdmin}
                    busy={busy}
                    pubState="none"
                    pubBusy={false}
                    onOpen={setDrawer}
                    onApply={apply}
                    onDelete={null}
                    onOpenMatrix={onOpenMatrix}
                    onPublish={onPublish}
                    onUnpublish={onUnpublish}
                    onRepublish={onRepublish}
                  />
                ))}
              </div>
            )}
          </section>
          <section className="tgrp" id="kb-tpl-group-mine" data-testid="kb-tpl-group-mine">
            <GroupHead title={t("tpl_tab_mine")} count={mine.length} desc={tn("group_mine_desc")} />
            {mine.length === 0 ? (
              <div className="emptyline">{tn("group_mine_empty")}</div>
            ) : (
              <div className="kb-tpl-grid">
                {mine.map((dt) => (
                  <TplCard
                    key={dt.doc_type_id}
                    dt={dt}
                    records={recordCounts[dt.doc_type_id] ?? 0}
                    srcMatrix={srcMatrixOf(dt)}
                    matricesLoaded={matricesLoaded}
                    canEdit={canEdit}
                    uid={uid}
                    isAdmin={isAdmin}
                    busy={busy}
                    pubState={pubStateOf(dt)}
                    pubBusy={pubBusy && dialogDt?.doc_type_id === dt.doc_type_id}
                    onOpen={setDrawer}
                    onApply={apply}
                    onDelete={setDel}
                    onOpenMatrix={onOpenMatrix}
                    onPublish={onPublish}
                    onUnpublish={onUnpublish}
                    onRepublish={onRepublish}
                  />
                ))}
              </div>
            )}
          </section>
          <section className="tgrp" data-testid="kb-tpl-group-team">
            <GroupHead title={t("tpl_tab_team")} count={teamEntries.length} desc={tn("group_team_desc")} />
            <TeamLibraryView
              entries={teamEntries}
              loading={teamLoading}
              error={teamError}
              canCopy={canEdit}
              copyingId={copyingId}
              onCopy={onCopy}
              onViewMine={() => document.getElementById("kb-tpl-group-mine")?.scrollIntoView({ behavior: "smooth", block: "start" })}
              onRetry={() => void reloadTeam()}
              inlineEmpty={tn("group_team_empty")}
            />
          </section>
          <ExtractionTemplateGroup
            items={extraction}
            recordCounts={recordCounts}
            uid={uid}
            isAdmin={isAdmin}
            busy={busy}
            onEdit={(dt, trigger) => { wizardTriggerRef.current = trigger; setWizard({ mode: "edit", dt }); }}
            onDelete={setDel}
            onViewRecords={onViewRecords}
          />
          <div style={{ height: 24 }} />
        </div>
      )}

      {purposeOpen && (
        <TemplatePurposeDialog
          onClose={() => setPurposeOpen(false)}
          onPick={(purpose) => {
            setPurposeOpen(false);
            if (purpose === "matrix") onNewMatrix();
            else { wizardTriggerRef.current = newTemplateRef.current; setWizard({ mode: "create" }); }
          }}
        />
      )}
      {wizard && (
        <DraftWizard
          mode={wizard.mode}
          existing={wizard.dt}
          returnFocusRef={wizardTriggerRef}
          onClose={() => setWizard(null)}
          onSaved={async () => {
            setWizard(null);
            await refresh();
          }}
        />
      )}

      {/* Drawer (reuse mx-panel shell). */}
      <div className={"mx-panelwrap" + (drawer ? " open" : "")} onClick={() => !busy && setDrawer(null)}>
        <div className="mx-panel-scrim" />
        {drawer && (
          <TplDrawer
            dt={drawer}
            canEdit={canEdit}
            uid={uid}
            isAdmin={isAdmin}
            busy={busy}
            pubState={pubStateOf(drawer)}
            onClose={() => setDrawer(null)}
            onApply={apply}
            onSaved={async (updated) => {
              setDrawer(updated);
              await refresh();
              toast("success", t("tpl_toast_saved"));
            }}
            setBusy={setBusy}
            toast={toast}
            onPublish={onPublish}
            onUnpublish={onUnpublish}
            onRepublish={onRepublish}
          />
        )}
      </div>

      {/* delete confirm */}
      {del && (
        <DialogShell
          onClose={() => setDel(null)}
          busy={busy}
          label={t("tpl_delete_title", { name: del.name })}
          backdropClassName="kb-overlay"
          panelClassName="kb-modal"
        >
          <div className="kb-modal-head">
            <h3>{t("tpl_delete_title", { name: del.name })}</h3>
          </div>
          <div className="kb-modal-body">
            <p style={{ fontSize: "0.8125rem", color: "var(--c-ink-2)", lineHeight: 1.6, margin: 0 }}>
              {kindOf(del) === "graduated"
                ? t("tpl_delete_grad_body", { n: del.row_count ?? 0 })
                : kindOf(del) === "manual"
                  ? tn("extraction_delete_body")
                  : t("tpl_delete_body")}
            </p>
          </div>
          <div className="kb-modal-foot">
            <button type="button" className="kb-btn kb-btn-ghost" disabled={busy} onClick={() => setDel(null)}>
              {t("cancel")}
            </button>
            <button
              type="button"
              className="kb-btn kb-btn-danger-solid"
              data-testid="kb-matrix-tpl-delete-confirm"
              disabled={busy}
              onClick={() => void confirmDelete()}
            >
              {t("tpl_delete_cta")}
            </button>
          </div>
        </DialogShell>
      )}

      {/* M5 publish / unpublish / name-conflict dialogs. */}
      {dialog && (
        <>
          {(dialog.kind === "publish" || dialog.kind === "republish") && (
            <PublishConfirmDialog
              mode={dialog.kind}
              fieldCount={dialog.dt.fields.length}
              submitting={pubBusy}
              onClose={() => setDialog(null)}
              onConfirm={() => void confirmPublish(dialog.dt, dialog.kind === "republish")}
            />
          )}
          {dialog.kind === "unpublish" && (
            <UnpublishConfirmDialog
              name={dialog.dt.name}
              submitting={pubBusy}
              onClose={() => setDialog(null)}
              onConfirm={() => void confirmUnpublish(dialog.dt, dialog.entry)}
            />
          )}
          {dialog.kind === "collision" && (
            <CopyCollisionDialog
              name={dialog.attempted}
              suggestion={dialog.suggestion}
              error={dialog.error}
              submitting={pubBusy}
              onClose={() => setDialog(null)}
              onConfirm={(nm) => void confirmCopy(dialog.entryId, nm)}
            />
          )}
        </>
      )}

      {/* M5 copy-success Toast ([去套用] → ?from_dt= apply flow). */}
      {copyToast && (
        <CopyToast
          message={t.rich("otpl_toast_copied", { name: copyToast.name, b: (c) => <b>{c}</b> })}
          cta={t("otpl_toast_copied_cta")}
          onCta={() => {
            const dt = copyToast.dt;
            setCopyToast(null);
            onApplyTemplate(dt);
          }}
        />
      )}
    </div>
  );
}

/** Flat group header in sidebar "模板": name + count + one-line explanation (interaction design §5 template-group explanation). */
function GroupHead({ title, count, desc }: { title: string; count: number; desc: string }) {
  return (
    <div className="tgh">
      <span className="t">{title}</span>
      <span className="n">{count}</span>
      <span className="d">{desc}</span>
    </div>
  );
}

// ─── card ───────────────────────────────────────────────────

function TplCard({
  dt,
  records,
  srcMatrix,
  matricesLoaded,
  canEdit,
  uid,
  isAdmin,
  busy,
  pubState,
  pubBusy,
  onOpen,
  onApply,
  onDelete,
  onOpenMatrix,
  onPublish,
  onUnpublish,
  onRepublish,
}: {
  dt: KbDocTypeDTO;
  records: number;
  srcMatrix: MatrixListItem | null;
  /** false = matrices not fetched (API failure/BE flag off); omit source row rather than fabricate a tombstone. */
  matricesLoaded: boolean;
  canEdit: boolean;
  uid: string | null;
  isAdmin: boolean;
  busy: boolean;
  pubState: PublishState;
  pubBusy: boolean;
  onOpen: (dt: KbDocTypeDTO) => void;
  onApply: (dt: KbDocTypeDTO) => void;
  onDelete: ((dt: KbDocTypeDTO) => void) | null;
  onOpenMatrix: (matrixId: string) => void;
  onPublish: (dt: KbDocTypeDTO) => void;
  onUnpublish: (dt: KbDocTypeDTO) => void;
  onRepublish: (dt: KbDocTypeDTO) => void;
}) {
  const t = useTranslations("kbMatrix");
  const k = kindOf(dt);
  const isGrad = k === "graduated";
  // Deletion uses server admin-or-creator gate; noncreators/nonadmins receive 403 even as Space members,
  // so hide the button beforehand (seed is never deletable; onDelete already null).
  const canManage = canManageTemplate(dt.created_by, uid, isAdmin);
  // Publish entry only for kind=template + canManage (server hard kind gate + admin-or-creator).
  const canPublish = k === "template" && canManage;
  const published = canPublish && (pubState === "published" || pubState === "drift");
  return (
    <div
      className="kb-tpl-card"
      data-testid={`kb-matrix-tpl-${dt.doc_type_id}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(dt)}
      onKeyDown={(e) => e.key === "Enter" && onOpen(dt)}
    >
      <div className="top">
        <span className="nm">{dt.name}</span>
        {published && <PublishedBadge />}
        <span className={"kb-kind " + k}>{t(`kind_${k}` as never)}</span>
      </div>
      <span className="fields">
        {t("tpl_fields_n", { n: dt.fields.length })}
        {k === "manual" ? ` · ${t("tpl_records_n", { n: records })}` : ""}
      </span>
      {isGrad && (
        <span className="datarow">
          <span className="ic">
            <IcGrid s={12} />
          </span>
          {t("tpl_rows_query", { n: dt.row_count ?? 0 })}
        </span>
      )}
      {isGrad &&
        matricesLoaded &&
        (srcMatrix ? (
          <span className="src">
            {t.rich("tpl_src_from", {
              name: srcMatrix.name,
              a: (chunks) => (
                <a
                  role="button"
                  tabIndex={0}
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenMatrix(srcMatrix.matrixId);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.stopPropagation();
                      onOpenMatrix(srcMatrix.matrixId);
                    }
                  }}
                >
                  {chunks}
                </a>
              ),
            })}
          </span>
        ) : (
          <span className="src dead">{t("tpl_src_dead")}</span>
        ))}
      {canPublish && pubState === "drift" && (
        <span style={{ marginTop: 8 }}>
          <DriftFlag />
        </span>
      )}
      <div className="acts">
        {canEdit && (
          <button
            type="button"
            className="kb-btn kb-btn-secondary kb-btn-sm"
            disabled={busy}
            onClick={(e) => {
              e.stopPropagation();
              void onApply(dt);
            }}
          >
            {t("tpl_apply")}
          </button>
        )}
        {canPublish && pubState === "none" && (
          <button
            type="button"
            className="kb-btn kb-btn-secondary kb-btn-sm"
            data-testid={`kb-otpl-publish-${dt.doc_type_id}`}
            onClick={(e) => {
              e.stopPropagation();
              onPublish(dt);
            }}
          >
            <IcShare s={13} />
            {t("tpl_publish")}
          </button>
        )}
        {canPublish && pubState === "published" && (
          <button
            type="button"
            className="kb-btn kb-btn-ghost kb-btn-sm"
            disabled={pubBusy}
            onClick={(e) => {
              e.stopPropagation();
              onUnpublish(dt);
            }}
          >
            {t("tpl_unpublish")}
          </button>
        )}
        {canPublish && pubState === "drift" && (
          <button
            type="button"
            className="kb-btn kb-btn-secondary kb-btn-sm kb-btn-republish"
            onClick={(e) => {
              e.stopPropagation();
              onRepublish(dt);
            }}
          >
            <IcRefresh s={13} />
            {t("tpl_republish")}
          </button>
        )}
        <span className="grow" />
        <span className="updated">{dt.updated_at ? t("tpl_updated", { date: dt.updated_at.slice(0, 10) }) : ""}</span>
        {onDelete && canManage && (
          <button
            type="button"
            className="mx-mini"
            aria-label={t("tpl_delete_cta")}
            data-testid={`kb-matrix-tpl-del-${dt.doc_type_id}`}
            onClick={(e) => {
              e.stopPropagation();
              onDelete(dt);
            }}
          >
            <IcDots s={16} />
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Drawer (detail/edit: graduated fields read-only versus editable) ───

function TplDrawer({
  dt,
  canEdit,
  uid,
  isAdmin,
  busy,
  pubState,
  onClose,
  onApply,
  onSaved,
  setBusy,
  toast,
  onPublish,
  onUnpublish,
  onRepublish,
}: {
  dt: KbDocTypeDTO;
  canEdit: boolean;
  uid: string | null;
  isAdmin: boolean;
  busy: boolean;
  pubState: PublishState;
  onClose: () => void;
  onApply: (dt: KbDocTypeDTO) => void;
  onSaved: (updated: KbDocTypeDTO) => Promise<void>;
  setBusy: (v: boolean) => void;
  toast: NotifyFn;
  onPublish: (dt: KbDocTypeDTO) => void;
  onUnpublish: (dt: KbDocTypeDTO) => void;
  onRepublish: (dt: KbDocTypeDTO) => void;
}) {
  const t = useTranslations("kbMatrix");
  const td = useTranslations("kt");
  const k = kindOf(dt);
  const grad = k === "graduated";
  const canManage = canManageTemplate(dt.created_by, uid, isAdmin);
  const canPublish = k === "template" && canManage;
  // FE-2 (follow-on from action #2): seed is entirely read-only; server PUT requires admin-or-creator, so member edits receive 403.
  const readOnly = k === "seed" || !canManage;
  const [name, setName] = useState(dt.name);
  const [desc, setDesc] = useState(dt.description ?? "");
  const [nameErr, setNameErr] = useState<string | null>(null);
  const [optDraft, setOptDraft] = useState<Record<string, string[]>>({});

  useEffect(() => {
    setName(dt.name);
    setDesc(dt.description ?? "");
    setOptDraft({});
    setNameErr(null);
  }, [dt]);

  const optionFields = grad || readOnly ? [] : dt.fields.filter((f) => f.type === "select" || f.type === "multi_select");
  const optionsChanged = Object.keys(optDraft).length > 0;
  const optionsEmpty = Object.values(optDraft).some((opts) => opts.length === 0);
  const dirty = name.trim() !== dt.name || desc.trim() !== (dt.description ?? "") || optionsChanged;

  const save = async () => {
    if (readOnly || busy || !name.trim()) return;
    setBusy(true);
    setNameErr(null);
    try {
      const patch: { name?: string; description?: string; fields?: KbDocTypeDTO["fields"] } = {};
      if (name.trim() !== dt.name) patch.name = name.trim();
      if (desc.trim() !== (dt.description ?? "")) patch.description = desc.trim();
      if (!grad && optionsChanged) {
        patch.fields = dt.fields.map((f) => (optDraft[f.name] ? { ...f, options: optDraft[f.name] } : f));
      }
      const updated = await updateDocTypeMeta(dt.doc_type_id, patch);
      await onSaved(updated);
    } catch (e) {
      const errorKey = kbDocTypeError(e, "edit");
      if (errorKey === "errors.name_taken") setNameErr(td(errorKey));
      else toast("error", td(errorKey));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-panel tpl" data-testid="kb-matrix-tpl-drawer" onClick={(e) => e.stopPropagation()}>
      <div className="mx-panel-head">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="col">{dt.name}</div>
          <div className="file" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className={"kb-kind " + k}>{t(`kind_${k}` as never)}</span>
            {canPublish && (pubState === "published" || pubState === "drift") && <PublishedBadge />}
            {canPublish && pubState === "drift" && <DriftFlag />}
          </div>
        </div>
        <button type="button" className="kb-x" aria-label={t("close")} onClick={onClose}>
          <IcX s={16} />
        </button>
      </div>
      <div className="mx-panel-body">
        {canPublish && pubState === "drift" && (
          <div className="mx-lockbar" style={{ marginTop: 0 }}>
            <span className="ic">
              <IcRefresh s={14} />
            </span>
            {t("tpl_drift_lockbar")}
          </div>
        )}
        <div className="mx-flabel" style={{ marginTop: canPublish && pubState === "drift" ? 4 : 0 }}>
          {t("grad_name_label")}
        </div>
        <input
          className="mx-inp"
          value={name}
          disabled={readOnly}
          maxLength={200}
          onChange={(e) => {
            setName(e.target.value);
            setNameErr(null);
          }}
          style={nameErr ? { borderColor: "var(--c-err)" } : undefined}
        />
        {nameErr && (
          <div className="mx-fielderr" style={{ marginTop: 6 }}>
            {nameErr}
          </div>
        )}
        <div className="mx-flabel">{t("tpl_desc_label")}</div>
        <input className="mx-inp" value={desc} disabled={readOnly} maxLength={2000} placeholder={t("tpl_desc_ph")} onChange={(e) => setDesc(e.target.value)} />

        {grad && (
          <div className="mx-lockbar" style={{ marginTop: 16 }}>
            <span className="ic">
              <IcClock s={14} />
            </span>
            {t("tpl_lockbar")}
          </div>
        )}

        <div className="mx-flabel">{t("tpl_fields_label", { n: dt.fields.length })}</div>
        <div className={grad ? "mx-fields-readonly" : ""}>
          {dt.fields.map((f) => (
            <div className="mx-fieldrow" key={f.name}>
              <span className="g">{TYPE_GLYPH[f.type] ?? "Aa"}</span>
              <span className="fn">{f.name}</span>
              <span className="ft">{t(`type_${f.type}` as never)}</span>
            </div>
          ))}
        </div>

        {optionFields.map((f) => (
          <div key={f.name} style={{ marginTop: 12 }}>
            <div className="mx-flabel">{t("tpl_options_label", { name: f.name })}</div>
            <OptionsEditor value={optDraft[f.name] ?? f.options ?? []} onChange={(opts) => setOptDraft((d) => ({ ...d, [f.name]: opts }))} />
          </div>
        ))}
      </div>
      <div className="mx-panel-foot">
        {!readOnly && (
          <button
            type="button"
            className="kb-btn kb-btn-primary"
            data-testid="kb-matrix-tpl-save"
            disabled={busy || !dirty || !name.trim() || optionsEmpty}
            onClick={() => void save()}
          >
            {t("save")}
          </button>
        )}
        {canEdit && (
          <button type="button" className="kb-btn kb-btn-secondary" disabled={busy} onClick={() => void onApply(dt)}>
            {t("tpl_apply")}
          </button>
        )}
        {canPublish && pubState === "none" && (
          <button type="button" className="kb-btn kb-btn-secondary" onClick={() => onPublish(dt)}>
            <IcShare s={13} />
            {t("tpl_publish")}
          </button>
        )}
        {canPublish && pubState === "published" && (
          <button type="button" className="kb-btn kb-btn-ghost" onClick={() => onUnpublish(dt)}>
            {t("tpl_unpublish")}
          </button>
        )}
        {canPublish && pubState === "drift" && (
          <button type="button" className="kb-btn kb-btn-secondary kb-btn-republish" onClick={() => onRepublish(dt)}>
            <IcRefresh s={13} />
            {t("tpl_republish")}
          </button>
        )}
        {/* Verbatim mockup (fidelity review P2): graduated foot has only [保存][套用]; editable/read-only modes also have [取消]. */}
        {!grad && (
          <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
            {t("cancel")}
          </button>
        )}
      </div>
    </div>
  );
}
