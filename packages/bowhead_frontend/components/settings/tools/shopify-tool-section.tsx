"use client";

import { useEffect, useState } from "react";
import { codedErrorMessage } from "@/lib/user-facing-error";
import { useTranslations } from "next-intl";
import { ShoppingBag } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import {
  createShopifyPlugin,
  createShopifyContentPlugin,
  createShopifyTool,
  deleteShopifyTool,
  setShopifyEnabled,
  updateShopifyTool,
  type ShopifyToolInput,
  type TeamBuiltinTool,
} from "@/lib/team-builtin-tools-api";

const empty: ShopifyToolInput = { name: "", shop_domain: "", client_id: "", client_secret: "" };

// Coded Shopify failures carry localized catalog copy from the gateway; provider text never does.
const SHOPIFY_ERROR_CODES = ["shopify_", "capability_", "builtin_tool_"] as const;

export function ShopifyToolSection({
  tools,
  createRequest,
  onChanged,
}: {
  tools: TeamBuiltinTool[];
  createRequest: number;
  onChanged: () => Promise<void>;
}) {
  const t = useTranslations("shopify_tool");
  const [editing, setEditing] = useState<TeamBuiltinTool | "new" | null>(null);
  const [removing, setRemoving] = useState<TeamBuiltinTool | null>(null);
  const [form, setForm] = useState<ShopifyToolInput>(empty);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function close() {
    setEditing(null);
    setForm(empty);
    setError("");
  }

  useEffect(() => {
    if (createRequest > 0) {
      setForm(empty);
      setError("");
      setEditing("new");
    }
  }, [createRequest]);

  function showError(cause: unknown) {
    const code = cause && typeof cause === "object" && "code" in cause ? String(cause.code) : "";
    setError(
      code === "shopify_connection_changed"
        ? t("changed")
        : code === "shopify_name_conflict" || code === "capability_name_conflict"
          ? t("name_conflict")
          : code === "shopify_scope_unavailable" || code === "shopify_access_denied"
            ? t("access_denied")
            : codedErrorMessage(cause, t("failed"), SHOPIFY_ERROR_CODES),
    );
  }

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (cause) {
      showError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    const target = editing;
    if (!target) return;
    await run(async () => {
      if (target === "new") await createShopifyTool(form);
      else
        await updateShopifyTool(target.id, {
          name: form.name,
          client_id: form.client_id,
          client_secret: form.client_secret || undefined,
          connection_revision: target.connection_revision!,
        });
      close();
      await onChanged();
    });
  }

  return (
    <>
      {tools.map((tool) => (
        <div key={tool.id} className={`legacy-tools-pbi-row${tool.status === "enabled" ? "" : " is-off"}`}>
          <ShoppingBag size={22} aria-hidden="true" />
          <div className="legacy-tools-pbi-main">
            <strong>{tool.display_name}</strong>
            <div className="legacy-tools-pbi-meta">
              {tool.shop_domain} · {t("operation_count", { count: tool.supported_tools.length })}
            </div>
          </div>
          <Button
            size="sm"
            variant="secondary"
            disabled={busy || tool.status !== "enabled"}
            onClick={() =>
              void run(async () => {
                await createShopifyPlugin(tool.id);
                setNotice(t("plugin_created"));
              })
            }
          >
            {t("create_plugin")}
          </Button>
          {tool.supported_tools.includes("shopify_preview_content_update") ? (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || tool.status !== "enabled"}
              onClick={() =>
                void run(async () => {
                  await createShopifyContentPlugin(tool.id);
                  setNotice(t("content_plugin_created"));
                })
              }
            >
              {t("create_content_plugin")}
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            onClick={() => {
              setEditing(tool);
              setError("");
              setForm({ name: tool.name, shop_domain: tool.shop_domain ?? "", client_id: tool.client_id ?? "", client_secret: "" });
            }}
          >
            {t("edit")}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await setShopifyEnabled(tool.id, tool.status !== "enabled", tool.connection_revision!);
                await onChanged();
              })
            }
          >
            {tool.status === "enabled" ? t("disable") : t("enable")}
          </Button>
          <Button size="sm" variant="danger" disabled={busy} onClick={() => setRemoving(tool)}>
            {t("delete")}
          </Button>
        </div>
      ))}
      {error && !editing ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}
      {editing ? (
        <Dialog title={editing === "new" ? t("add_title") : t("edit_title")} onClose={busy ? () => undefined : close}>
          <p>{t("intro")}</p>
          {(["name", "shop_domain", "client_id", "client_secret"] as const).map((key) => (
            <Field key={key} id={`shopify-${key}`} label={t(key)}>
              <input
                id={`shopify-${key}`}
                className="ui-input"
                type={key === "client_secret" ? "password" : "text"}
                autoComplete="off"
                disabled={busy || (key === "shop_domain" && editing !== "new")}
                maxLength={key === "name" ? 60 : key === "client_secret" ? 4096 : 128}
                value={form[key]}
                onChange={(event) => setForm((current) => ({ ...current, [key]: event.target.value }))}
                placeholder={key === "client_secret" && editing !== "new" ? t("keep_secret") : key === "shop_domain" ? "store.myshopify.com" : ""}
              />
            </Field>
          ))}
          <p className="legacy-tools-pbi-note">{t("app_note")}</p>
          {error ? <Notice tone="error">{error}</Notice> : null}
          <div className="legacy-tools-pbi-actions">
            <Button variant="secondary" disabled={busy} onClick={close}>
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={busy || !form.name.trim() || !form.shop_domain.trim() || !form.client_id.trim() || (editing === "new" && !form.client_secret)}
              onClick={() => void save()}
            >
              {t("verify_save")}
            </Button>
          </div>
        </Dialog>
      ) : null}
      {removing ? (
        <ConfirmDialog
          title={t("delete_title", { name: removing.display_name })}
          description={t("delete_note")}
          confirmLabel={t("delete")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setRemoving(null)}
          onConfirm={() =>
            void run(async () => {
              await deleteShopifyTool(removing.id);
              setRemoving(null);
              await onChanged();
            })
          }
        />
      ) : null}
    </>
  );
}
