"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { generateSkill, type SkillDetailDTO } from "@/lib/api";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { ErrorNotice } from "@/components/error-notice";
import { DialogShell } from "@/components/ui/dialog";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { X } from "@/components/icons";

export function SkillCreateModal({
  onClose,
  onCreated,
  presentError,
}: {
  onClose: () => void;
  onCreated: (skill: SkillDetailDTO) => void;
  presentError: (error: unknown) => ErrorPresentation;
}) {
  const t = useTranslations("skills_page");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const close = async () => {
    if (busy) return;
    if (name || description || body) {
      const ok = await confirm({
        title: t("drawer.discard_title"),
        description: t("drawer.discard_confirm"),
        confirmLabel: t("drawer.discard_action"),
        cancelLabel: t("drawer.discard_keep"),
      });
      if (!ok) return;
    }
    onClose();
  };
  useEffect(() => {
    if (!name && !description && !body) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [name, description, body]);
  return (
    <DialogShell
      onClose={() => void close()}
      busy={busy}
      label={t("create.title")}
      mount="inline"
      backdropClassName="scrim"
      panelClassName="modal skill-create-modal"
    >
      {/* The shell div is the panel; the form only handles submission. display:contents keeps form children directly in .modal's flex layout. */}
      <form
        style={{ display: "contents" }}
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          try {
            const skill = await generateSkill(`---\nname: ${name.trim()}\ndescription: ${JSON.stringify(description.trim())}\n---\n${body}`);
            onCreated(skill);
            onClose();
          } catch (error) {
            setError(presentError(error));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="modal-head">
          <h2>{t("create.title")}</h2>
          <button type="button" className="drawer-x" disabled={busy} aria-label={t("drawer.close")} onClick={() => void close()}>
            <X size={15} />
          </button>
        </div>
        <div className="modal-body skill-create-fields">
          <label>
            {t("create.name")}
            <input
              required
              pattern="[a-z0-9][a-z0-9-]{0,63}"
              maxLength={64}
              disabled={busy}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-skill"
            />
          </label>
          <label>
            {t("create.description")}
            <textarea required maxLength={2048} disabled={busy} value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <label>
            {t("create.body")}
            <textarea required rows={10} disabled={busy} value={body} onChange={(e) => setBody(e.target.value)} />
          </label>
          <p>{t("create.hint")}</p>
          {error ? <ErrorNotice message={error.message} diagnostics={error.diagnostics} surface="settings" diagnosticsEnabled /> : null}
        </div>
        <div className="modal-foot">
          <button type="button" className="btn ghost" disabled={busy} onClick={() => void close()}>
            {t("cancel")}
          </button>
          <button type="submit" className="btn pri" disabled={busy}>
            {busy ? t("drawer.saving") : t("create.title")}
          </button>
        </div>
      </form>
      {/* Put confirmation inside the shell to make it a child dialog: Esc closes only confirmation. */}
      {confirmDialog}
    </DialogShell>
  );
}
