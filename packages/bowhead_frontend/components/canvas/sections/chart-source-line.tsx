"use client";

/** Neutral chart source linkage backed only by viewer-safe source projection. */

import { useState, type KeyboardEvent, type MouseEvent } from "react";
import {
  FloatingFocusManager,
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useDismiss,
  useFloating,
  useInteractions,
  useRole,
} from "@floating-ui/react";
import { useTranslations } from "next-intl";
import type { SourceRef } from "@/lib/canvas-pm-adapter";
import { appPath } from "@/lib/base-path";
import { useSourceAccess, useSourceAccessActions } from "@/lib/source-access-registry";
import { useNarrowViewport } from "@/components/ui/sheet";
import { DialogShell } from "@/components/ui/dialog";
import { CharIcon } from "@/components/char-icon";
import { sourceNoteStatement } from "@/lib/chart/contextual-evidence";
import type { EvidenceNote } from "@/lib/chart/presentation-plan";

export type DataSourceRef = SourceRef;

export function ChartSourceLine({
  source,
  dataSource = null,
  notes = [],
  measureLabels = {},
}: {
  source: SourceRef | null;
  dataSource?: DataSourceRef | null;
  notes?: EvidenceNote[];
  measureLabels?: Record<string, string>;
}) {
  const t = useTranslations("provenance");
  const [open, setOpen] = useState(false);
  const selectedSource = source ?? dataSource ?? notes[0]?.source_ref;
  const access = useSourceAccess(selectedSource ?? undefined);
  const { check } = useSourceAccessActions();
  const accessStatus = open && access?.generation === 0 ? "checking" : access?.status;
  // Report Canvas Chrome Spec §6 (layer requirements): the source card mounts on body (an in-place absolute card was
  // trapped in report stacking contexts, clipped near scroll edges and covered by the sticky toolbar). It uses --z-menu,
  // follows the anchor on scroll; Esc/outside clicks close, keyboard opening focuses the card, closing returns to the
  // source row; narrow screens (<640) use a bottom sheet.
  const narrow = useNarrowViewport(true);
  const floating = useFloating({
    open: open && !narrow,
    onOpenChange: setOpen,
    placement: "top-start",
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware: [offset(6), flip({ padding: 8 }), shift({ padding: 8 })],
  });
  const dismiss = useDismiss(floating.context);
  const role = useRole(floating.context, { role: "dialog" });
  const interactions = useInteractions([dismiss, role]);

  if (!selectedSource) {
    return (
      <div className="rc-source none">
        <span className="src-n">–</span>
        <span>{t("no_source")}</span>
      </div>
    );
  }

  // Batch 0 0-D: supply ordinal only when it matches the actual source-list number (projection
  // once hardcoded 1, making all 37 production source rows [1]). Missing means name only, without invented numbering.
  const ordinal = typeof selectedSource.ordinal === "number" ? selectedSource.ordinal : null;
  const accessibleRecord = accessStatus === "linked_accessible" && access ? access : null;
  const label = accessibleRecord
    ? accessibleRecord.source.safe_label || accessibleRecord.source.safe_query || t("source_linked")
    : accessStatus === "checking" ? t("source_checking") : t("source_unavailable");

  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    await Promise.all([check(selectedSource), ...notes.map((note) => check(note.source_ref))]);
  };

  const card = (
    <>
      <div className="rc-prov-head">
        <span className="rc-prov-tool" aria-hidden><CharIcon ch="↗" aria-hidden /></span>
        <div className="legacy-rc-prov-main">
          <div className="t-name">{label}</div>
          <div className="t-sub">{t("source_link_definition")}</div>
        </div>
      </div>
      {accessStatus === "checking" ? <div className="rc-prov-rows">{t("source_checking")}</div> : null}
      {accessStatus === "linked_unavailable" ? <div className="rc-prov-rows">{t("source_unavailable")}</div> : null}
      {accessibleRecord?.source.safe_url ? (
        <div className="rc-prov-rows">
          <a href={appPath(accessibleRecord.source.safe_url)} target="_blank" rel="noopener noreferrer">
            {t("open_source")}
          </a>
        </div>
      ) : null}
      {notes.length ? <div className="rc-prov-rows">
        <div className="t-sub">{t("notes_title")}</div>
        {notes.map((note) => <EvidenceNoteText key={note.id} note={note} measureLabels={measureLabels} primaryUrl={accessibleRecord?.source.safe_url} />)}
      </div> : null}
    </>
  );

  // React portal events still bubble through component tree to source row; card clicks/Enter must not toggle the row again.
  const stop = (event: MouseEvent | KeyboardEvent) => event.stopPropagation();
  return (
    <div
      ref={floating.refs.setReference}
      className="rc-source"
      data-source-access={accessStatus ?? "linked_unavailable"}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      {...interactions.getReferenceProps({
        onClick: () => void toggle(),
        onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            void toggle();
          }
        },
      })}
    >
      {ordinal !== null ? <span className="src-n">[{ordinal}]</span> : null}
      <span className="src-link">{label}</span>
      {open && !narrow ? (
        // External focus guards would mark adjacent editable prose as inert and cause ProseMirror to remount figures.
        <FloatingPortal preserveTabOrder={!floating.elements.domReference?.closest(".ProseMirror")}>
          <FloatingFocusManager context={floating.context} modal={false} initialFocus={0}>
            <div
              ref={floating.refs.setFloating}
              className="rc-prov rc-prov--floating"
              aria-label={label}
              tabIndex={-1}
              style={{ ...floating.floatingStyles, zIndex: "var(--z-menu)", cursor: "default",
                ...(notes.length ? { maxHeight: "calc(100dvh - 32px)", overflowY: "auto" } : {}) }}
              {...interactions.getFloatingProps({ onClick: stop, onKeyDown: stop })}
            >
              {card}
            </div>
          </FloatingFocusManager>
        </FloatingPortal>
      ) : null}
      {open && narrow ? (
        <DialogShell
          onClose={() => setOpen(false)}
          presentation="sheet"
          label={label}
          backdropClassName="settings-dialog-backdrop"
          panelClassName="rc-prov rc-prov--sheet"
          panelProps={{ onClick: stop, onKeyDown: stop }}
        >
          {card}
        </DialogShell>
      ) : null}
    </div>
  );
}

function EvidenceNoteText({ note, measureLabels, primaryUrl }: { note: EvidenceNote; measureLabels: Record<string, string>; primaryUrl?: string | null }) {
  const t = useTranslations("provenance");
  const access = useSourceAccess(note.source_ref);
  const available = note.availability === "available" && access?.status === "linked_accessible";
  const statement = available ? sourceNoteStatement(note) : null;
  return <div className="rc-prov-rows" data-evidence-note={note.id}>
    <div className="t-sub">{note.measure_keys.map((key) => measureLabels[key] ?? key).join(" · ")}</div>
    {available ? <>
      <p className="whitespace-pre-wrap break-words">{statement?.text ?? note.excerpt}</p>
      {statement ? <div className="t-sub">{statement.period}</div> : null}
      {note.locator ? <div className="t-sub">{note.locator}</div> : null}
      {access.source.safe_url && appPath(access.source.safe_url) !== (primaryUrl ? appPath(primaryUrl) : null) ? <a href={appPath(access.source.safe_url)} target="_blank" rel="noopener noreferrer">{t("open_source")}</a> : null}
    </> : <p>{t("note_unavailable")}</p>}
  </div>;
}
