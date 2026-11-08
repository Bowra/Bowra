"use client";

/**
 * "Confirm Tool Categories" for a remote MCP Team tool source (FEATURE_TEAM_TOOL_CONFIRMATION).
 *
 * Lists each tool with what the service marked and lets an owner or administrator confirm a
 * category. A category below the service's marking is not offered; the server rejects it too.
 * Read-only tools then run without asking, and every other call waits for the requester's
 * confirmation. Only changed tools are sent; a confirmation applies until the service changes
 * the tool's definition, after which the list asks for it again.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import {
  getSourceToolCategories,
  saveSourceToolCategories,
  TeamToolApiError,
  type SourceToolCategories,
  type TeamToolCategory,
  type TeamToolSourcePublic,
} from "@/lib/team-tool-sources-api";

const ORDER: TeamToolCategory[] = ["read", "external_write", "destructive"];
const RANK: Record<TeamToolCategory, number> = { read: 0, external_write: 1, destructive: 2 };

type ErrorKey =
  | "category_error_load"
  | "category_error_save"
  | "category_error_below"
  | "category_error_unavailable"
  | "category_error_changed"
  | "category_error_forbidden";

type Load = { kind: "loading" } | { kind: "error"; error: ErrorKey } | { kind: "ready"; data: SourceToolCategories };

function errorKey(error: unknown, fallback: ErrorKey): ErrorKey {
  if (error instanceof TeamToolApiError) {
    if (error.status === 401 || error.status === 403) return "category_error_forbidden";
    if (error.code === "team_tool_category_unavailable") return "category_error_unavailable";
    if (error.code === "team_tool_category_below_annotation") return "category_error_below";
    if (error.code === "team_tool_category_unknown_tool" || error.status === 404) return "category_error_changed";
  }
  return fallback;
}

export function SourceToolCategoryDialog({
  source,
  onClose,
  onSaved,
  returnFocusRef,
}: {
  source: TeamToolSourcePublic;
  onClose: () => void;
  onSaved: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("settings.tools");
  const tCommon = useTranslations("common");
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [draft, setDraft] = useState<Record<string, TeamToolCategory | "">>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<ErrorKey | null>(null);
  const alive = useRef(true);
  // Tool names are server-defined, so element ids use the row index instead.
  const idPrefix = useId();

  const loadCategories = useCallback(async () => {
    setLoad({ kind: "loading" });
    try {
      const data = await getSourceToolCategories(source.id);
      if (!alive.current) return;
      setDraft(Object.fromEntries(data.items.map((item) => [item.name, item.confirmed_category ?? ""])));
      setLoad({ kind: "ready", data });
    } catch (error) {
      if (alive.current) setLoad({ kind: "error", error: errorKey(error, "category_error_load") });
    }
  }, [source.id]);

  useEffect(() => {
    alive.current = true;
    void loadCategories();
    return () => {
      alive.current = false;
    };
  }, [loadCategories]);

  const data = load.kind === "ready" ? load.data : null;
  const changes = useMemo(() => {
    const out: Record<string, TeamToolCategory | null> = {};
    for (const item of data?.items ?? []) {
      const next = draft[item.name] ?? "";
      if (next !== (item.confirmed_category ?? "")) out[item.name] = next === "" ? null : next;
    }
    return out;
  }, [data, draft]);
  const changedCount = Object.keys(changes).length;

  const save = async () => {
    if (!changedCount) return;
    setSaving(true);
    setSaveError(null);
    try {
      await saveSourceToolCategories(source.id, changes);
      if (alive.current) onSaved();
    } catch (error) {
      if (alive.current) setSaveError(errorKey(error, "category_error_save"));
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  const categoryLabel = (category: TeamToolCategory | "unknown") => t(`category_${category}`);

  const footer = (
    <>
      <Button onClick={onClose} disabled={saving} data-dialog-initial-focus="">
        {t("cancel")}
      </Button>
      {load.kind === "error" ? (
        <Button variant="primary" onClick={() => void loadCategories()}>
          {tCommon("retry")}
        </Button>
      ) : (
        <Button variant="primary" onClick={() => void save()} disabled={!data || saving || changedCount === 0 || !data.enabled}>
          {saving ? t("category_saving") : t("save")}
        </Button>
      )}
    </>
  );

  return (
    <Dialog
      title={t("category_title", { name: source.name })}
      onClose={onClose}
      busy={saving}
      width={640}
      returnFocusRef={returnFocusRef}
      footer={footer}
    >
      {saveError ? <Notice tone="error" className="legacy-tools-dialog-error">{t(saveError)}</Notice> : null}
      {load.kind === "loading" ? (
        <p role="status" className="legacy-tools-refresh-loading">{t("category_loading")}</p>
      ) : load.kind === "error" ? (
        <Notice tone="error">{t(load.error)}</Notice>
      ) : (
        <div className="legacy-tools-refresh">
          {load.data.enabled ? null : <Notice tone="info">{t("category_error_unavailable")}</Notice>}
          <p className="legacy-tools-dialog-lead">{t("category_lead")}</p>
          <ul className="legacy-tools-choose-list" aria-label={t("category_list_label")}>
            {load.data.items.map((item, index) => {
              const selectId = `${idPrefix}-category-${index}`;
              const marked = item.annotation_category;
              return (
                <li key={item.name} className="legacy-tools-choose-row">
                  <div className="legacy-tools-choose-label">
                    <label htmlFor={selectId} className="legacy-tools-choose-name">
                      <span translate="no">{item.name}</span>
                      {item.imported ? null : <span className="legacy-tools-choose-badge">{t("category_not_imported")}</span>}
                    </label>
                    {item.description ? <span className="legacy-tools-choose-desc">{item.description}</span> : null}
                    <span className="legacy-tools-choose-desc">{t("category_marked", { category: categoryLabel(marked) })}</span>
                    {item.needs_reconfirmation ? (
                      <span className="legacy-tools-refresh-note is-warn">
                        <AlertTriangle size={13} aria-hidden="true" /> {t("category_reconfirm")}
                      </span>
                    ) : null}
                  </div>
                  <select
                    id={selectId}
                    className="ui-input legacy-input-tools"
                    value={draft[item.name] ?? ""}
                    disabled={saving || !load.data.enabled}
                    aria-label={t("category_select_label", { tool: item.name })}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, [item.name]: event.target.value as TeamToolCategory | "" }))
                    }
                  >
                    <option value="">{t("category_option_unset")}</option>
                    {ORDER.map((category) => (
                      <option
                        key={category}
                        value={category}
                        disabled={marked !== "unknown" && RANK[category] < RANK[marked]}
                      >
                        {categoryLabel(category)}
                      </option>
                    ))}
                  </select>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </Dialog>
  );
}
