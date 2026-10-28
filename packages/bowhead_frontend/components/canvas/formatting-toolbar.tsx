/**
 * FormattingToolbar — inline toolbar appearing over narrative selection (design D2TextEdit, rc-sel-toolbar).
 *
 * WP3 (2026-06-13): refactor into design's single selection toolbar — formatting (B/I/link/font) + AI shortcuts
 * (✦改写 / 缩短 / 正式) merged into one rc-sel-toolbar, replacing separate top AIRewriteMenu.
 *
 * AI actions map to existing rewrite presets:
 *   改写 → different_angle · 缩短 → concise · 正式 → professional
 * Trigger uses useAIRewrite (selection-level) → AIRewriteDiffBar handles accept/revert.
 */

"use client";

import { useEffect, useId, useRef, useState } from "react";
import {
  useFloating,
  autoUpdate,
  offset,
  flip,
  shift,
  FloatingPortal,
} from "@floating-ui/react";
import type { Editor } from "@tiptap/react";
import type { Node as PMNode } from "@tiptap/pm/model";
import { useTranslations } from "next-intl";
import { Spark, Link } from "@/components/icons";
import { toast } from "@/lib/use-toast";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import type { RewritePreset } from "./ai-rewrite-menu";
import {
  aiRewriteDraftKey,
  readAiRewriteDraft,
  sectionIdAtResolvedPos,
  writeAiRewriteDraft,
} from "@/lib/ai-rewrite-drafts";
import { CharIcon } from "@/components/char-icon";

/**
 * feedback-0713 F1: captured absolute positions are valid only for the doc at capture time — rebuilding or
 * out-of-band rewriting while toolbar is open can make {from,to} refer to other text. Verify selected
 * text still matches before formatting; out-of-bounds positions (shortened doc) also count as mismatch. exported for tests.
 */
/**
 * Appearance refresh batch 4b: selection-toolbar keyboard entry ⌥⏎ (Alt+Enter / Option+Return). Accept only
 * Alt+Enter without ⌘, Ctrl or Shift; ignore IME composition (isComposing, keyCode 229) — Chinese IME commits with Enter.
 * Editor (StarterKit) binds only Enter, Shift+Enter, Mod+Enter, leaving Alt+Enter free. exported for tests.
 */
export function isSelectionToolbarShortcut(event: {
  key: string;
  altKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  isComposing?: boolean;
  keyCode?: number;
}): boolean {
  if (event.isComposing || event.keyCode === 229) return false;
  return event.key === "Enter" && event.altKey && !event.metaKey && !event.ctrlKey && !event.shiftKey;
}

export function capturedSelectionStillValid(
  doc: PMNode,
  sel: { from: number; to: number; text: string },
): boolean {
  try {
    return doc.textBetween(sel.from, sel.to, "\n") === sel.text;
  } catch {
    return false;
  }
}

interface Props {
  editor: Editor | null;
  open: boolean;
  isEditMode: boolean;
  isStreaming: boolean;
  isAIRewriting: boolean;
  onOpen: () => void;
  onClose: () => void;
  floatingRef?: (node: HTMLElement | null) => void;
  /** WP3: selection-level AI shortcuts → ReportView calls aiRewrite.start(preset). AI group hidden by default. */
  onAIAction?: (preset: RewritePreset) => void;
  /** Feishu 07.06 #15c: selection-level custom rewrite instruction → ReportView calls aiRewrite.start("custom", instruction, range).
   *  Only when supplied, ✦改写 opens input (aligned with whole-block AI bar); otherwise use one-click preset. */
  onAICustom?: (instruction: string, range: { from: number; to: number }) => void;
  /** Appearance refresh: save custom-rewrite drafts by report + section, retaining on reopen (canvas id, otherwise session id). */
  draftScope?: string | null;
}

