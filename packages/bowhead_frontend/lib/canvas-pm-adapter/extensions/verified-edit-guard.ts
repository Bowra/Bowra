/**
 * VerifiedEditGuard — design D8: editing a verified numeric phrase de-anchors it, downgrades to unverified, and confirms.
 *
 * Background: current narrative numbers are plain text + adjacent [N] (citation-plugin citation-vnum
 * adjacency detection → verified underline+tick). If users directly edit a number ("962.7" → "999.9"),
 * the regex still matches and verified visuals remain, while the number no longer matches its source: de-anchoring risk.
 * A silently altered number retaining ✓ is more dangerous than an unmarked one.
 *
 * This guard makes verified numeric phrases atomic:
 *   - Detect input/deletion within a verified phrase (handleTextInput / Backspace·Delete)
 *   - Block this edit first (return true), then show the app confirmation (confirmEdit injected by ReportView)
 *   - Confirm → in one transaction: apply the user edit + delete [N] anchor (de-anchor,
 *     citation-vnum no longer matches) + add citation-downgraded decoration to the numeric range
 *     (dijon unverified visuals, mapped along with edits). Other transactions may alter the document while confirmation
 *     is open (autosave normalization, SSE full-document replacement, edits elsewhere). Map the editing range through
 *     intervening changes to the current doc; if the number/anchor changed or mapping fails, drop this edit and notify via onEditDropped
 *   - Cancel → this edit has no effect
 *
 * Only citation-vnum (plain number+[N]), the actual de-anchoring hazard. Editing [num:..:src:S] / [N]
 * markers directly breaks token syntax and naturally removes verification, outside this guard.
 *
 * Two protection layers:
 *  1) Confirm + de-anchor common paths: typing (handleTextInput, including IME commit),
 *     Backspace/Delete (handleKeyDown), paste replacement (handlePaste).
 *  2) appendTransaction fallback: cut Cmd/Ctrl+X, drag/drop, mid-IME,
 *     programmatic edits etc. bypassing those handlers; scan afterward for verified numbers whose values changed
 *     while [N] remains → delete anchor + downgrade (no confirmation, edit already occurred).
 * Layer 2 closes the safety invariant (no modified verified number retains ✓); layer 1 adds
 * pre-edit confirmation UX for frequent paths.
 */

import { Plugin, PluginKey, TextSelection } from "@tiptap/pm/state";
import type { EditorState, Transaction } from "@tiptap/pm/state";
import type { Node as PMNode, Slice } from "@tiptap/pm/model";
import { Mapping } from "@tiptap/pm/transform";
import type { EditorView } from "@tiptap/pm/view";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { Extension } from "@tiptap/core";
import { findCitedNumberRanges } from "./citation-plugin";

export const VerifiedGuardKey = new PluginKey("verifiedEditGuard");

interface VerifiedGuardOptions {
  /** numberedSources length (same source as citation-plugin, determines whether [N] has backing). */
  getNumberedSourcesLength: () => number;
  /** App confirmation (injected by ReportView, localized copy); resolve true = user confirms de-anchored editing. */
  confirmEdit: () => Promise<boolean>;
  /** User confirmed, but other changes replaced the number during confirmation; edit not applied (ReportView notifies). */
  onEditDropped: () => void;
}

/** Contents of one blocked edit awaiting confirmation. */
type PendingEditKind =
  | { kind: "text"; text: string }
  | { kind: "delete" }
  | { kind: "slice"; slice: Slice };

interface PendingEdit {
  /** All following positions refer to the document at interception time. */
  hit: VerifiedHit;
  from: number;
  to: number;
  edit: PendingEditKind;
  /** Original number, anchor and edited-range text at interception; compare each against the current doc after confirmation. */
  numberText: string;
  anchorText: string;
  editText: string;
  /** Mappings of all applied transactions since interception; doc is the document at the mapping-chain end. */
  mapping: Mapping;
  doc: PMNode;
}

interface VerifiedHit {
  /** Numeric phrase doc range. */
  from: number;
  to: number;
  /** Doc range of the " [N]" anchor; deleting it de-anchors the number. */
  anchorFrom: number;
  anchorTo: number;
}

