"use client";

import { useEffect, useState, type MouseEvent } from "react";
import { useTranslations } from "next-intl";

import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { CharIcon } from "@/components/char-icon";

interface RepairContext {
  agentId: string;
  dependency: string;
}

export function CapabilityRepairContext({
  kind,
  onDependency,
}: {
  kind: "skill" | "tool";
  onDependency: (dependency: string) => void;
}) {
  const t = useTranslations("capability_repair");
  const router = useAppRouter();
  const [context, setContext] = useState<RepairContext | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const agentId = params.get("from_agent")?.trim() ?? "";
    const dependency = params.get("dependency")?.trim() ?? "";
    if (!agentId || agentId.length > 160 || !dependency || dependency.length > 240) return;
    setContext({ agentId, dependency });
    onDependency(dependency);
  }, [onDependency]);

  if (!context) return null;
  const returnHref = `/agents/detail/?${new URLSearchParams({ id: context.agentId })}`;
  const navigate = (event: MouseEvent<HTMLAnchorElement>) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(returnHref);
  };

  return (
    <aside aria-label={t("label")} className="cap-repair-context">
      <span>{t(kind === "skill" ? "skill_context" : "tool_context", { dependency: context.dependency })}</span>
      <a
        href={appPath(returnHref)}
        onClick={navigate}
      >
        <CharIcon ch="←" /> {t("back_to_agent")}
      </a>
    </aside>
  );
}
