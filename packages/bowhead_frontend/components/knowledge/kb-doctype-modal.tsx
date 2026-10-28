"use client";

/**
 * kb-management M2 · bind doc_type to selected file(s).
 * Radio list of the workspace's doc_types (+ unbind). Applies via the bulk
 * doc_type endpoint (works for 1 or N files). Binding also queues extraction
 * server-side (kb-management D5: rate-limited, async); unbind does not.
 */
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { DialogShell } from "@/components/ui/dialog";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { kbFileError } from "@/lib/kb-file-error";
import { bulkBindKbFilesDocType } from "@/lib/kb-api";
import { type KbDocTypeDTO, listKnowledgeTables } from "@/lib/kb-knowledge-tables-api";

import type { NotifyFn } from "@/lib/use-toast";

interface Props {
  fileIds: string[];
  workspaceId: string;
  onClose: () => void;
  onApplied: () => void;
  toast: NotifyFn;
}

export function KbDocTypeModal({ fileIds, workspaceId, onClose, onApplied, toast }: Props) {
  const t = useTranslations("knowledge_base.doctype_modal");
  const tKb = useTranslations("knowledge_base");
  const tm = useTranslations("kbMatrix");
  const [docTypes, setDocTypes] = useState<KbDocTypeDTO[] | null>(null);
  const [chosen, setChosen] = useState<string | null>(null); // doc_type_id, "" = unbind
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listKnowledgeTables(workspaceId)
      // kb-matrix M4 gate① (decision 12): column-group templates (seed/template/graduated) are not
      // extraction-binding targets—picker lists only manual (server 422 doc_type_not_bindable is the backstop).
      .then((rows) => { if (!cancelled) setDocTypes(rows.filter((d) => (d.kind ?? "manual") === "manual")); })
      .catch(() => { if (!cancelled) setDocTypes([]); });
    return () => { cancelled = true; };
  }, [workspaceId]);

  const apply = async () => {
    if (chosen === null) return;
    setBusy(true);
    try {
      const docTypeId = chosen === "" ? null : chosen;
      const res = await bulkBindKbFilesDocType(fileIds, docTypeId);
      const label = chosen === ""
        ? t("unbind_label")
        : `「${docTypes?.find((d) => d.doc_type_id === chosen)?.name ?? "doc_type"}」`;
      if (res.failed > 0) {
        toast("error", t("toast.partial", { ok: res.succeeded, failed: res.failed }));
      } else if (chosen === "") {
        toast("success", t.rich("toast.unbound", { n: res.succeeded, b: (c) => <b>{c}</b> }));
      } else if (docTypes?.find((d) => d.doc_type_id === chosen)?.auto_extract_enabled === false) {
        // M4 registered item: binding succeeded but extraction disabled for this table (worker gate③ silently refuses)—tell the user accurately.
        toast("success", tm("bind_extract_disabled"));
      } else {
        toast("success", t.rich("toast.bound", { n: res.succeeded, label, b: (c) => <b>{c}</b> }));
      }
      onApplied();
    } catch (e) {
      toast(
        "error",
        t("toast.bind_failed", { error: tKb(kbFileError(e, "bind")) }),
      );
    } finally {
      setBusy(false);
    }
  };

  // Title asks “Which table should this file be extracted into?”; singular/bulk wording differs.
  const title = t("title", { n: fileIds.length });

  return (
    <DialogShell onClose={onClose} busy={busy} label={title} backdropClassName="kb-overlay" panelClassName="kb-modal">
      <div className="kb-modal-head">
        <h3 id="kb-doctype-modal-title">{title}</h3>
        <div className="sub">{t("sub", { n: fileIds.length })}</div>
      </div>
      <div className="kb-modal-body">
        {docTypes === null && <div className="kb-skel" style={{ width: "60%", height: 16 }} />}
        {docTypes && docTypes.length === 0 && (
          <div className="kb-notice">{tm("bind_empty")}</div>
        )}
        {docTypes && docTypes.length > 0 && (
          // Cards are radio options: arrows/Home/End move and select; Tab stops only at selection (component unification P1 step 6);
          // while applying, busy ignores selection changes and preserves focus.
          <RadioGroup
            orientation="both"
            aria-labelledby="kb-doctype-modal-title"
            value={chosen}
            onValueChange={setChosen}
            busy={busy}
          >
            {docTypes.map((d) => (
              <Radio
                key={d.doc_type_id}
                value={d.doc_type_id}
                data-testid={`kb-doctype-option-${d.doc_type_id}`}
                className={`kb-scope-card ${chosen === d.doc_type_id ? "on" : ""}`}
              >
                <span className="radio" />
                <span>
                  <span className="title">{d.name}</span>
                  <span className="desc">
                    {d.cardinality === "many" ? t("cardinality_many") : t("cardinality_one")}
                  </span>
                </span>
              </Radio>
            ))}
            <Radio
              value=""
              data-testid="kb-doctype-option-unbind"
              className={`kb-scope-card ${chosen === "" ? "on" : ""}`}
            >
              <span className="radio" />
              <span>
                <span className="title">{t("unbind_title")}</span>
                <span className="desc">{t("unbind_desc")}</span>
              </span>
            </Radio>
          </RadioGroup>
        )}
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose} disabled={busy}>{t("cancel")}</button>
        <button type="button" className="kb-btn kb-btn-primary" onClick={apply} disabled={chosen === null || busy}>
          {busy ? t("applying") : t("apply")}
        </button>
      </div>
    </DialogShell>
  );
}