export function FormattingToolbar({
  editor,
  open,
  isEditMode,
  isStreaming,
  isAIRewriting,
  onOpen,
  onClose,
  floatingRef,
  onAIAction,
  onAICustom,
  draftScope = null,
}: Props) {
  const t = useTranslations("formatting_toolbar");
  const [selection, setSelection] = useState<{
    rect: DOMRect | null;
    from: number;
    to: number;
    /** feedback-0713 F1: selected text at capture time; verify doc was not replaced before restoreSel */
    text: string;
  } | null>(null);
  // Feishu 07.06 #15c: custom instruction input opened by ✦改写 (null=closed). Ref lets selectionUpdate
  // handler keep toolbar open while input is open (avoids unexpected closing when input takes focus).
  const [aiInput, setAiInput] = useState<string | null>(null);
  const aiInputOpenRef = useRef(false);
  aiInputOpenRef.current = aiInput !== null;

  // Selection dismissed with Esc. Editor regains focus; Esc keyup rechecks selection,
  // but unchanged selection must not reopen it (previously both browsers reopened immediately; timing differences looked inconsistent).
  // Clear when selection changes or empties.
  const dismissedRangeRef = useRef<{ from: number; to: number } | null>(null);

  const { refs, floatingStyles } = useFloating({
    open: open && !!selection,
    placement: "top",
    whileElementsMounted: autoUpdate,
    middleware: [offset(8), flip(), shift({ padding: 8 })],
  });

  useEffect(() => {
    if (!editor) return;
    const handler = () => {
      if (isStreaming || isAIRewriting || !isEditMode) {
        setSelection(null);
        onClose();
        return;
      }
      const { from, to, empty } = editor.state.selection;
      const dismissed = dismissedRangeRef.current;
      if (dismissed) {
        if (!empty && dismissed.from === from && dismissed.to === to) return;
        dismissedRangeRef.current = null;
      }
      if (empty || from === to) {
        // #15c review[3]: with custom rewrite input open, an **empty** selection usually means input took focus, causing blur →
        // retain captured range and keep toolbar open. Still capture new **nonempty** selections below (otherwise
        // user opening input then selecting B elsewhere would incorrectly rewrite old selection A on submit).
        if (aiInputOpenRef.current) return;
        setSelection(null);
        onClose();
        return;
      }
      // Text-based sections only
      const $from = editor.state.selection.$from;
      let inTextBased = false;
      for (let d = $from.depth; d >= 0; d--) {
        const n = $from.node(d).type.name;
        if (n === "section_narrative" || n === "section_deck" || n === "section_callout") {
          inTextBased = true;
          break;
        }
      }
      if (!inTextBased) {
        setSelection(null);
        onClose();
        return;
      }
      const view = editor.view;
      const start = view.coordsAtPos(from);
      const end = view.coordsAtPos(to);
      const rect = {
        x: Math.min(start.left, end.left),
        y: start.top,
        width: Math.abs(end.right - start.left),
        height: end.bottom - start.top,
        top: start.top,
        bottom: end.bottom,
        left: Math.min(start.left, end.left),
        right: Math.max(start.right, end.right),
      } as DOMRect;
      setSelection({ rect, from, to, text: editor.state.doc.textBetween(from, to, "\n") });
      onOpen();
      refs.setReference({
        getBoundingClientRect: () => rect,
        contextElement: view.dom,
      });
    };
    editor.on("selectionUpdate", handler);
    const syncEditorSelectionFromDOM = () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.anchorNode || !sel.focusNode) return;
      const root = editor.view.dom;
      if (!root.contains(sel.anchorNode) || !root.contains(sel.focusNode)) return;
      try {
        const anchor = editor.view.posAtDOM(sel.anchorNode, sel.anchorOffset);
        const head = editor.view.posAtDOM(sel.focusNode, sel.focusOffset);
        if (anchor !== head) {
          editor.commands.setTextSelection({
            from: Math.min(anchor, head),
            to: Math.max(anchor, head),
          });
        }
      } catch {
        // Ignore transient native-selection positions outside ProseMirror text.
      }
    };
    const syncAfterNativeSelection = () => {
      requestAnimationFrame(() => {
        syncEditorSelectionFromDOM();
        handler();
      });
    };
    // Releasing pointer in body explicitly selects: invalidate Esc dismissal (even reselecting same text reopens toolbar)
    const onPointerUp = () => {
      dismissedRangeRef.current = null;
      syncAfterNativeSelection();
    };
    editor.view.dom.addEventListener("pointerup", onPointerUp);
    editor.view.dom.addEventListener("keyup", syncAfterNativeSelection);
    return () => {
      editor.off("selectionUpdate", handler);
      editor.view.dom.removeEventListener("pointerup", onPointerUp);
      editor.view.dom.removeEventListener("keyup", syncAfterNativeSelection);
    };
  }, [editor, isEditMode, isStreaming, isAIRewriting, onClose, onOpen, refs]);

  useEffect(() => {
    if (!open) setSelection(null);
  }, [open]);

  // Appearance refresh batch 4b (Report Canvas Chrome Spec section 4):
  //   · "Aa" contains font / size / weight dropdowns, visible only on opening (dropdowns remain compact);
  //   · ⌥⏎ (Alt+Enter) moves focus from body into toolbar (only on shortcut, never steals focus); ←/→ moves between buttons;
  //     Esc returns to body with selection preserved (overlay manager closes toolbar; here return focus to editor).
  const [fontsOpen, setFontsOpen] = useState(false);
  const fontGroupId = useId();
  const toolbarRef = useRef<HTMLDivElement | null>(null);
  // Currently open font / size / weight dropdown; Esc closes only that dropdown first.
  const openFontMenuRef = useRef<FontMenuHandle | null>(null);
  // Esc reaches window capture here first (before overlay manager closes whole toolbar at document capture):
  //   · Font dropdown open: close only dropdown, focus its button, keep toolbar (nested overlays close topmost only; batch 4b browser alignment);
  //   · Otherwise overlay manager then closes toolbar: record dismissed selection to prevent reopening on editor focus; if focus is in toolbar,
  //     return it to editor first (selection unchanged, restored on focus). Do not steal focus if outside toolbar (e.g. shortcut in body).
  useEffect(() => {
    if (!editor) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || editor.isDestroyed) return;
      const fontMenu = openFontMenuRef.current;
      if (fontMenu) {
        event.preventDefault();
        event.stopPropagation();
        fontMenu.close();
        return;
      }
      if (!toolbarRef.current) return;
      const { from, to, empty } = editor.state.selection;
      dismissedRangeRef.current = empty ? null : { from, to };
      const active = document.activeElement;
      if (!active || !toolbarRef.current.contains(active)) return;
      editor.view.focus();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [editor]);
  useEffect(() => {
    if (!open) {
      setFontsOpen(false);
      openFontMenuRef.current = null;
    }
  }, [open]);
  useEffect(() => {
    if (!editor || editor.isDestroyed) return;
    const dom = editor.view.dom;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!isSelectionToolbarShortcut(event)) return;
      const first = toolbarRef.current?.querySelector<HTMLElement>("button.rc-sel-btn");
      if (!first) return;
      event.preventDefault();
      event.stopPropagation();
      first.focus();
    };
    dom.addEventListener("keydown", onKeyDown, true);
    return () => dom.removeEventListener("keydown", onKeyDown, true);
  }, [editor]);

  // Toolbar closed externally (selection cleared, etc.) → reset custom input, leaving no orphan.
  // Must precede early return below (Rules of Hooks): formerly placed after return →
  // selection changed hook count 7→8 → React #310 crash (caught in Feishu 07.06 #15 dogfood).
  useEffect(() => {
    if (!open) setAiInput(null);
  }, [open]);

  if (!open || !selection || !editor) return null;

  // feedback-0713 F1 defense: captured absolute positions apply only to doc at capture time. Rebuilding or
  // out-of-band rewriting while toolbar is open can make {from,to} point to different text — verify
  // selected text still matches before applying; close on mismatch rather than format wrong range.
  const restoreSel = (): boolean => {
    if (!selection) return false;
    if (!capturedSelectionStillValid(editor.state.doc, selection)) {
      setSelection(null);
      onClose();
      // feedback-0713 F1 review: mismatch dismissal was previously silent (user clicked → toolbar
      // disappeared → nothing happened, reported as bug). Show a hint to reselect.
      toast({ kind: "info", text: t("selection_stale") });
      return false;
    }
    editor
      .chain()
      .focus()
      .setTextSelection({ from: selection.from, to: selection.to })
      .run();
    return true;
  };

  const isBold = editor.isActive("bold");
  const isItalic = editor.isActive("italic");
  const isLink = editor.isActive("link");

  // #305 typography: current textStyle mark attribute values (mark active option in dropdown)
  const curFontFamily = (editor.getAttributes("textStyle").fontFamily as string | null) ?? null;
  const curFontSize = (editor.getAttributes("textStyle").fontSize as string | null) ?? null;
  const curFontWeight = (editor.getAttributes("textStyle").fontWeight as string | null) ?? null;

  const onBold = () => {
    if (!restoreSel()) return;
    editor.chain().focus().toggleBold().run();
  };
  const onItalic = () => {
    if (!restoreSel()) return;
    editor.chain().focus().toggleItalic().run();
  };
  const onLink = () => {
    if (!restoreSel()) return;
    if (isLink) {
      editor.chain().focus().unsetLink().run();
      return;
    }
    const url = window.prompt(t("link_url_prompt"));
    if (!url) return;
    editor.chain().focus().setLink({ href: url }).run();
  };

  // #305: dropdown handlers — token-friendly font stack, consistent across platforms
  const onSetFontFamily = (family: string | null) => {
    if (!restoreSel()) return;
    if (!family) {
      editor.chain().focus().unsetFontFamily().run();
    } else {
      editor.chain().focus().setFontFamily(family).run();
    }
  };
  const onSetFontSize = (size: string | null) => {
    if (!restoreSel()) return;
    if (!size) editor.chain().focus().unsetFontSize().run();
    else editor.chain().focus().setFontSize(size).run();
  };
  const onSetFontWeight = (weight: string | null) => {
    if (!restoreSel()) return;
    if (!weight) editor.chain().focus().unsetFontWeight().run();
    else editor.chain().focus().setFontWeight(weight).run();
  };

  // WP3: AI shortcuts — restore selection before triggering preset rewrite.
  const onAI = (preset: RewritePreset) => {
    if (!onAIAction) return;
    if (!restoreSel()) return;
    onAIAction(preset);
    setSelection(null);
    onClose();
  };

  // Draft key for the selected section
  const aiDraftKey = (): string | null => {
    if (!editor || !selection) return null;
    try {
      return aiRewriteDraftKey(draftScope, sectionIdAtResolvedPos(editor.state.doc.resolve(selection.from)));
    } catch {
      return null;
    }
  };

  // #15c: submit custom selection rewrite. Captured selection.from/to supplies range (unaffected by input taking focus);
  // empty input → generic rewrite (preserves one-click convenience).
  const submitAICustom = () => {
    if (!selection) { setAiInput(null); return; }
    const text = (aiInput ?? "").trim();
    const range = { from: selection.from, to: selection.to };
    if (!restoreSel()) { setAiInput(null); return; }
    if (text && onAICustom) onAICustom(text, range);
    else onAIAction?.("different_angle");
    writeAiRewriteDraft(aiDraftKey(), "");
    setAiInput(null);
    setSelection(null);
    onClose();
  };

  // i18n: resolve dropdown option labels (labelKey → t) just before render.
  const resolveOpts = (opts: FontOption[]) =>
    opts.map((o) => ({ value: o.value, label: o.labelKey ? t(o.labelKey) : (o.label ?? "") }));

  const formatGroup = (
    <>
      <Btn active={isBold} onClick={onBold} label={t("bold")}>
        <strong>B</strong>
      </Btn>
      <Btn active={isItalic} onClick={onItalic} label={t("italic")}>
        <em className="legacy-rc-italic-glyph">I</em>
      </Btn>
      <Btn active={isLink} onClick={onLink} label={isLink ? t("link_remove") : t("link_add")}>
        <Link width={13} height={13} />
      </Btn>
    </>
  );
  // Dropdowns register so Esc above closes only the open dropdown
  const registerFontMenu = (handle: FontMenuHandle | null) => {
    if (handle) openFontMenuRef.current = handle;
    else if (openFontMenuRef.current) openFontMenuRef.current = null;
  };
  const fontDropdowns = (
    <>
      <FontDropdown label={t("font_family")} value={curFontFamily} options={resolveOpts(FONT_FAMILY_OPTIONS)} onPick={onSetFontFamily} width={64} onMenuOpenChange={registerFontMenu} />
      <FontDropdown label={t("font_size")} value={curFontSize} options={resolveOpts(FONT_SIZE_OPTIONS)} onPick={onSetFontSize} width={48} onMenuOpenChange={registerFontMenu} />
      <FontDropdown label={t("font_weight")} value={curFontWeight} options={resolveOpts(FONT_WEIGHT_OPTIONS)} onPick={onSetFontWeight} width={56} onMenuOpenChange={registerFontMenu} />
    </>
  );
  const aiGroup = (
    aiInput !== null ? (
      // #15c: custom selection rewrite input (aligned with whole-block AI bar). Enter submits / Esc closes.
      // Original inline styles moved to styles/report-canvas.css .legacy-rc-ai-custom* (workbench and canvas batch 1)
      <span className="rc-ai-custom legacy-rc-ai-custom">
        <input
          autoFocus
          value={aiInput}
          onChange={(e) => {
            setAiInput(e.target.value);
            writeAiRewriteDraft(aiDraftKey(), e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); submitAICustom(); }
            else if (e.key === "Escape") { e.preventDefault(); setAiInput(null); }
          }}
          placeholder={t("ai_custom_placeholder")}
          aria-label={t("ai_custom_placeholder")}
          className="legacy-rc-ai-custom-input"
        />
        <Btn accent onClick={submitAICustom} label={t("ai_custom_send")}>
          <Spark width={12} height={12} /> {t("ai_custom_send")}
        </Btn>
      </span>
    ) : (
      <>
        {/* #15c: with onAICustom, ✦改写 opens custom instruction input; otherwise use one-click preset. */}
        <Btn
          accent
          onClick={() => (onAICustom ? setAiInput(readAiRewriteDraft(aiDraftKey())) : onAI("different_angle"))}
          label={t("ai_rewrite")}
        >
          <Spark width={12} height={12} /> {t("ai_rewrite")}
        </Btn>
        <Btn onClick={() => onAI("concise")} label={t("ai_shorten")}>
          {t("ai_shorten")}
        </Btn>
        <Btn onClick={() => onAI("professional")} label={t("ai_formal")}>
          {t("ai_formal")}
        </Btn>
      </>
    )
  );

  const floatingRefCallback = (node: HTMLDivElement | null) => {
    refs.setFloating(node);
    floatingRef?.(node);
    // Record the toolbar node for ⌥⏎ and Esc focus return
    toolbarRef.current = node;
  };

  // Three groups — AI (first, only group with accent text) | formatting | fonts (inside "Aa").
  // ←/→ moves between buttons (does not take over inputs or open dropdown menus).
  const fontsActive = Boolean(curFontFamily || curFontSize || curFontWeight);
  const onToolbarKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const target = event.target as HTMLElement;
    if (target.tagName === "INPUT" || target.closest("[role='menu']")) return;
    const buttons = Array.from(
      toolbarRef.current?.querySelectorAll<HTMLButtonElement>("button.rc-sel-btn") ?? [],
    );
    const index = buttons.indexOf(target as HTMLButtonElement);
    if (index < 0 || buttons.length === 0) return;
    event.preventDefault();
    const step = event.key === "ArrowRight" ? 1 : -1;
    buttons[(index + step + buttons.length) % buttons.length].focus();
  };
  return (
    <FloatingPortal>
      <div
        ref={floatingRefCallback}
        className="rc-sel-toolbar"
        role="toolbar"
        aria-label={t("aria")}
        aria-keyshortcuts="Alt+Enter"
        style={{ ...floatingStyles, zIndex: 999 }}
        onKeyDown={onToolbarKeyDown}
      >
        {onAIAction && (
          <>
            {aiGroup}
            <span className="rc-action-sep" />
          </>
        )}
        {formatGroup}
        <span className="rc-action-sep" />
        <button
          type="button"
          className={"rc-sel-btn rc-sel-aa" + (fontsActive || fontsOpen ? " active" : "")}
          title={t("font_group")}
          aria-label={t("font_group")}
          aria-expanded={fontsOpen}
          aria-controls={fontsOpen ? fontGroupId : undefined}
          onClick={() => setFontsOpen((v) => !v)}
        >
          Aa
        </button>
        {fontsOpen ? (
          <span id={fontGroupId} className="rc-sel-fontgroup" role="group" aria-label={t("font_group")}>
            {fontDropdowns}
          </span>
        ) : null}
      </div>
    </FloatingPortal>
  );
}

