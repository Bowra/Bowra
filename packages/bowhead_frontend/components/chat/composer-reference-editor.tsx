"use client";

import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import type { Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import {
  autoUpdate,
  flip,
  FloatingPortal,
  offset,
  shift,
  size,
  useFloating,
} from "@floating-ui/react";
import { useLocale, useTranslations } from "next-intl";
import {
  parseInputDocument,
  validateInputDocumentTask,
  type InputDocument,
  type ReferenceNode,
} from "@/lib/composer-input-document";
import {
  captureReferenceInsertion,
  insertReference,
  referenceEditorOptions,
  updateReferenceParameters,
} from "@/lib/composer-reference-editor";
import {
  closeReferenceTrigger,
  type ReferenceTriggerQuery,
} from "@/lib/composer-reference-trigger";
import {
  composerDraftScopeSignature,
  completeComposerScope,
  type ComposerPersistenceScope,
} from "@/lib/composer-persistence-scope";
import { MenuKeyHints } from "@/components/ui/menu";
import { useReferenceCatalog } from "@/lib/use-reference-catalog";
import { randomUuid } from "@/lib/random-id";
import { getReferenceParameterSchema, referenceCatalogItemIdentity } from "@/lib/reference-catalog-api";
import { ReferenceParameterPanel, type ReferenceParameterTarget } from "./reference-parameter-panel";
import type {
  ReferenceCatalogItem,
  ReferenceEntryMode,
} from "@/lib/reference-catalog-api";
import "@/styles/composer-reference-editor.css";

/** An agent that cannot be bound is still reachable through Lead, so agents get their own wording for it. */
function referenceReasonKey(isAgent: boolean, code: string | null | undefined): string {
  return isAgent && code === "unsupported_binding" ? "reason.agent_unsupported_binding" : `reason.${code}`;
}

function referenceItemReasonKey(item: ReferenceCatalogItem): string {
  if (
    item.kind === "skill" &&
    item.resource !== null &&
    item.read_only_parameter_schema === null &&
    item.reason_code === "unsupported_binding"
  ) {
    return "reason.skill_readonly_unavailable";
  }
  if (item.kind === "skill" && item.reason_code === "unsupported_binding") return "reason.skill_unsupported_binding";
  return referenceReasonKey(item.kind === "agent", item.reason_code);
}

export type ComposerReferenceEditorSnapshot = {
  scope: ComposerPersistenceScope;
  /** Change only for an explicit external replacement/recovery, not each onChange echo. */
  resetKey: string;
  document: InputDocument;
};
type MenuAction = {
  id: string;
  label: string;
  disabled: boolean;
  run: () => void;
};

type Props = {
  scope: ComposerPersistenceScope | null;
  snapshot: ComposerReferenceEditorSnapshot | null;
  readOnly?: boolean;
  hasAttachments?: boolean;
  entryMode?: ReferenceEntryMode;
  onChange: (document: InputDocument, scope: ComposerPersistenceScope) => void;
  /** Intent only. The owning Composer must validate/admit/persist before issuing HTTP. */
  onSubmitIntent: (
    document: InputDocument,
    scope: ComposerPersistenceScope,
  ) => void;
  onGhostKeyDown?: (event: KeyboardEvent, atEnd: boolean) => boolean;
  ghostText?: string | null;
  ghostHint?: string;
  autoFocus?: boolean;
  placeholder?: string;
  onCompositionStateChange?: (blocked: boolean, scope: ComposerPersistenceScope) => void;
};

/** The source snapshot must belong to the current scope before any editor is mounted.
 * Persistence and sending remain owned by the enclosing Composer and turn runner.
 */
export function ComposerReferenceEditor(props: Props) {
  const t = useTranslations("composer_references");
  const signature = completeComposerScope(props.scope)
    ? composerDraftScopeSignature(props.scope)
    : "";
  const owner = completeComposerScope(props.snapshot?.scope ?? null)
    ? composerDraftScopeSignature(props.snapshot!.scope)
    : "";
  const key =
    signature && signature === owner && props.snapshot?.resetKey
      ? JSON.stringify([signature, props.snapshot.resetKey])
      : "";
  const active = useRef(key);
  const parsed = useMemo(() => {
    if (!key || !props.snapshot) return null;
    try {
      return parseInputDocument(props.snapshot.document);
    } catch {
      return null;
    }
  }, [key, props.snapshot?.document]);
  active.current = parsed ? key : "";
  if (!key) return <p role="status">{t("waiting_for_draft")}</p>;
  if (!parsed || !props.scope) return <p role="alert">{t("invalid_draft")}</p>;
  return (
    <ScopedReferenceEditor
      key={key}
      {...props}
      scope={props.scope}
      initialDocument={parsed}
      isCurrent={() => active.current === key}
    />
  );
}

function ScopedReferenceEditor(
  props: Props & {
    scope: ComposerPersistenceScope;
    initialDocument: InputDocument;
    isCurrent: () => boolean;
  },
) {
  const t = useTranslations("composer_references");
  const locale = useLocale();
  // Behavior batch A: keyboard hints below the completion list (desktop pointers only, controlled by CSS).
  const listId = useId();
  const hintId = useId();
  const [document, setDocument] = useState(props.initialDocument);
  const source = useRef(document);
  const mounted = useRef(false);
  const compositionFrame = useRef<number | null>(null);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (compositionFrame.current !== null) cancelAnimationFrame(compositionFrame.current);
    };
  }, []);
  const referenceCount = useRef(
    props.initialDocument.nodes.filter((node) => node.type === "reference")
      .length,
  );
  const [query, setQuery] = useState<ReferenceTriggerQuery | null>(null);
  const queryRef = useRef(query);
  const [notice, setNotice] = useState<{ key: string; count?: number; label?: string } | null>(
    null,
  );
  const [navigation, setNavigation] = useState({ key: "", index: 0 });
  const root = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Editor | null>(null);
  const parameterRequest = useRef<{ editId: string; controller: AbortController; document: ProseMirrorNode } | null>(null);
  const [parameterTarget, setParameterTarget] = useState<ReferenceParameterTarget | null>(null);
  function closeParameters() {
    parameterRequest.current?.controller.abort();
    parameterRequest.current = null;
    setParameterTarget(null);
    setNotice(null);
  }
  useEffect(() => () => {
    parameterRequest.current?.controller.abort();
    parameterRequest.current = null;
  }, []);
  const menu = useRef<HTMLDivElement | null>(null);
  const latest = useRef({ ...props, t });
  latest.current = {
    ...props,
    t,
    isCurrent: () => mounted.current && props.isCurrent(),
  };
  const menuState = useRef({
    key: "",
    queryKey: "",
    index: -1,
    items: [] as ReferenceCatalogItem[],
    actions: [] as MenuAction[],
  });
  const queryKey = (value: ReferenceTriggerQuery | null) =>
    value ? JSON.stringify(value) : "";
  const [options] = useState(() =>
    referenceEditorOptions(props.initialDocument, {
      ariaLabel: t("input_label"),
      onChange: (value) => {
        const current = latest.current;
        if (!current.isCurrent() || current.readOnly) return;
        closeParameters();
        source.current = value;
        setDocument(value);
        const count = value.nodes.filter(
          (node) => node.type === "reference",
        ).length;
        setNotice(
          count === referenceCount.current
            ? null
            : { key: "selected_count", count },
        );
        referenceCount.current = count;
        current.onChange(parseInputDocument(value), { ...current.scope });
      },
      onQueryChange: (value) => {
        const current = latest.current;
        if (!current.isCurrent()) return;
        queryRef.current = current.readOnly ? null : value;
        setQuery(queryRef.current);
      },
      onRejected: (reason) => {
        if (latest.current.isCurrent())
          setNotice({ key: `edit_error.${reason}` });
      },
      isMenuOpen: () =>
        latest.current.isCurrent() &&
        !latest.current.readOnly &&
        queryRef.current !== null,
      onMenuClose: () => closeMenu(),
      onMenuKeyDown: (key) => {
        const current = menuState.current;
        if (key === "Escape") {
          closeMenu();
          return;
        }
        if (key === "Tab") {
          if (current.queryKey === queryKey(queryRef.current) && current.items[current.index] && current.items[current.index].availability !== "unavailable") {
            selectItem(current.index, current.key);
            return;
          }
          return "move_focus";
        }
        if (key === "PageUp" || key === "PageDown") {
          const list = window.document.getElementById(listId);
          list?.scrollBy({
            top: (key === "PageUp" ? -1 : 1) * list.clientHeight * 0.8,
            behavior: "auto",
          });
          return;
        }
        const count = current.items.length + current.actions.length;
        if (current.queryKey !== queryKey(queryRef.current) || !count) return;
        let index = current.index;
        if (key === "ArrowDown") index = (index + 1) % count;
        if (key === "ArrowUp") index = (index - 1 + count) % count;
        if (key === "Home") index = 0;
        if (key === "End") index = count - 1;
        if (key === "Enter") {
          const action = current.actions[index - current.items.length];
          if (action) {
            if (!action.disabled) action.run();
          } else selectItem(index, current.key);
          return;
        }
        menuState.current = { ...current, index };
        setNavigation({ key: current.key, index });
      },
      onGhostKeyDown: (event) =>
        latest.current.isCurrent() && !latest.current.readOnly
          ? (latest.current.onGhostKeyDown?.(event, Boolean(editorRef.current?.state.selection.empty && editorRef.current.state.selection.to === editorRef.current.state.doc.content.size - 1)) ?? false)
          : false,
      onSubmit: () => {
        const current = latest.current;
        if (!current.isCurrent() || current.readOnly || !editorRef.current)
          return;
        const value = parseInputDocument(source.current);
        try {
          validateInputDocumentTask(value, current.hasAttachments ?? false);
        } catch (error) {
          setNotice({
            key:
              error instanceof Error &&
              error.message === "reference_command_conflict"
                ? "command_conflict"
                : "task_required",
          });
          return;
        }
        current.onSubmitIntent(value, { ...current.scope });
      },
    }),
  );
  const open = Boolean(query && !props.readOnly);
  const selection = useMemo(
    () =>
      !query || props.readOnly
        ? null
        : {
            trigger: query.trigger,
            query: query.query,
            entryMode: props.entryMode ?? "ordinary",
            selectedRefs: document.nodes.flatMap((node) =>
              node.type === "reference" ? [node.resource] : [],
            ),
          },
    [query, document, props.readOnly, props.entryMode],
  );
  const catalog = useReferenceCatalog(props.scope, selection, locale);
  const items = catalog.data?.groups.flatMap((group) => group.items) ?? [];
  const actions: MenuAction[] = [];
  if (
    catalog.error ||
    catalog.data?.groups.some(
      (group) => group.error === "catalog_metadata_unavailable",
    )
  )
    actions.push({
      id: "retry",
      label: t("retry"),
      disabled: false,
      run: catalog.retry,
    });
  if (catalog.data?.next_cursor)
    actions.push({
      id: "more",
      label: t(catalog.loadingMore ? "loading_more" : "load_more"),
      disabled: catalog.loadingMore,
      run: () => {
        void catalog.loadMore();
      },
    });
  const optionCount = items.length + actions.length;
  const navigationKey = JSON.stringify([
    queryKey(query),
    catalog.data?.catalog_generation ?? "",
    locale,
  ]);
  const activeIndex = optionCount
    ? Math.min(
        navigation.key === navigationKey ? navigation.index : 0,
        optionCount - 1,
      )
    : -1;
  menuState.current = {
    key: navigationKey,
    queryKey: queryKey(query),
    items,
    actions,
    index: activeIndex,
  };
  const activeId =
    activeIndex >= 0 ? `${listId}-option-${activeIndex}` : undefined;
  const editor = useEditor({
    ...options,
    autofocus: props.autoFocus ? "end" : false,
    editable: !props.readOnly,
    editorProps: {
      decorations: (state) => {
        const current = latest.current;
        if (!current.isCurrent() || current.readOnly) return DecorationSet.empty;
        const decorations: Decoration[] = [];
        state.doc.descendants((node, position) => {
          if (node.type.name !== "reference" || ["agent", "mcp_source"].includes(node.attrs.reference.resource.kind)) return;
          const reference = node.attrs.reference as ReferenceNode;
          decorations.push(Decoration.node(position, position + node.nodeSize, {
            role: "button", tabindex: "0", "data-reference-node-id": reference.node_id,
            "aria-label": current.t("edit_parameters", { label: reference.label }),
            "aria-haspopup": "dialog",
          }));
        });
        if (!queryRef.current && current.ghostText && !editorRef.current?.view.composing)
          decorations.push(Decoration.widget(state.doc.content.size - 1, () => {
          const span = window.document.createElement("span");
          span.className = "composer-reference-editor__ghost";
          span.setAttribute("aria-hidden", "true");
          span.setAttribute("data-testid", "composer-autocomplete-ghost");
          span.textContent = current.ghostText!;
          const hint = window.document.createElement("span");
          hint.className = "composer-reference-editor__ghost-hint";
          hint.textContent = " " + (current.ghostHint ?? "");
          span.append(hint);
          return span;
        }, { side: 1, key: JSON.stringify([current.ghostText, current.ghostHint]) }));
        return DecorationSet.create(state.doc, decorations);
      },
      attributes: {
        role: "combobox",
        "aria-label": t("input_label"),
        "aria-autocomplete": "list",
        "aria-expanded": String(open),
        "aria-haspopup": "listbox",
        "aria-describedby": hintId,
        ...(open ? { "aria-controls": listId } : {}),
        ...(open && activeId ? { "aria-activedescendant": activeId } : {}),
        ...(props.readOnly ? { "aria-readonly": "true" } : {}),
        style: "white-space: pre-wrap; overflow-wrap: anywhere;",
      },
    },
    immediatelyRender: false,
    shouldRerenderOnTransaction: false,
  });
  editorRef.current = editor;
  const parameterReturnFocus = useRef<HTMLElement | null>(null);
  async function openParameters(nodeId: string) {
    const owner = latest.current;
    const current = editorRef.current;
    if (!owner.isCurrent() || owner.readOnly || !current || current.isDestroyed || current.view.composing || compositionFrame.current !== null) return;
    const document = parseInputDocument(source.current);
    const node = document.nodes.find((node): node is ReferenceNode => node.type === "reference" && node.node_id === nodeId);
    if (!node || node.resource.kind === "agent" || node.resource.kind === "mcp_source") return;
    closeMenu();
    closeParameters();
    const request = { editId: randomUuid(), controller: new AbortController(), document: current.state.doc };
    parameterRequest.current = request;
    parameterReturnFocus.current = current.view.dom;
    const scope = { ...owner.scope };
    setNotice({ key: "parameters_loading" });
    try {
      const schema = await getReferenceParameterSchema(scope, node.resource, owner.entryMode ?? "ordinary", request.controller.signal);
      if (parameterRequest.current !== request || request.controller.signal.aborted || !latest.current.isCurrent()
        || latest.current.readOnly || current.isDestroyed || current.state.doc !== request.document) return;
      setNotice(null);
      setParameterTarget({ scope, editId: request.editId, document, nodeId, resource: node.resource, parameterSchema: schema });
    } catch {
      if (parameterRequest.current !== request || !latest.current.isCurrent() || request.controller.signal.aborted) return;
      closeParameters();
      setNotice({ key: "parameters_unavailable" });
    }
  }
  function parameterNode(event: React.SyntheticEvent) {
    const element = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-reference-node-id]") : null;
    return element && root.current?.contains(element) ? element.dataset.referenceNodeId : undefined;
  }
  function selectItem(index: number, expectedKey: string) {
    const owner = latest.current;
    const current = menuState.current;
    const query = queryRef.current;
    const editor = editorRef.current;
    if (!owner.isCurrent() || owner.readOnly || !editor || editor.isDestroyed || !query
      || current.key !== expectedKey || current.queryKey !== queryKey(query)) return;
    const item = current.items[index];
    if (!item) return;
    if (item.availability === "unavailable" || !item.resource) {
      setNotice({ key: referenceItemReasonKey(item) });
      return;
    }
    const inserted = insertReference({
      type: "reference",
      node_id: randomUuid(),
      resource: item.resource,
      label: item.label,
      parameters: {},
    }, captureReferenceInsertion(editor.state, query.from, query.to))(editor.state, editor.view.dispatch);
    if (!inserted) {
      setNotice({ key: "edit_error.invalid_document" });
      return;
    }
    closeMenu();
    setNotice({
      key:
        item.kind === "agent"
          ? "agent_selected"
          : item.kind === "capability"
            ? "capability_selected"
            : "skill_selected",
      label: item.label,
    });
    editor.view.focus();
  }
  function closeMenu() {
    queryRef.current = null;
    setQuery(null);
    const current = editorRef.current;
    if (current && !current.isDestroyed)
      closeReferenceTrigger(current.state, current.view.dispatch);
  }
  const { refs, floatingStyles, update } = useFloating({
    open,
    placement: "top-start",
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware: [
      offset(8),
      flip({ padding: 8 }),
      shift({ padding: 8 }),
      size({
        padding: 8,
        apply({ availableHeight, availableWidth, elements }) {
          Object.assign(elements.floating.style, {
            maxHeight: `${Math.max(0, Math.min(360, availableHeight))}px`,
            maxWidth: `${Math.max(0, availableWidth)}px`,
          });
        },
      }),
    ],
  });
  useLayoutEffect(() => {
    if (!editor) return;
    editor.setEditable(!props.readOnly, false);
    if (props.readOnly) {
      closeMenu();
      closeParameters();
    }
  }, [editor, props.readOnly]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !root.current?.contains(event.target) &&
        !menu.current?.contains(event.target)
      )
        closeMenu();
    };
    window.document.addEventListener("pointerdown", outside, true);
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    return () => {
      window.document.removeEventListener("pointerdown", outside, true);
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
    };
  }, [open, update]);
  useEffect(() => {
    if (open && activeId)
      window.document
        .getElementById(activeId)
        ?.scrollIntoView?.({ block: "nearest" });
  }, [open, activeId]);
  const empty = document.nodes.every(
    (node) => node.type === "text" && !node.text,
  );
  const keepFocus = (event: React.PointerEvent) => event.preventDefault();
  let itemIndex = 0;
  return (
    <div className="composer-reference-editor" ref={root}
      onClickCapture={(event) => {
        const nodeId = parameterNode(event);
        if (nodeId) void openParameters(nodeId);
      }}
      onKeyDownCapture={(event) => {
        const nodeId = parameterNode(event);
        if (nodeId && (event.key === "Enter" || event.key === " ")) {
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          event.preventDefault();
          event.stopPropagation();
          void openParameters(nodeId);
        }
      }}
      onCompositionStartCapture={(event) => {
        // A portalled parameter form owns its own IME lifecycle. It must not
        // fence the Composer, especially if that form closes before compositionend.
        if (!(event.target instanceof Node) || !root.current?.contains(event.target)) return;
        if (compositionFrame.current !== null) cancelAnimationFrame(compositionFrame.current);
        compositionFrame.current = null;
        if (latest.current.isCurrent()) latest.current.onCompositionStateChange?.(true, latest.current.scope);
      }}
      onCompositionEndCapture={(event) => {
        if (!(event.target instanceof Node) || !root.current?.contains(event.target)) return;
        if (compositionFrame.current !== null) cancelAnimationFrame(compositionFrame.current);
        let previous = source.current;
        let stable = 0;
        const settle = () => {
          if (!latest.current.isCurrent()) return;
          stable = source.current === previous ? stable + 1 : 0;
          previous = source.current;
          if (stable < 2 || editorRef.current?.view.composing) {
            compositionFrame.current = requestAnimationFrame(settle);
          } else {
            compositionFrame.current = null;
            latest.current.onCompositionStateChange?.(false, latest.current.scope);
          }
        };
        compositionFrame.current = requestAnimationFrame(settle);
      }}
    >
      <div className="composer-reference-editor__input" ref={refs.setReference}>
        <EditorContent editor={editor} />
        {editor && empty ? (
          <span
            className="composer-reference-editor__placeholder"
            aria-hidden="true"
          >
            {props.placeholder ?? t("placeholder")}
          </span>
        ) : null}
        {!editor ? <p role="status">{t("preparing")}</p> : null}
      </div>
      <p className="composer-v2__sr-only" id={hintId}>
        {props.readOnly
          ? t("readonly")
          : t(open ? "menu_keyboard_hint" : "keyboard_hint")}{" "}
        {t("selected_count", {
          count: document.nodes.filter((node) => node.type === "reference")
            .length,
        })}
      </p>
      <p role="status" className={`composer-reference-editor__notice${notice?.key === "selected_count" || notice?.key.startsWith("reason.") ? " composer-v2__sr-only" : ""}`}>
        {notice
          ? t(
              notice.key,
              notice.label !== undefined ? { label: notice.label } : notice.count === undefined ? undefined : { count: notice.count },
            )
          : ""}
      </p>
      <ReferenceParameterPanel
        scope={props.scope}
        document={document}
        target={parameterTarget}
        returnFocusRef={parameterReturnFocus}
        readOnly={props.readOnly}
        onDismiss={(editId) => {
          if (parameterRequest.current?.editId !== editId) return;
          closeParameters();
        }}
        onCommitIntent={(intent) => {
          const request = parameterRequest.current;
          const current = editorRef.current;
          if (!latest.current.isCurrent() || latest.current.readOnly || !request || !current || current.isDestroyed
            || request.editId !== intent.editId || JSON.stringify(source.current) !== JSON.stringify(intent.sourceDocument)) return;
          if (!updateReferenceParameters(request.document, intent.nodeId, intent.parameters)(current.state, current.view.dispatch)) {
            closeParameters();
            setNotice({ key: "parameters_changed" });
            return;
          }
          closeParameters();
        }}
      />
      {open ? (
        <FloatingPortal>
          <div
            ref={(element) => {
              menu.current = element;
              refs.setFloating(element);
            }}
            style={floatingStyles}
            className="app-floating-menu composer-reference-menu"
            onMouseDown={(event) => event.preventDefault()}
          >
            <div className="composer-reference-menu__heading">
              {t(query?.trigger === "@" ? "agents_title" : "resources_title")}
            </div>
            <div
              className="composer-reference-menu__results"
              id={listId}
              role="listbox"
              aria-label={t("results_label")}
              aria-busy={catalog.status === "loading" || catalog.loadingMore}
            >
              {catalog.data?.groups
                .filter((group) => group.items.length > 0)
                .map((group) => (
                  <div
                    role="group"
                    aria-label={t(`group.${group.id}`)}
                    key={group.id}
                  >
                    <div
                      className="composer-reference-menu__group"
                      aria-hidden="true"
                    >
                      {t(`group.${group.id}`)}
                    </div>
                    {group.items.map((item) => {
                      const index = itemIndex++;
                      const id = `${listId}-option-${index}`;
                      return (
                        <div
                          id={id}
                          key={referenceCatalogItemIdentity(item)}
                          role="option"
                          aria-selected="false"
                          aria-disabled={item.availability === "unavailable"}
                          aria-describedby={`${id}-reason`}
                          data-active={
                            index === activeIndex ? "true" : undefined
                          }
                          className="app-floating-menu-item composer-reference-menu__option"
                          onPointerMove={() =>
                            setNavigation({ key: navigationKey, index })
                          }
                          onClick={() => selectItem(index, navigationKey)}
                        >
                          <span
                            aria-hidden="true"
                            className="composer-reference-menu__icon"
                          >
                            {item.kind === "agent" ? "@" : "/"}
                          </span>
                          <span className="composer-reference-menu__copy">
                            <span className="composer-reference-menu__name">
                              {item.label}
                            </span>
                            <span className="composer-reference-menu__summary" title={item.summary}>
                              {item.summary}
                            </span>
                            <span className="composer-reference-menu__source">
                              {t(
                                item.kind === "agent"
                                  ? item.source_kind === "system"
                                    ? "system_agent"
                                    : item.created_by_me ? "my_agent" : "team_agent"
                                  : item.kind === "capability"
                                    ? "platform_capability"
                                    : item.kind === "mcp_source"
                                      ? "mcp_source"
                                      : item.kind === "mcp_tool"
                                        ? "mcp_tool"
                                        : item.kind === "mcp_prompt"
                                          ? "mcp_prompt"
                                    : item.source_kind === "user"
                                    ? "personal_skill"
                                    : item.source_kind === "team"
                                      ? "team_skill"
                                      : "builtin_skill",
                              )}
                            </span>
                            <span
                              id={`${id}-reason`}
                              className={`composer-reference-menu__reason${item.availability !== "unavailable" ? " composer-v2__sr-only" : ""}`}
                            >
                              {t(item.availability === "baseline" ? "baseline_available" : item.availability === "readonly_extension" ? "extension_available" : referenceItemReasonKey(item))}
                            </span>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ))}
              {actions.map((action, index) => {
                const optionIndex = items.length + index;
                return (
                  <div
                    key={action.id}
                    id={`${listId}-option-${optionIndex}`}
                    role="option"
                    aria-label={action.label}
                    aria-selected="false"
                    aria-disabled={action.disabled}
                    data-active={
                      optionIndex === activeIndex ? "true" : undefined
                    }
                    className="app-floating-menu-item composer-reference-menu__option composer-reference-menu__action"
                    onPointerMove={() =>
                      setNavigation({ key: navigationKey, index: optionIndex })
                    }
                    onClick={() => {
                      if (!action.disabled) action.run();
                    }}
                  >
                    <span>{action.label}</span>
                    <kbd aria-hidden="true">Enter</kbd>
                  </div>
                );
              })}
            </div>
            {catalog.data?.groups
              .filter((group) => group.error)
              .map((group) => (
                <div
                  key={group.id}
                  role="status"
                  className="composer-reference-menu__message"
                >
                  <span className="composer-reference-menu__group-label">
                    {t(`group.${group.id}`)}
                  </span>
                  <span>
                    {group.error
                      ? group.items.length && group.error === "catalog_metadata_unavailable"
                        ? t("partial_metadata_unavailable")
                        : t(referenceReasonKey(group.id === "team_agents" || group.id === "system_agents", group.error))
                      : t("no_matches")}
                  </span>
                </div>
              ))}
            {catalog.status === "loaded" &&
              !items.length &&
              !catalog.data?.next_cursor &&
              catalog.data?.groups.every((group) => !group.error) ? (
              <p className="composer-reference-menu__message" role="status">{t("no_matches")}</p>
            ) : null}
            {catalog.status === "loading" ? (
              <p className="composer-reference-menu__message" role="status">
                {t("loading")}
              </p>
            ) : null}
            {catalog.error ? (
              <p className="composer-reference-menu__message" role="alert">
                {t(`load_error.${catalog.error}`)}
              </p>
            ) : null}
            <div className="composer-reference-menu__actions">
              <button
                type="button"
                className="app-floating-menu-item"
                onPointerDown={keepFocus}
                onClick={() => {
                  closeMenu();
                  editor?.commands.focus();
                }}
              >
                {t("keep_typing")}
              </button>
            </div>
            <MenuKeyHints
              hints={[
                { keys: "↑↓", label: t("hint_select") },
                { keys: "↵", label: t("hint_confirm") },
                { keys: "esc", label: t("hint_close") },
              ]}
            />
          </div>
        </FloatingPortal>
      ) : null}
    </div>
  );
}