const ANCHOR_RE = /^\s\[\d{1,3}\]/;

/**
 * Build textblock textContent plus a character→doc-position mapping.
 * Non-text inline nodes (hardBreak etc., size=1) occupy doc positions without entering textContent,
 * so naive parentStart+textIndex addition misaligns them. posAt[i] is the doc position of the ith textContent
 * character; posAt[text.length] is the block content end (sentinel).
 */
function inlineTextMap(
  block: import("@tiptap/pm/model").Node,
  blockContentStart: number,
): { text: string; posAt: number[] } {
  let text = "";
  const posAt: number[] = [];
  block.forEach((child, offset) => {
    if (child.isText && child.text) {
      for (let k = 0; k < child.text.length; k++) {
        text += child.text[k];
        posAt.push(blockContentStart + offset + k);
      }
    }
    // Non-text inline node: advance doc position without textContent → do not add to text/posAt
  });
  posAt.push(blockContentStart + block.content.size);
  return { text, posAt };
}

/** For a doc position within a verified numeric phrase, return its match (including [N] anchor range); otherwise null. */
function verifiedHitAt(
  state: EditorState,
  pos: number,
  numberedLen: number,
): VerifiedHit | null {
  if (numberedLen <= 0) return null;
  let $pos;
  try {
    $pos = state.doc.resolve(pos);
  } catch {
    return null;
  }
  const parent = $pos.parent;
  if (!parent || !parent.isTextblock) return null;
  const blockStart = $pos.start($pos.depth);
  const { text, posAt } = inlineTextMap(parent, blockStart);
  for (const r of findCitedNumberRanges(text, numberedLen)) {
    const docFrom = posAt[r.from];
    const docTo = posAt[r.to];
    if (pos < docFrom || pos > docTo) continue;
    const m = ANCHOR_RE.exec(text.slice(r.to));
    if (!m) continue;
    return {
      from: docFrom,
      to: docTo,
      anchorFrom: docTo,
      anchorTo: posAt[r.to + m[0].length],
    };
  }
  return null;
}

/** Scan all verified numeric phrase matches in the doc (including [N] anchor ranges), for appendTransaction fallback. */
function collectVerifiedHits(
  doc: import("@tiptap/pm/model").Node,
  numberedLen: number,
): VerifiedHit[] {
  const out: VerifiedHit[] = [];
  if (numberedLen <= 0) return out;
  doc.descendants((node, pos) => {
    if (!node.isTextblock) return true;
    const { text, posAt } = inlineTextMap(node, pos + 1);
    for (const r of findCitedNumberRanges(text, numberedLen)) {
      const m = ANCHOR_RE.exec(text.slice(r.to));
      if (!m) continue;
      out.push({
        from: posAt[r.from],
        to: posAt[r.to],
        anchorFrom: posAt[r.to],
        anchorTo: posAt[r.to + m[0].length],
      });
    }
    return false; // Do not descend into textblock inline children
  });
  return out;
}

/** Map intercepted ranges to the current doc; return null when their original text changed or a range was deleted. */
function mapRange(
  pending: PendingEdit,
  doc: PMNode,
  from: number,
  to: number,
  original: string,
): { from: number; to: number } | null {
  // Boundary insertions remain outside: start associates forward, end backward; empty ranges use the same association at both ends.
  const mappedFrom = pending.mapping.map(from, from === to ? -1 : 1);
  const mappedTo = pending.mapping.map(to, -1);
  if (mappedTo - mappedFrom !== to - from) return null;
  try {
    if (doc.textBetween(mappedFrom, mappedTo) !== original) return null;
  } catch {
    return null;
  }
  return { from: mappedFrom, to: mappedTo };
}

/**
 * In one transaction: delete [N] anchor + apply user edit + decorate numeric range as downgraded.
 * First map positions through changes during confirmation; if original number, anchor or edit-range text changed, return false without applying.
 */
