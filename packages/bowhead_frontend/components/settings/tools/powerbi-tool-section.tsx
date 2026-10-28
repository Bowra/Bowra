"use client";

import { useEffect, useState } from "react";
import { codedErrorMessage, genericUserFacingErrorMessage } from "@/lib/user-facing-error";
import { useTranslations } from "next-intl";

import { PowerBiMark } from "@/components/settings/data-source-icons";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import {
  createPowerBiTemplate,
  createPowerBiTool,
  deletePowerBiTemplate,
  deletePowerBiTool,
  listPowerBiTemplates,
  setPowerBiEnabled,
  updatePowerBiTool,
  type PowerBiTemplate,
  type PowerBiToolInput,
  type TeamBuiltinTool,
} from "@/lib/team-builtin-tools-api";

const empty: PowerBiToolInput = {
  name: "", tenant_id: "", client_id: "", client_secret: "", workspace_id: "", dataset_id: "",
};

// Coded Power BI failures carry localized catalog copy from the gateway.
const POWERBI_ERROR_CODES = ["powerbi_", "pbi_", "builtin_tool_", "request_validation_"] as const;

export function PowerBiToolSection({ tools, createRequest, onChanged }: {
  tools: TeamBuiltinTool[];
  createRequest: number;
  onChanged: () => Promise<void>;
}) {
  const t = useTranslations("powerbi_tool");
  const [editing, setEditing] = useState<TeamBuiltinTool | "new" | null>(null);
  const [removing, setRemoving] = useState<TeamBuiltinTool | null>(null);
  const [form, setForm] = useState<PowerBiToolInput>(empty);
  const [templatesFor, setTemplatesFor] = useState<TeamBuiltinTool | null>(null);
  const [templates, setTemplates] = useState<PowerBiTemplate[]>([]);
  const [templateName, setTemplateName] = useState("");
  const [templateDax, setTemplateDax] = useState("");
  const [templateRemoving, setTemplateRemoving] = useState<PowerBiTemplate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (createRequest > 0) {
      setForm(empty);
      setError("");
      setEditing("new");
    }
  }, [createRequest]);

  function openEdit(tool: TeamBuiltinTool) {
    setForm({
      name: tool.name, tenant_id: tool.tenant_id ?? "", client_id: tool.client_id ?? "",
      client_secret: "", workspace_id: tool.workspace_id ?? "", dataset_id: tool.dataset_id ?? "",
    });
    setError("");
    setEditing(tool);
  }

  function setField(key: keyof PowerBiToolInput, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save() {
    if (!editing) return;
    setBusy(true);
    setError("");
    try {
      if (editing === "new") await createPowerBiTool(form);
      else await updatePowerBiTool(editing.id, { ...form, client_secret: form.client_secret || undefined });
      setEditing(null);
      await onChanged();
    } catch (cause) {
      setError(codedErrorMessage(cause, genericUserFacingErrorMessage(), POWERBI_ERROR_CODES));
    } finally { setBusy(false); }
  }

  async function toggle(tool: TeamBuiltinTool) {
    setBusy(true);
    setError("");
    try { await setPowerBiEnabled(tool.id, tool.status !== "enabled"); await onChanged(); }
    catch (cause) { setError(codedErrorMessage(cause, genericUserFacingErrorMessage(), POWERBI_ERROR_CODES)); }
    finally { setBusy(false); }
  }

  async function remove() {
    if (!removing) return;
    setBusy(true);
    try { await deletePowerBiTool(removing.id); setRemoving(null); await onChanged(); }
    catch (cause) { setError(codedErrorMessage(cause, genericUserFacingErrorMessage(), POWERBI_ERROR_CODES)); }
    finally { setBusy(false); }
  }

  async function openTemplates(tool: TeamBuiltinTool) {
    setError("");
    setTemplatesFor(tool);
    try { setTemplates(await listPowerBiTemplates(tool.id)); }
    catch (cause) { setError(codedErrorMessage(cause, genericUserFacingErrorMessage(), POWERBI_ERROR_CODES)); }
  }

  async function addTemplate() {
    if (!templatesFor) return;
    setBusy(true);
    try {
      await createPowerBiTemplate(templatesFor.id, templateName.trim(), templateDax.trim());
      setTemplates(await listPowerBiTemplates(templatesFor.id));
      setTemplateName(""); setTemplateDax(""); setError("");
    } catch (cause) { setError(codedErrorMessage(cause, genericUserFacingErrorMessage(), POWERBI_ERROR_CODES)); }
    finally { setBusy(false); }
  }

  async function removeTemplate() {
    if (!templatesFor || !templateRemoving) return;
    setBusy(true);
    try {
      await deletePowerBiTemplate(templatesFor.id, templateRemoving.id);
      setTemplates(await listPowerBiTemplates(templatesFor.id));
      setTemplateRemoving(null); setError("");
    } catch (cause) { setError(codedErrorMessage(cause, genericUserFacingErrorMessage(), POWERBI_ERROR_CODES)); }
    finally { setBusy(false); }
  }

  return <>
    {tools.map((tool) => <div key={tool.id} className={`legacy-tools-pbi-row${tool.status === "enabled" ? "" : " is-off"}`}>
      <PowerBiMark />
      <div className="legacy-tools-pbi-main">
        <strong>{tool.display_name}</strong>
        <div className="legacy-tools-pbi-meta">
          {tool.workspace_id} · {tool.dataset_id} {tool.key_hint ? `· ••••${tool.key_hint}` : ""}
        </div>
      </div>
      <RowActionsMenu label={t("more_actions", { name: tool.display_name })} actions={[
        { id: "edit", label: t("edit"), onSelect: () => openEdit(tool) },
        { id: "templates", label: t("dax_templates"), onSelect: () => void openTemplates(tool) },
        { id: "delete", label: t("delete"), danger: true, onSelect: () => setRemoving(tool) },
      ]} />
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => void toggle(tool)}>
        {tool.status === "enabled" ? t("disable") : t("enable")}
      </Button>
    </div>)}
    {error && !editing && !templatesFor ? <Notice tone="error">{error}</Notice> : null}
    {editing ? <Dialog title={editing === "new" ? t("add_title") : t("edit_title")} onClose={() => setEditing(null)}>
      <p>{t("intro")}</p>
      <Field id="powerbi-tool-name" label={t("connection_name")}><input id="powerbi-tool-name" className="ui-input" value={form.name} onChange={(event) => setField("name", event.target.value)} maxLength={60} /></Field>
      <Field id="powerbi-tool-tenant" label="Tenant ID"><input id="powerbi-tool-tenant" className="ui-input" value={form.tenant_id} onChange={(event) => setField("tenant_id", event.target.value)} /></Field>
      <Field id="powerbi-tool-client" label="Client ID"><input id="powerbi-tool-client" className="ui-input" value={form.client_id} onChange={(event) => setField("client_id", event.target.value)} /></Field>
      <Field id="powerbi-tool-secret" label="Client secret"><input id="powerbi-tool-secret" className="ui-input" type="password" autoComplete="new-password" value={form.client_secret} onChange={(event) => setField("client_secret", event.target.value)} placeholder={editing === "new" ? "" : t("secret_keep_placeholder")} /></Field>
      <Field id="powerbi-tool-workspace" label={t("workspace_id_optional")}><input id="powerbi-tool-workspace" className="ui-input" value={form.workspace_id} onChange={(event) => setField("workspace_id", event.target.value)} /></Field>
      <Field id="powerbi-tool-dataset" label={t("dataset_id_optional")}><input id="powerbi-tool-dataset" className="ui-input" value={form.dataset_id} onChange={(event) => setField("dataset_id", event.target.value)} /></Field>
      <p className="legacy-tools-pbi-note">{t("ids_note")}</p>
      {error ? <Notice tone="error">{error}</Notice> : null}
      <div className="legacy-tools-pbi-actions"><Button variant="secondary" onClick={() => setEditing(null)}>{t("cancel")}</Button><Button variant="primary" disabled={busy || !form.name.trim() || !form.tenant_id.trim() || !form.client_id.trim() || (editing === "new" && !form.client_secret)} onClick={() => void save()}>{t("verify_save")}</Button></div>
    </Dialog> : null}
    {templatesFor ? <Dialog title={t("templates_title", { name: templatesFor.display_name })} onClose={() => setTemplatesFor(null)}>
      <p>{t("templates_intro")}</p>
      {templates.map((item) => <div key={item.id} className="legacy-tools-pbi-template"><strong>{item.name}</strong><pre className="legacy-tools-pbi-template-dax">{item.dax_template}</pre><Button size="sm" variant="danger" onClick={() => setTemplateRemoving(item)}>{t("remove")}</Button></div>)}
      <Field id="powerbi-template-name" label={t("template_name")}><input id="powerbi-template-name" className="ui-input" value={templateName} onChange={(event) => setTemplateName(event.target.value)} /></Field>
      <Field id="powerbi-template-dax" label="DAX"><textarea id="powerbi-template-dax" className="ui-input" rows={4} value={templateDax} onChange={(event) => setTemplateDax(event.target.value)} placeholder="EVALUATE ..." /></Field>
      {error ? <Notice tone="error">{error}</Notice> : null}
      <Button variant="primary" disabled={busy || !templateName.trim() || !templateDax.trim()} onClick={() => void addTemplate()}>{t("add_template")}</Button>
    </Dialog> : null}
    {removing ? <ConfirmDialog title={t("delete_title", { name: removing.display_name })} description={t("delete_description")} confirmLabel={t("delete")} cancelLabel={t("cancel")} busy={busy} onCancel={() => setRemoving(null)} onConfirm={() => void remove()} /> : null}
    {templateRemoving ? <ConfirmDialog title={t("remove_template_title", { name: templateRemoving.name })} description={t("remove_template_description")} confirmLabel={t("remove")} cancelLabel={t("cancel")} busy={busy} onCancel={() => setTemplateRemoving(null)} onConfirm={() => void removeTemplate()} /> : null}
  </>;
}
