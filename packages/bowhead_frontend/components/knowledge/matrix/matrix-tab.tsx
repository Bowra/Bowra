"use client";

/**
 * Container for three KB left-navigation views “文档矩阵 / 提取结果 / 模板”: list ↔ main-view routing
 * (?tab=matrix&view=mx_*, ?tab=records&view=tpl_<doc_type_id>, ?tab=templates),
 * snapshot loading, keyboard navigation, side panel/floating sources, edit/verification actions.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { notify } from "@/lib/use-toast";
import { useAppRouter } from "@/lib/use-app-router";
import {
  type KbDocTypeDTO,
  editRecordField,
  listDocTypesWithStats,
  updateRecordVerifyStatus,
} from "@/lib/kb-knowledge-tables-api";
import { fieldsToColumns } from "@/lib/kb-matrix/graduate-api";
import { type MatrixListItem, listMatrices } from "@/lib/kb-matrix/snapshot-api";
import { patchMatrixCell, patchMatrixRow, setMatrixSnapshot, useMatrixSnapshot } from "@/lib/kb-matrix/store";
import {
  buildCell,
  docTypeIdFromViewId,
  listTemplateViews,
  loadTemplateSnapshot,
  resolveSpaceEditPerms,
  spaceEditPermsFrom,
} from "@/lib/kb-matrix/template-view";
import type { TemplateViewSummary } from "@/lib/kb-matrix/types";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { useSpaceSelection } from "@/lib/use-space";
import { kbRecordError } from "@/lib/kb-record-error";

import { type CiteHoverInfo, MatrixGrid } from "./matrix-grid";
import { CitePopover } from "./cite-popover";
import { IcBack } from "./matrix-icons";
import { MatrixSection, TemplateRecordsSection } from "./matrix-sections";
import { MatrixViewM2 } from "./matrix-view-m2";
import { NewMatrixFlow } from "./new-matrix-flow";
import { SidePanel } from "./side-panel";
import { TemplateManager } from "./template-manager";

import "./matrix.css";

type Focus = { fileId: string; columnId: string } | null;

/** “打开为矩阵” file_ids handoff (unbounded URL length → sessionStorage bridge). */
export const NEW_MATRIX_FILES_KEY = "kbMatrixNewFiles";

/** KB left-navigation (2b) item served by MatrixTab. */
export type MatrixSectionKey = "matrix" | "records" | "templates";

/** View URLs: mx_* / new flow belongs to “文档矩阵”, tpl_* to “提取结果”. */
function sectionUrl(section: MatrixSectionKey, extra = ""): string {
  return `/knowledge?tab=${section}${extra}`;
}