function dispatchDeAnchor(view: EditorView, pending: PendingEdit): boolean {
  const doc = view.state.doc;
  if (pending.doc !== doc) return false; // Mapping chain broken (editor state replaced in full)
  const { hit } = pending;
  const number = mapRange(pending, doc, hit.from, hit.to, pending.numberText);
  const anchor = mapRange(pending, doc, hit.anchorFrom, hit.anchorTo, pending.anchorText);
  const edit = mapRange(pending, doc, pending.from, pending.to, pending.editText);
  if (!number || !anchor || !edit) return false;

  const tr = view.state.tr;
  // Delete [N] anchor first, then apply the user edit using coordinates after anchor deletion
  tr.delete(anchor.from, anchor.to);
  const editFrom = tr.mapping.map(edit.from, -1);
  const editTo = tr.mapping.map(edit.to, 1);
  if (pending.edit.kind === "text") {
    tr.insertText(pending.edit.text, editFrom, editTo);
  } else if (pending.edit.kind === "delete") {
    tr.delete(editFrom, editTo);
  } else {
    tr.setSelection(TextSelection.create(tr.doc, editFrom, editTo));
    tr.replaceSelection(pending.edit.slice);
  }
  // Downgrade decoration covers the mapped numeric range (dijon unverified)
  const from = tr.mapping.map(number.from);
  const to = tr.mapping.map(number.to);
  tr.setMeta(VerifiedGuardKey, { downgrade: { from, to } });
  view.dispatch(tr);
  return true;
}

