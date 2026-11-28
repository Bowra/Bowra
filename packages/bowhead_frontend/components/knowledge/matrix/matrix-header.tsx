"use client";

/**
 * Matrix page header (document matrix step 1, S2): breadcrumb 知识库 › 文档矩阵, the matrix name with inline rename,
 * and the action row (⋯ with Rename / Delete Matrix…, then the page's own actions, the primary one last).
 *
 * Rename: Enter or leaving the field saves a changed, non-empty name; Esc keeps the current one. An empty name on
 * Enter stays in the field with the reason; leaving the field empty keeps the current name. Editing is desktop only:
 * the caller passes `canEdit=false` on phones.
 *
 * The page actions (`children`) show without edit rights too, so the caller passes only those the viewer may use
 * (S5: a viewer may export, but not add columns or run).
 *
 * Step 1 S8: an only-me matrix shows "仅自己可见" next to its name; its creator (server `can_change_visibility`) finds
 * 设为仅自己可见 / 设为空间成员可见 in ⋯, which the caller confirms before changing.
 */

import { Children, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import type { MatrixVisibility } from "@/lib/kb-matrix/types";

import { MatrixVisibilityBadge } from "./visibility-badge";

export function MatrixHeader({
  title,
  meta,
  canEdit,
  canManage,
  visibility = "space",
  canChangeVisibility = false,
  onHome,
  onBack,
  onRename,
  onDelete,
  onChangeVisibility,
  children,
}: {
  title: string;
  meta: ReactNode;
  /** Member rank on desktop: rename and the ⋯ menu. */
  canEdit: boolean;
  /** Creator or Space/Team admin (server `can_manage`): may delete the matrix. */
  canManage: boolean;
  /** S8: who can see the matrix. */
  visibility?: MatrixVisibility;
  /** S8: the viewer created the matrix and only-me matrices are enabled (server `can_change_visibility`). */
  canChangeVisibility?: boolean;
  onHome: () => void;
  onBack: () => void;
  /** Resolves true once saved; on false the field closes and the caller has reported the failure. */
  onRename: (name: string) => Promise<boolean>;
  onDelete: () => void;
  /** S8: asked to switch to the other visibility; the caller confirms first. */
  onChangeVisibility?: () => void;
  /** Page actions after ⋯ (the primary action last), already limited to what the viewer may use. */
  children?: ReactNode;
}) {
  const t = useTranslations("kbMatrix");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const [empty, setEmpty] = useState(false);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const nameRef = useRef<HTMLButtonElement | null>(null);
  // Esc unmounts the field; a blur that follows must not save it.
  const doneRef = useRef(false);
  const errorId = useId();
  const hasActions = Children.toArray(children).length > 0;

  useEffect(() => {
    if (!editing) return;
    // From ⋯ → Rename the menu has already returned focus to its trigger, so the field can take it now.
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [editing]);

  const start = () => {
    doneRef.current = false;
    setDraft(title);
    setEmpty(false);
    setEditing(true);
  };

  const close = () => {
    doneRef.current = true;
    setEditing(false);
    setEmpty(false);
    requestAnimationFrame(() => nameRef.current?.focus());
  };

  const commit = async (fromBlur: boolean) => {
    if (doneRef.current) return;
    const name = draft.trim();
    if (!name) {
      if (fromBlur) close();
      else setEmpty(true);
      return;
    }
    if (name === title) {
      close();
      return;
    }
    // One save per edit: the blur that follows Enter (or the field turning disabled) must not send it again.
    doneRef.current = true;
    setSaving(true);
    await onRename(name);
    setSaving(false);
    close();
  };

  return (
    <div className="mx-topbar mx-head" data-testid="kb-matrix-head">
      <div className="mx-headcopy">
        <nav className="mx-crumb" aria-label={t("crumb_label")}>
          <button type="button" className="mx-crumb-link" onClick={onHome}>
            {t("crumb_kb")}
          </button>
          <span aria-hidden="true">›</span>
          <button type="button" className="mx-crumb-link" data-testid="kb-matrix-crumb-list" onClick={onBack}>
            {t("crumb_matrices")}
          </button>
        </nav>
        <div className="mx-titlerow">
          {editing ? (
            <input
              ref={inputRef}
              className="mx-inp mx-title-input"
              aria-label={t("rename_input")}
              aria-invalid={empty || undefined}
              aria-describedby={empty ? errorId : undefined}
              data-testid="kb-matrix-rename-input"
              value={draft}
              maxLength={200}
              disabled={saving}
              onChange={(e) => {
                setDraft(e.target.value);
                if (empty) setEmpty(false);
              }}
              onKeyDown={(e) => {
                // The matrix moves its cell focus on arrow keys and closes panels on Esc; keys stay in the field.
                e.stopPropagation();
                if (e.key === "Enter") {
                  e.preventDefault();
                  void commit(false);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  close();
                }
              }}
              onBlur={() => void commit(true)}
            />
          ) : (
            <h2 className="mx-title">
              {canEdit ? (
                <button
                  ref={nameRef}
                  type="button"
                  className="mx-title-button"
                  aria-label={t("rename_button", { name: title })}
                  data-testid="kb-matrix-title"
                  onClick={start}
                >
                  {title}
                </button>
              ) : (
                <span data-testid="kb-matrix-title">{title}</span>
              )}
            </h2>
          )}
          <MatrixVisibilityBadge visibility={visibility} />
          <span className="mx-meta">{meta}</span>
        </div>
        {empty && (
          <p id={errorId} className="mx-inlineerr mx-rename-error" role="alert">
            {t("rename_empty")}
          </p>
        )}
      </div>
      <span className="grow" />
      {(canEdit || hasActions) && (
        <div className="acts">
          {canEdit && (
            <RowActionsMenu
              className="mx-head-more"
              label={t("matrix_more")}
              actions={[
                { id: "rename", label: t("menu_rename"), onSelect: start },
                ...(canChangeVisibility && onChangeVisibility
                  ? [
                      {
                        id: "visibility",
                        label: t(visibility === "private" ? "menu_visibility_space" : "menu_visibility_private"),
                        onSelect: onChangeVisibility,
                      },
                    ]
                  : []),
                {
                  id: "delete",
                  label: t("menu_delete_matrix"),
                  danger: true,
                  disabled: !canManage,
                  disabledReason: canManage ? undefined : t("delete_matrix_reason"),
                  onSelect: onDelete,
                },
              ]}
            />
          )}
          {children}
        </div>
      )}
    </div>
  );
}
