import type { ReactNode } from "react";
import { CharIcon } from "@/components/char-icon";

type AdminRouteStateTone = "loading" | "forbidden" | "error";

interface AdminRouteStateProps {
  title: ReactNode;
  children: ReactNode;
  tone?: AdminRouteStateTone;
  /** Rendered under the message (e.g. "who can help" on a forbidden state). */
  aside?: ReactNode;
}

export function AdminRouteState({
  title,
  children,
  tone = "loading",
  aside,
}: AdminRouteStateProps) {
  return (
    <div className={`admin-route-state is-${tone}`}>
      <div
        className="admin-route-state__card"
        role={tone === "loading" ? "status" : "alert"}
      >
        <div className="admin-route-state__mark" aria-hidden="true">
          {tone === "loading" ? "…" : tone === "forbidden" ? <CharIcon ch="⊘" aria-hidden /> : "!"}
        </div>
        <h1>{title}</h1>
        <p>{children}</p>
        {aside}
      </div>
    </div>
  );
}
