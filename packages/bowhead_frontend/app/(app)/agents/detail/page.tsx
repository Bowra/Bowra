import { Suspense } from "react";
import { AgentDetail } from "@/components/agents/agent-detail";
import { AgentPageFrame } from "@/components/agents/agent-page-frame";

export default function AgentDetailPage() {
  return <AgentPageFrame><Suspense fallback={null}><AgentDetail /></Suspense></AgentPageFrame>;
}