// #305: typography dropdown options. Font-family CSS variable tokens ensure cross-platform consistency.
// `labelKey` → resolve via formatting_toolbar.* at render (i18n); plain `label` is a
// locale-neutral literal (numbers / weight names) shown verbatim.
type FontOption = { label?: string; labelKey?: string; value: string | null };
const FONT_FAMILY_OPTIONS: FontOption[] = [
  { labelKey: "font_opt_default", value: null },
  { labelKey: "font_opt_sans", value: "var(--font-v2-sans)" },
  { labelKey: "font_opt_serif", value: "var(--font-v2-serif)" },
  { labelKey: "font_opt_mono", value: "var(--font-v2-mono)" },
];
const FONT_SIZE_OPTIONS: FontOption[] = [
  { labelKey: "font_opt_default", value: null },
  { label: "12", value: "12px" },
  { label: "14", value: "14px" },
  { label: "16", value: "16px" },
  { label: "18", value: "18px" },
  { label: "24", value: "24px" },
  { label: "32", value: "32px" },
];
const FONT_WEIGHT_OPTIONS: FontOption[] = [
  { labelKey: "font_opt_default", value: null },
  { label: "Regular", value: "400" },
  { label: "Medium", value: "500" },
  { label: "Semibold", value: "600" },
  { label: "Bold", value: "700" },
];

