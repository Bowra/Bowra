"use client";

import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslations } from "next-intl";

import { ShareDialog } from "@/components/share/share-dialog";
import { ShIcon } from "@/components/share/share-ui";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { getFileShares } from "@/lib/file-share";

type FileShareActionProps = {
  artifactId: string;
  filename: string;
  renderTrigger?: (openShare: (() => void) | undefined) => ReactNode;
  returnFocusRef?: RefObject<HTMLElement | null>;
};

export function FileShareAction(props: FileShareActionProps) {
  const identity = useOptionalAuthSession()?.me;
  const key = JSON.stringify([props.artifactId, identity?.id, identity?.team_id, identity?.auth_session_generation]);
  return <FileShareActionForFile key={key} {...props} />;
}

function FileShareActionForFile({ artifactId, filename, renderTrigger, returnFocusRef }: FileShareActionProps) {
  const t = useTranslations("share");
  const button = useRef<HTMLButtonElement>(null);
  const [canManage, setCanManage] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void getFileShares(artifactId, controller.signal).then(() => {
      if (!controller.signal.aborted) setCanManage(true);
    }).catch(() => {});
    return () => controller.abort();
  }, [artifactId]);

  const openShare = canManage ? () => setOpen(true) : undefined;
  return <>
    {renderTrigger ? renderTrigger(openShare) : openShare ? (
      <button ref={button} type="button" className="v2-icon-btn results-report-share" aria-haspopup="dialog"
        aria-label={t("header_eyebrow")} title={t("header_eyebrow")} onClick={openShare}>
        <ShIcon name="share" size={15} />
      </button>
    ) : null}
    {open ? <ShareDialog kind="file" resourceId={artifactId} label={filename}
      returnFocusRef={returnFocusRef ?? button} onClose={() => setOpen(false)} /> : null}
  </>;
}
