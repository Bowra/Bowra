import { AgentCreateEntry } from "@/components/agents/agent-create-entry";
import { AgentPageFrame } from "@/components/agents/agent-page-frame";

export default function AgentCreatePage() {
  return <AgentPageFrame page="create"><AgentCreateEntry /></AgentPageFrame>;
}
