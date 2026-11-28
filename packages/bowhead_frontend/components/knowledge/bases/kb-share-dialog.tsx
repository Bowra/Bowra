"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES): the Share dialog (PRD §4.3; mockups ④, ⑬ as a bottom sheet on phones).
 *
 * Managers of a Space or Team knowledge base get, in order: "who can use it" cards, "add members or Spaces", the
 * list and the summary; changes apply immediately. Everyone else (and My knowledge bases, which cannot be shared)
 * sees the same content read-only under "who has access". "Copy link" copies the page address only: opening it
 * grants nothing.
 */
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { appPath } from "@/lib/base-path";
import { copyTextWithFallback } from "@/lib/clipboard";
import { knowledgeBaseHref } from "@/lib/knowledge-base-display";
import type { KnowledgeBaseDTO } from "@/lib/knowledge-bases-api";
import { useSpaceSelection } from "@/lib/use-space";
import { toast } from "@/lib/use-toast";

import { KbAccessEditor } from "./kb-members-panel";
import { useKnowledgeBaseNamer } from "./use-knowledge-bases";

/** The address of a knowledge base page, with the deployment prefix. */
export function knowledgeBasePageUrl(id: string): string {
  return `${window.location.origin}${appPath(knowledgeBaseHref(id))}`;
}

export function KbShareDialog({ kb, onClose, onChanged }: { kb: KnowledgeBaseDTO; onClose: () => void; onChanged?: () => void }) {
  const t = useTranslations("knowledge_bases.share");
  const tCommon = useTranslations("knowledge_bases.common");
  const tMembers = useTranslations("knowledge_bases.members");
  const nameOf = useKnowledgeBaseNamer();
  const { items: spaces } = useSpaceSelection();
  const name = nameOf(kb);
  const manage = kb.can.manage && kb.home !== "personal";
  const spaceLabel = kb.home === "space" ? (spaces.find((space) => space.id === kb.home_id)?.name ?? tMembers("this_space")) : "";
  const description =
    kb.home === "personal" ? t("desc_personal") : kb.home === "team" ? t("desc_team") : t("desc_space", { space: spaceLabel });

  const copyLink = async () => {
    const copied = await copyTextWithFallback(knowledgeBasePageUrl(kb.id), t("copy_prompt"));
    if (copied) toast({ kind: "success", text: t("copied") });
  };

  return (
    <Dialog
      title={manage ? t("title", { name }) : t("title_readonly", { name })}
      onClose={onClose}
      width={560}
      presentation="sheet"
      footer={
        <>
          <Button variant="plain" className="kbm-copy-link" title={t("copy_link_hint")} onClick={() => void copyLink()}>
            {t("copy_link")}
          </Button>
          <Button variant="primary" onClick={onClose}>
            {manage ? t("done") : tCommon("close")}
          </Button>
        </>
      }
    >
      <p className="settings-dialog__desc kbm-dialog-desc">{description}</p>
      <KbAccessEditor kb={kb} variant="dialog" onChanged={onChanged} />
    </Dialog>
  );
}