export const VerifiedEditGuardExtension = Extension.create<VerifiedGuardOptions>({
  name: "verifiedEditGuard",

  addOptions() {
    return {
      getNumberedSourcesLength: () => 0,
      // Conservative without injected confirmation: block the edit; do not silently de-anchor.
      confirmEdit: () => Promise.resolve(false),
      onEditDropped: () => {},
    };
  },

  addProseMirrorPlugins() {
    const getLen = this.options.getNumberedSourcesLength;
    const confirmEdit = this.options.confirmEdit;
    const onEditDropped = this.options.onEditDropped;
    // Edit awaiting confirmation. Plugin state.apply appends each applied transaction mapping (see apply below).
    const pendingEdits = new Set<PendingEdit>();
    const trackPending = (tr: Transaction) => {
      for (const pending of pendingEdits) {
        if (tr.before !== pending.doc) continue; // Not attached to the mapping-chain end (unapplied branch)
        pending.mapping.appendMapping(tr.mapping);
        pending.doc = tr.doc;
      }
    };
    // Confirmation is asynchronous: block first, then map to the current doc and apply edit + de-anchor after confirmation.
    const confirmThenDeAnchor = (
      view: EditorView,
      hit: VerifiedHit,
      from: number,
      to: number,
      edit: PendingEditKind,
    ): true => {
      const doc = view.state.doc;
      let pending: PendingEdit;
      try {
        pending = {
          hit,
          from,
          to,
          edit,
          numberText: doc.textBetween(hit.from, hit.to),
          anchorText: doc.textBetween(hit.anchorFrom, hit.anchorTo),
          editText: doc.textBetween(from, to),
          mapping: new Mapping(),
          doc,
        };
      } catch {
        return true; // Invalid position: block without applying
      }
      pendingEdits.add(pending);
      void confirmEdit().then(
        (ok) => {
          pendingEdits.delete(pending);
          if (!ok || view.isDestroyed) return;
          if (!dispatchDeAnchor(view, pending)) {
            onEditDropped();
            return;
          }
          view.focus();
        },
        () => {
          pendingEdits.delete(pending);
        },
      );
      return true;
    };
    return [
      new Plugin({
        key: VerifiedGuardKey,
        state: {
          init: () => DecorationSet.empty,
          apply(tr, old) {
            trackPending(tr);
            let set = old.map(tr.mapping, tr.doc);
            const meta = tr.getMeta(VerifiedGuardKey) as
              | {
                  downgrade?: { from: number; to: number };
                  downgradeRanges?: { from: number; to: number }[];
                }
              | undefined;
            const ranges = [
              ...(meta?.downgrade ? [meta.downgrade] : []),
              ...(meta?.downgradeRanges ?? []),
            ];
            for (const { from, to } of ranges) {
              if (to > from) {
                set = set.add(tr.doc, [
                  Decoration.inline(from, to, {
                    class: "citation-downgraded",
                    "data-citation-kind": "downgraded",
                  }),
                ]);
              }
            }
            return set;
          },
        },
        // #17 fallback: vectors missed by typing/backspace/delete/paste handlers (cut Cmd+X / drag /
        // mid-IME / programmatic) alter a verified value; scan afterward for matches whose value changed but
        // [N] anchor remains, deleting anchor + downgrading. No confirmation on these paths (edit already occurred),
        // but leave no altered number retaining ✓, closing the D8 safety invariant.
        appendTransaction(transactions, oldState, newState) {
          if (!transactions.some((t) => t.docChanged)) return null;
          // Skip our own de-anchor transactions (VerifiedGuardKey meta) to prevent recursion
          if (transactions.some((t) => t.getMeta(VerifiedGuardKey))) return null;
          const len = getLen();
          if (len <= 0) return null;
          const oldHits = collectVerifiedHits(oldState.doc, len);
          if (oldHits.length === 0) return null;
          const toFix: { nf: number; nt: number; naf: number; nat: number }[] = [];
          for (const hit of oldHits) {
            let nf = hit.from, nt = hit.to, naf = hit.anchorFrom, nat = hit.anchorTo;
            for (const t of transactions) {
              nf = t.mapping.map(nf, 1);
              nt = t.mapping.map(nt, -1);
              naf = t.mapping.map(naf, 1);
              nat = t.mapping.map(nat, -1);
            }
            if (nt <= nf) continue; // Entire numeric phrase disappeared
            let newText: string;
            let anchorText: string;
            try {
              newText = newState.doc.textBetween(nf, nt);
              anchorText = newState.doc.textBetween(naf, nat);
            } catch {
              continue;
            }
            if (newText === oldState.doc.textBetween(hit.from, hit.to)) continue; // Number unchanged
            if (!ANCHOR_RE.test(anchorText)) continue; // Handler already deleted [N] → skip
            toFix.push({ nf, nt, naf, nat });
          }
          if (toFix.length === 0) return null;
          // Delete anchors in descending order; deletions leave earlier match coordinates unchanged
          toFix.sort((a, b) => b.naf - a.naf);
          const tr = newState.tr;
          for (const f of toFix) tr.delete(f.naf, f.nat);
          tr.setMeta(VerifiedGuardKey, {
            downgradeRanges: toFix.map((f) => ({ from: f.nf, to: f.nt })),
          });
          return tr;
        },
        props: {
          decorations(state) {
            return VerifiedGuardKey.getState(state) as DecorationSet | undefined;
          },
          handleTextInput(view, from, to, text) {
            const len = getLen();
            const hit =
              verifiedHitAt(view.state, from, len) ??
              (to !== from ? verifiedHitAt(view.state, to, len) : null);
            if (!hit) return false;
            return confirmThenDeAnchor(view, hit, from, to, { kind: "text", text });
          },
          handleKeyDown(view, event) {
            if (event.key !== "Backspace" && event.key !== "Delete") return false;
            const len = getLen();
            const sel = view.state.selection;
            let editFrom: number;
            let editTo: number;
            if (!sel.empty) {
              editFrom = sel.from;
              editTo = sel.to;
            } else if (event.key === "Backspace") {
              editFrom = Math.max(0, sel.from - 1);
              editTo = sel.from;
            } else {
              editFrom = sel.from;
              editTo = sel.from + 1;
            }
            const hit =
              verifiedHitAt(view.state, editFrom, len) ??
              verifiedHitAt(view.state, editTo, len);
            if (!hit) return false;
            return confirmThenDeAnchor(view, hit, editFrom, editTo, { kind: "delete" });
          },
          handlePaste(view, _event, slice) {
            // Pasting over a verified number is the most realistic bypass; same confirmation + de-anchoring.
            const len = getLen();
            if (len <= 0) return false;
            const sel = view.state.selection;
            const hit =
              verifiedHitAt(view.state, sel.from, len) ??
              (sel.to !== sel.from ? verifiedHitAt(view.state, sel.to, len) : null);
            if (!hit) return false;
            return confirmThenDeAnchor(view, hit, sel.from, sel.to, { kind: "slice", slice });
          },
        },
      }),
    ];
  },
});
