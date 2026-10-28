import { AgentDashboard } from "@/components/agents/agent-dashboard";
import { AgentPageFrame } from "@/components/agents/agent-page-frame";

export default function AgentDashboardPage() {
  return <AgentPageFrame page="dashboard" adminOnly><AgentDashboard /></AgentPageFrame>;
}