export function MatrixTab({ section }: { section: MatrixSectionKey }) {
  const t = useTranslations("kbMatrix");
  const tRecords = useTranslations("records");
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const viewId = searchParams?.get("view") ?? null;
  const newFlow = searchParams?.get("new") === "1";
  // M4 apply as matrix (decision 14): column-group template doc_type_id → new-flow prefilled columns.
  const fromDt = searchParams?.get("from_dt") ?? null;
  const isTplMgr = section === "templates";

  // Same space source as KB homepage: await hydration, follow header switching (previously read localStorage each render;
  // deep-link initial empty value never refetched when space settled).
  const { activeId, items: spaceItems, loading: spacesLoading } = useSpaceSelection();
  // Edit permission: app shell already fetched user, space list contains role; compute directly, no separate /auth/me or /auth/me/workspaces (FR8.2).
  const sessionMe = useOptionalAuthSession()?.me ?? null;
  const ws = activeId ?? "";
  const derivedPerms = useMemo(
    () => (sessionMe ? spaceEditPermsFrom(sessionMe, spaceItems.find((w) => w.id === ws)?.role) : null),
    [sessionMe, spaceItems, ws],
  );
  const hasDerivedPerms = derivedPerms !== null;
  const [views, setViews] = useState<TemplateViewSummary[]>([]);
  const [matrices, setMatrices] = useState<MatrixListItem[]>([]);
  const [docTypes, setDocTypes] = useState<KbDocTypeDTO[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingSnap, setLoadingSnap] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // review P3 (wave-2): fail-closed initial value; viewer deep link to template/list no longer flashes
  // edit controls (hide action area until resolved; ~100ms delay safer).
  const [canEdit, setCanEdit] = useState(false);
  // Server template-edit/delete gate admin-or-creator (stricter than space-member canEdit): expose current
  // uid + isAdmin so template manager hides noncreator buttons by created_by beforehand (rather than 403 after click).
  const [uid, setUid] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [focus, setFocus] = useState<Focus>(null);
  const [panel, setPanel] = useState<Focus>(null);
  const [citePop, setCitePop] = useState<CiteHoverInfo | null>(null);
  const [saving, setSaving] = useState(false);
  const toast = notify;
  const snapshot = useMatrixSnapshot();

  // ── list + role ──
  useEffect(() => {
    if (spacesLoading) return;
    if (!ws) {
      setLoadingList(false);
      return;
    }
    let cancelled = false;
    Promise.all([
      listTemplateViews(ws),
      listDocTypesWithStats(ws),
      hasDerivedPerms ? Promise.resolve(null) : resolveSpaceEditPerms(ws),
      // Matrix endpoint failure degrades “我的矩阵” to empty; extracted results continue (no whole-page error).
      listMatrices(ws).catch(() => [] as MatrixListItem[]),
    ])
      .then(([vs, dts, perms, mxs]) => {
        if (cancelled) return;
        setViews(vs);
        setDocTypes(dts.items);
        if (perms) {
          setCanEdit(perms.canEdit);
          setUid(perms.uid);
          setIsAdmin(perms.isAdmin);
        }
        setMatrices(mxs);
      })
      .catch((e) => !cancelled && setError(tRecords(kbRecordError(e, "load"))))
      .finally(() => !cancelled && setLoadingList(false));
    return () => {
      cancelled = true;
    };
  }, [ws, spacesLoading, viewId, newFlow, tRecords, hasDerivedPerms]);

  useEffect(() => {
    if (!derivedPerms) return;
    setCanEdit(derivedPerms.canEdit);
    setUid(derivedPerms.uid);
    setIsAdmin(derivedPerms.isAdmin);
  }, [derivedPerms]);

  const isMxView = !!viewId?.startsWith("mx_");

  // ── snapshot for the active view (M1 template path only) ──
  const activeDocType = useMemo(() => {
    const dtId = viewId ? docTypeIdFromViewId(viewId) : null;
    return dtId ? (docTypes.find((d) => d.doc_type_id === dtId) ?? null) : null;
  }, [viewId, docTypes]);

  useEffect(() => {
    if (isMxView) return; // MatrixViewM2 owns mx_* snapshot.
    setMatrixSnapshot(null);
    setPanel(null);
    setFocus(null);
    setError(null); // a previous view's transient failure must not poison this one (FE P2-1)
    if (!ws || !activeDocType) return;
    let cancelled = false;
    setLoadingSnap(true);
    loadTemplateSnapshot(ws, activeDocType)
      .then((snap) => !cancelled && setMatrixSnapshot(snap))
      .catch((e) => !cancelled && setError(tRecords(kbRecordError(e, "load"))))
      .finally(() => !cancelled && setLoadingSnap(false));
    return () => {
      cancelled = true;
    };
  }, [ws, activeDocType, isMxView, tRecords]);

  const openView = useCallback(
    (id: string) => {
      setError(null);
      const tab = id.startsWith("tpl_") ? "records" : "matrix";
      router.push(`/knowledge?tab=${tab}&view=${encodeURIComponent(id)}`); // push: Back returns to the list
    },
    [router],
  );
  const backToList = useCallback(() => {
    setError(null);
    router.replace(sectionUrl(section));
  }, [router, section]);
  const newMatrixUrl = (extra = "") => sectionUrl("matrix", `&new=1${extra}`);

  // Leaving the tab drops the module-level snapshot (large matrices linger otherwise).
  useEffect(() => () => setMatrixSnapshot(null), []);

  // Stable ref — an inline arrow here would defeat DataCell's memo on every
  // MatrixTab re-render (impl review FE P2-3).
  const onCellOpen = useCallback((fileId: string, columnId: string) => {
    setFocus({ fileId, columnId });
    setPanel({ fileId, columnId });
  }, []);

  // ── panel actions ──
  const panelRow = panel && snapshot ? (snapshot.rows.find((r) => r.fileId === panel.fileId) ?? null) : null;
  const panelCol = panel && snapshot ? (snapshot.columns.find((c) => c.columnId === panel.columnId) ?? null) : null;
  const panelCell = panelRow && panel ? panelRow.cells[panel.columnId] : undefined;

  const onSaveValue = useCallback(
    async (newValue: unknown) => {
      if (!panelRow || !panelCol) return;
      setSaving(true);
      try {
        const updated = await editRecordField(panelRow.recordId, ws, {
          field_name: panelCol.columnId,
          new_value: newValue,
        });
        const cell = buildCell(updated, panelCol.columnId, updated.extracted_at);
        if (cell) patchMatrixCell(panelRow.fileId, panelCol.columnId, cell);
        toast("success", t("toast_saved"));
      } catch (e) {
        toast("error", tRecords(kbRecordError(e, "edit")));
      } finally {
        setSaving(false);
      }
    },
    [panelRow, panelCol, ws, toast, t, tRecords],
  );

  const onMarkVerified = useCallback(async () => {
    if (!panelRow) return;
    setSaving(true);
    try {
      await updateRecordVerifyStatus(panelRow.recordId, ws, "approved");
      patchMatrixRow(panelRow.fileId, (r) => ({
        ...r,
        approved: true,
        cells: Object.fromEntries(
          // Decision 2: only done cells overlay ✓ (needs_review never pretends verified, FE P3-1).
          Object.entries(r.cells).map(([k, c]) => [k, c && c.status === "done" ? { ...c, verified: true } : c]),
        ),
      }));
      toast("success", t("toast_verified"));
    } catch (e) {
      toast("error", tRecords(kbRecordError(e, "status")));
    } finally {
      setSaving(false);
    }
  }, [panelRow, ws, toast, t, tRecords]);

  // Esc closes the panel regardless of where focus sits (design §5.4) — the
  // grid container's onKeyDown only fires while focus is inside it.
  // M3 §4.8: never blindly close while focus within input control (side-panel input handles Escape to exit editing).
  useEffect(() => {
    if (!panel) return;
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if ((e.target as HTMLElement | null)?.closest?.("input,textarea,select,[contenteditable]")) return;
      // Overlay above side panel (e.g. “查看原文” file details) handles own Esc, uppermost closes only.
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      setPanel(null);
    };
    document.addEventListener("keydown", onDocKey);
    return () => document.removeEventListener("keydown", onDocKey);
  }, [panel]);

  // ── keyboard: arrows move focus, Enter opens panel, Esc closes ──
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        if ((e.target as HTMLElement | null)?.closest?.("input,textarea,select,[contenteditable]")) return; // §4.8
        setPanel(null);
        return;
      }
      // Panel open → keystrokes belong to the panel (edit input caret would be
      // hijacked by grid navigation otherwise, impl review FE P1-2).
      if (panel) return;
      if (!snapshot) return;
      if (!focus) return;
      const ri = snapshot.rows.findIndex((r) => r.fileId === focus.fileId);
      const ci = snapshot.columns.findIndex((c) => c.columnId === focus.columnId);
      if (ri < 0 || ci < 0) return;
      let nr = ri;
      let nc = ci;
      if (e.key === "ArrowDown") nr = Math.min(snapshot.rows.length - 1, ri + 1);
      else if (e.key === "ArrowUp") nr = Math.max(0, ri - 1);
      else if (e.key === "ArrowRight") nc = Math.min(snapshot.columns.length - 1, ci + 1);
      else if (e.key === "ArrowLeft") nc = Math.max(0, ci - 1);
      else if (e.key === "Enter") {
        setPanel(focus);
        return;
      } else return;
      e.preventDefault();
      setFocus({ fileId: snapshot.rows[nr].fileId, columnId: snapshot.columns[nc].columnId });
    },
    [snapshot, focus],
  );

  if (error) {
    return (
      <div role="alert" style={{ padding: 24, color: "var(--c-err)" }}>
        {error}
        {viewId && (
          <div style={{ marginTop: 10 }}>
            <button type="button" className="kb-btn kb-btn-secondary" onClick={backToList}>
              {t("back")}
            </button>
          </div>
        )}
      </div>
    );
  }

  // ── new matrix flow (list “＋新建矩阵” / Files “打开为矩阵” prefill) ──
  if (newFlow) {
    const preselected =
      typeof window !== "undefined"
        ? ((): string[] => {
            try {
              return JSON.parse(sessionStorage.getItem(NEW_MATRIX_FILES_KEY) ?? "[]") as string[];
            } catch {
              return [];
            }
          })()
        : [];
    // M4 decision 14: ?from_dt=<doc_type_id> = apply template (column-group template → column prefill + skip template selection).
    // review P1: deep link/refresh before docTypes load → late preset strands flow;
    // mount after list loads (NewMatrixFlow derived effStep supplies second guard).
    if (fromDt && loadingList) {
      return <div style={{ padding: 24, textAlign: "center", color: "var(--c-ink-3)" }}>{t("loading")}</div>;
    }
    const fromDoc = fromDt ? (docTypes.find((d) => d.doc_type_id === fromDt) ?? null) : null;
    const preset = fromDoc ? { name: fromDoc.name, columns: fieldsToColumns(fromDoc.fields) } : undefined;
    return (
      <>
        {/* review P3: applied template deleted/invalid → disclose before degrading to ordinary creation (never silent). */}
        {fromDt && !fromDoc && (
          <div style={{ padding: "10px 24px 0", color: "var(--c-warn)", fontSize: "0.78125rem" }}>{t("tpl_apply_missing")}</div>
        )}
        <NewMatrixFlow
          workspaceId={ws}
          // review P2: from_dt application ignores stale sessionStorage selection (with zero-step footer,
          // would create against stale file set in one click).
          preselectedFileIds={fromDt ? [] : preselected}
          preset={preset}
          docTypes={docTypes}
          onCancel={() => {
            // review P2: cancel also clears selection residue (previously onCreated only; leftovers silently made later
            // ?new=1 entry skipFiles).
            if (typeof window !== "undefined") sessionStorage.removeItem(NEW_MATRIX_FILES_KEY);
            backToList();
          }}
          onCreated={(mxId) => {
            if (typeof window !== "undefined") sessionStorage.removeItem(NEW_MATRIX_FILES_KEY);
            openView(mxId);
          }}
          onError={(m) => toast("error", m)}
        />
      </>
    );
  }

  // ── M2 actual matrix view (mx_*) ──
  if (isMxView && viewId) {
    return <MatrixViewM2 key={viewId} matrixId={viewId} onBack={backToList} />;
  }

  // ── M4 template manager (?view=templates) ──
  if (isTplMgr) {
    return (
      <>
        <TemplateManager
          workspaceId={ws}
          canEdit={canEdit}
          uid={uid}
          isAdmin={isAdmin}
          onApplyTemplate={(dt) => router.push(newMatrixUrl(`&from_dt=${encodeURIComponent(dt.doc_type_id)}`))}
          onOpenMatrix={openView}
          toast={toast}
          onNewMatrix={() => router.push(newMatrixUrl())}
          onViewRecords={(dt) => openView(`tpl_${dt.doc_type_id}`)}
        />
      </>
    );
  }

  // ── list mode ──
  if (!viewId || !activeDocType) {
    return section === "records" ? (
      <TemplateRecordsSection views={views} loading={loadingList} onOpen={openView} />
    ) : (
      <MatrixSection
        matrices={matrices}
        loading={loadingList}
        canEdit={canEdit}
        onOpen={openView}
        onNew={() => router.push(newMatrixUrl())}
      />
    );
  }

  // ── main view mode ──
  const citeRow = citePop && snapshot ? (snapshot.rows.find((r) => r.fileId === citePop.fileId) ?? null) : null;
  const citeCell = citeRow && citePop ? citeRow.cells[citePop.columnId] : undefined;

  return (
    <div className="mx-app" style={{ outline: "none" }} tabIndex={0} onKeyDown={onKeyDown} data-testid="kb-matrix-app">
      <div className="mx-topbar">
        <button type="button" className="mx-back" aria-label={t("back")} onClick={backToList}>
          <IcBack s={16} />
        </button>
        <span className="mx-title">{snapshot?.title ?? activeDocType.name}</span>
        <span className="mx-meta">
          {snapshot ? t("meta", { rows: snapshot.rows.length, cols: snapshot.columns.length }) : ""}
        </span>
        <span className="grow" />
      </div>

      {loadingSnap || !snapshot ? (
        <div style={{ padding: 24, textAlign: "center", color: "var(--c-ink-3)" }}>{t("loading")}</div>
      ) : (
        <MatrixGrid snapshot={snapshot} focus={focus} onCell={onCellOpen} onCiteHover={setCitePop} />
      )}

      <div className="mx-gridfoot">
        <span>{snapshot ? t("foot", { rows: snapshot.rows.length }) : ""}</span>
        <span className="grow" />
      </div>

      {citePop && citeRow && citeCell && (
        <CitePopover anchor={citePop.anchor} fileId={citeRow.fileId} fileName={citeRow.fileName} cell={citeCell} />
      )}

      <div className={"mx-panelwrap" + (panel && panelCell ? " open" : "")} onClick={() => setPanel(null)}>
        <div className="mx-panel-scrim" />
        {panel && panelRow && panelCol && panelCell && (
          <SidePanel
            row={panelRow}
            col={panelCol}
            cell={panelCell}
            canEdit={canEdit}
            saving={saving}
            onClose={() => setPanel(null)}
            onSaveValue={onSaveValue}
            onMarkVerified={onMarkVerified}
          />
        )}
      </div>

    </div>
  );
}
