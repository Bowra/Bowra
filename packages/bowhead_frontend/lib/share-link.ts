import { copyTextWithFallback } from "@/lib/clipboard";
import { staticTranslator } from "@/lib/locale-messages";
import { toast } from "@/lib/use-toast";

export async function copyShareLink(href: string, copiedMessage: string): Promise<boolean> {
  const t = staticTranslator("report_share_link");
  const copied = await copyTextWithFallback(href, t("copy_prompt"));
  if (copied) toast({ kind: "success", text: copiedMessage });
  return copied;
}
