import { Suspense } from "react";
import { AgentManagementRoute } from "@/components/agents/agent-management-route";
import { AgentPageFrame } from "@/components/agents/agent-page-frame";

export default function AgentManagementPage() {
  return <AgentPageFrame page="manage" allowOwnerDeepLink><Suspense fallback={null}><AgentManagementRoute /></Suspense></AgentPageFrame>;
}
