"use client";

/**
 * "n8n Workflows" for a Team tool source that points at n8n's instance-level MCP server
 * (FEATURE_N8N_WORKFLOW_TOOLS).
 *
 * Lists the workflows n8n marks as available in MCP and published; others are only counted. An
 * owner or administrator opens one, chooses its category, writes the description the model reads
 * and edits its input schema, then approves it. The server reads the workflow from n8n again and
 * binds the approval to the published version it saw; each approval becomes one Agent tool from
 * the next task. Nothing is stored until Approve.
 */

import { useCallback, useEffect, useId, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import {
  approveN8nWorkflow,
  getN8nWorkflow,
  listN8nWorkflows,
  removeN8nWorkflow,
  TeamToolApiError,
  type N8nWorkflowDetail,
  type N8nWorkflowList,
  type TeamToolCategory,
  type TeamToolSourcePublic,
} from "@/lib/team-tool-sources-api";

const CATEGORIES: TeamToolCategory[] = ["read", "external_write", "destructive"];
const PROBLEM_CODES = new Set([
  "archived",
  "not_published",
  "cannot_execute",
  "no_supported_trigger",
  "several_triggers",
  "different_workflow",
  "category_invalid",
  "description_missing",
  "schema_invalid",
  "schema_not_object",
  "schema_too_large",
  "schedule_takes_no_input",
]);

type Message = { key: string; values?: Record<string, string | number> };
type Load<T> = { kind: "loading" } | { kind: "error"; error: Message } | { kind: "ready"; data: T };
type View = { kind: "list" } | { kind: "detail"; workflowId: string; name: string };

function problemMessage(code: string, message: string, detail?: string): Message {
  if (!PROBLEM_CODES.has(code)) return { key: "n8n_problem_generic", values: { message } };
  return { key: `n8n_problem_${code}`, values: code === "schema_invalid" ? { detail: detail ?? "" } : undefined };
}

function errorMessage(error: unknown, fallback: string): Message {
  if (!(error instanceof TeamToolApiError)) return { key: fallback };
  if (error.status === 401 || error.status === 403) return { key: "n8n_error_forbidden" };
  const details = error.details ?? {};
  switch (error.code) {
    case "n8n_workflows_unavailable":
      return { key: "n8n_error_unavailable" };
    case "n8n_sign_in_ended":
      return { key: "n8n_error_sign_in" };
    case "n8n_rate_limited":
      return { key: "n8n_error_rate_limited" };
    case "n8n_unreachable":
    case "n8n_refused":
      return { key: "n8n_error_unreachable" };
    case "n8n_workflow_limit":
      return { key: "n8n_error_limit", values: { limit: Number(details.limit) || 64 } };
    case "n8n_workflow_not_approvable":
      return problemMessage(String(details.reason_code ?? ""), String(details.reason ?? ""), String(details.detail ?? ""));
    default:
      return { key: fallback };
  }
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export function SourceN8nWorkflowsDialog({
  source,
  onClose,
  onChanged,
  returnFocusRef,
}: {
  source: TeamToolSourcePublic;
  onClose: () => void;
  onChanged: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("settings.tools");
  const tCommon = useTranslations("common");
  const idPrefix = useId();
  const alive = useRef(true);
  const [view, setView] = useState<View>({ kind: "list" });
  const [list, setList] = useState<Load<N8nWorkflowList>>({ kind: "loading" });
  const [detail, setDetail] = useState<Load<N8nWorkflowDetail>>({ kind: "loading" });
  const [category, setCategory] = useState<TeamToolCategory | "">("");
  const [description, setDescription] = useState("");
  const [schemaText, setSchemaText] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<Message | null>(null);
  const [notice, setNotice] = useState<Message | null>(null);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const loadList = useCallback(async () => {
    setList({ kind: "loading" });
    try {
      const data = await listN8nWorkflows(source.id);
      if (alive.current) setList({ kind: "ready", data });
    } catch (error) {
      if (alive.current) setList({ kind: "error", error: errorMessage(error, "n8n_error_load") });
    }
  }, [source.id]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const openWorkflow = async (workflowId: string, name: string) => {
    setView({ kind: "detail", workflowId, name });
    setDetail({ kind: "loading" });
    setSaveError(null);
    setNotice(null);
    try {
      const data = await getN8nWorkflow(source.id, workflowId);
      if (!alive.current) return;
      setCategory(data.approval?.category ?? "");
      setDescription(data.approval?.description ?? data.workflow.description);
      setSchemaText(prettyJson(data.approval?.input_schema ?? data.draft_input_schema));
      setDetail({ kind: "ready", data });
    } catch (error) {
      if (alive.current) setDetail({ kind: "error", error: errorMessage(error, "n8n_error_load") });
    }
  };

  const backToList = (message: Message | null = null) => {
    setView({ kind: "list" });
    setSaveError(null);
    setNotice(message);
    void loadList();
  };

  const data = detail.kind === "ready" ? detail.data : null;
  const takesBody = data?.trigger?.kind === "webhook";

  const approve = async () => {
    if (view.kind !== "detail" || !data || !category) return;
    let schema: Record<string, unknown> = { type: "object", properties: {}, additionalProperties: false };
    if (takesBody) {
      try {
        const parsed: unknown = JSON.parse(schemaText);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
        schema = parsed as Record<string, unknown>;
      } catch {
        setSaveError({ key: "n8n_error_schema_json" });
        return;
      }
    }
    setSaving(true);
    setSaveError(null);
    try {
      await approveN8nWorkflow(source.id, view.workflowId, { category, description: description.trim(), input_schema: schema });
      if (!alive.current) return;
      onChanged();
      backToList({ key: "n8n_approved_notice", values: { name: data.workflow.name } });
    } catch (error) {
      if (alive.current) setSaveError(errorMessage(error, "n8n_error_save"));
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  const remove = async () => {
    if (view.kind !== "detail") return;
    setSaving(true);
    setSaveError(null);
    try {
      await removeN8nWorkflow(source.id, view.workflowId);
      if (!alive.current) return;
      onChanged();
      backToList({ key: "n8n_removed_notice", values: { name: view.name } });
    } catch (error) {
      if (alive.current) setSaveError(errorMessage(error, "n8n_error_save"));
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  const say = (message: Message) => t(message.key, message.values);
  const categoryLabel = (value: TeamToolCategory) => t(`category_${value}`);

  const footer =
    view.kind === "list" ? (
      <>
        <Button onClick={onClose} data-dialog-initial-focus="">
          {t("cancel")}
        </Button>
        {list.kind === "error" ? (
          <Button variant="primary" onClick={() => void loadList()}>
            {tCommon("retry")}
          </Button>
        ) : null}
      </>
    ) : (
      <>
        <Button onClick={() => backToList()} disabled={saving} data-dialog-initial-focus="">
          {t("n8n_back")}
        </Button>
        {data?.approval ? (
          <Button variant="danger" onClick={() => void remove()} disabled={saving}>
            {t("n8n_remove")}
          </Button>
        ) : null}
        {detail.kind === "error" && view.kind === "detail" ? (
          <Button variant="primary" onClick={() => void openWorkflow(view.workflowId, view.name)}>
            {tCommon("retry")}
          </Button>
        ) : (
          <Button
            variant="primary"
            onClick={() => void approve()}
            disabled={!data || Boolean(data.problem) || saving || !category || !description.trim()}
          >
            {saving ? t("n8n_saving") : t(data?.approval ? "n8n_save" : "n8n_approve")}
          </Button>
        )}
      </>
    );

  return (
    <Dialog
      title={view.kind === "list" ? t("n8n_title", { name: source.name }) : view.name}
      onClose={onClose}
      busy={saving}
      width={680}
      returnFocusRef={returnFocusRef}
      footer={footer}
    >
      {notice ? <Notice tone="success" className="legacy-tools-dialog-error">{say(notice)}</Notice> : null}
      {saveError ? <Notice tone="error" className="legacy-tools-dialog-error">{say(saveError)}</Notice> : null}
      {view.kind === "list" ? (
        list.kind === "loading" ? (
          <p role="status" className="legacy-tools-refresh-loading">{t("n8n_loading")}</p>
        ) : list.kind === "error" ? (
          <Notice tone="error">{say(list.error)}</Notice>
        ) : (
          <div className="legacy-tools-refresh">
            <p className="legacy-tools-dialog-lead">{t("n8n_lead")}</p>
            {list.data.items.length === 0 ? <Notice tone="info">{t("n8n_empty")}</Notice> : null}
            <ul className="legacy-tools-choose-list" aria-label={t("n8n_list_label")}>
              {list.data.items.map((item) => (
                <li key={item.id} className="legacy-tools-choose-row legacy-tools-category-row">
                  <div className="legacy-tools-choose-label">
                    <span className="legacy-tools-choose-name">
                      <span>{item.name}</span>
                      <span className={`legacy-tools-choose-badge${item.approval && item.approval.category !== "read" ? " is-write" : item.approval ? " is-new" : ""}`}>
                        {item.approval ? t("n8n_status_approved", { category: categoryLabel(item.approval.category) }) : t("n8n_status_not_approved")}
                      </span>
                    </span>
                    {item.description ? <span className="legacy-tools-choose-desc">{item.description}</span> : null}
                    {item.listed ? null : (
                      <span className="legacy-tools-refresh-note is-warn">
                        <AlertTriangle size={13} aria-hidden="true" /> {t("n8n_status_unlisted")}
                      </span>
                    )}
                  </div>
                  <Button size="sm" onClick={() => void openWorkflow(item.id, item.name)} aria-label={t("n8n_open_label", { name: item.name })}>
                    {t("n8n_open")}
                  </Button>
                </li>
              ))}
            </ul>
            {list.data.hidden_count ? <p className="legacy-tools-choose-desc">{t("n8n_hidden", { n: list.data.hidden_count })}</p> : null}
            {list.data.more_than_listed ? <p className="legacy-tools-choose-desc">{t("n8n_more")}</p> : null}
          </div>
        )
      ) : detail.kind === "loading" ? (
        <p role="status" className="legacy-tools-refresh-loading">{t("n8n_loading_one")}</p>
      ) : detail.kind === "error" ? (
        <Notice tone="error">{say(detail.error)}</Notice>
      ) : (
        <div className="legacy-tools-refresh">
          {detail.data.problem ? (
            <p className="legacy-tools-refresh-note is-warn" role="status">
              <AlertTriangle size={13} aria-hidden="true" /> {say(problemMessage(detail.data.problem.code, detail.data.problem.message))}
            </p>
          ) : null}
          {detail.data.version_changed ? (
            <p className="legacy-tools-refresh-note is-warn" role="status">
              <AlertTriangle size={13} aria-hidden="true" /> {t("n8n_version_changed")}
            </p>
          ) : null}
          {detail.data.trigger ? (
            <p className="legacy-tools-dialog-lead">
              {t(detail.data.trigger.kind === "webhook" ? "n8n_trigger_webhook" : "n8n_trigger_schedule", { node: detail.data.trigger.node })}
            </p>
          ) : null}
          {detail.data.problem ? null : (
            <>
              <Field id={`${idPrefix}-category`} label={t("n8n_category_label")} hint={t("n8n_category_hint")}>
                <select
                  id={`${idPrefix}-category`}
                  className="ui-input legacy-input-tools"
                  value={category}
                  disabled={saving}
                  onChange={(event) => setCategory(event.target.value as TeamToolCategory | "")}
                >
                  <option value="">{t("n8n_category_choose")}</option>
                  {CATEGORIES.map((value) => (
                    <option key={value} value={value}>
                      {categoryLabel(value)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field id={`${idPrefix}-description`} label={t("n8n_description_label")} hint={t("n8n_description_hint")}>
                <textarea
                  id={`${idPrefix}-description`}
                  className="ui-input"
                  rows={3}
                  maxLength={1000}
                  value={description}
                  disabled={saving}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </Field>
              {takesBody ? (
                <Field id={`${idPrefix}-schema`} label={t("n8n_schema_label")} hint={t("n8n_schema_hint")}>
                  <textarea
                    id={`${idPrefix}-schema`}
                    className="ui-input legacy-tools-n8n-schema"
                    rows={8}
                    spellCheck={false}
                    translate="no"
                    value={schemaText}
                    disabled={saving}
                    onChange={(event) => setSchemaText(event.target.value)}
                  />
                </Field>
              ) : null}
            </>
          )}
        </div>
      )}
    </Dialog>
  );
}