/** Open font dropdown: Esc closes only it and focuses its button. */
interface FontMenuHandle {
  close: () => void;
}

function FontDropdown({
  label, value, options, onPick, width, onMenuOpenChange,
}: {
  label: string;
  value: string | null;
  options: Array<{ label: string; value: string | null }>;
  onPick: (v: string | null) => void;
  width: number;
  /** Passed by the toolbar: register close method on dropdown opening, unregister on closing. */
  onMenuOpenChange?: (handle: FontMenuHandle | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const onMenuOpenChangeRef = useRef(onMenuOpenChange);
  onMenuOpenChangeRef.current = onMenuOpenChange;
  useEffect(() => {
    const register = onMenuOpenChangeRef.current;
    if (!register) return;
    if (!open) return;
    const handle: FontMenuHandle = {
      close: () => {
        setOpen(false);
        triggerRef.current?.focus();
      },
    };
    register(handle);
    return () => register(null);
  }, [open]);
  // Portal mounts inside dropdown outer div rather than body: report overlay manager (use-report-overlay-manager)
  // detects outside clicks by selection-toolbar node containment; mounting to body would close toolbar before option selection.
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const menu = useMenu({ open, onOpenChange: setOpen, placement: "bottom-start", offset: 4 });
  const cur = options.find((o) => o.value === value);
  return (
    <div ref={setHost} style={{ position: "relative" }}>
      <button
        type="button"
        ref={(node) => {
          menu.refs.setReference(node);
          triggerRef.current = node;
        }}
        className={"rc-sel-btn" + (value ? " active" : "")}
        title={label}
        aria-label={label}
        style={{ minWidth: width }}
        {...menu.getReferenceProps()}
      >
        {(cur?.label ?? label)} <CharIcon ch="▾" />
      </button>
      <MenuFloating menu={menu} root={host}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({
            "aria-label": label,
            onMouseLeave: () => setOpen(false),
            // Original inline styles moved to styles/ui-controls-legacy.css (appearance refresh batch 3)
            className: "legacy-menu-font",
          })}
        >
          {options.map((o) => (
            <FontOptionItem key={o.label} option={o} selected={o.value === value} onPick={onPick} />
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function FontOptionItem({
  option, selected, onPick,
}: {
  option: { label: string; value: string | null };
  selected: boolean;
  onPick: (v: string | null) => void;
}) {
  const itemProps = useMenuItem({ onSelect: () => onPick(option.value) });
  return (
    // data-selected affects appearance only (row-leading check in refresh); screen-reader semantics stay ordinary menu item.
    <button
      type="button"
      className="legacy-menu-font-item"
      data-selected={selected ? "true" : undefined}
      {...itemProps}
    >
      {option.label}
    </button>
  );
}

function Btn({
  children,
  active,
  accent,
  onClick,
  label,
}: {
  children: React.ReactNode;
  /** Toggle state (B/I/link) — apply aria-pressed */
  active?: boolean;
  /** Accent state (AI rewrite, nontoggle action) — same accent style without aria-pressed */
  accent?: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      className={"rc-sel-btn" + (active || accent ? " active" : "")}
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={active === undefined ? undefined : active}
    >
      {children}
    </button>
  );
}
