import "./_process-shim";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { MemberFeedbackView } from "../components/agents/agent-member-feedback";
import { MessageActions } from "../components/chat/message-actions";
import { ToastContainer } from "../components/chat/toast-container";
import zh from "../messages/zh-CN.json";
import "../styles/ui-refresh-workbench.css";
import "../styles/agent-marketplace.css";

/**
 * Real-browser fixture for the thumbs-down on team Agent answers (Claude Design "Agent Eval Alignment
 * Loop" L0-a…L0-d): the real answer action bar with its dialog and Undo toast, and the Owner's real
 * Pending row. The spec supplies every API through page.route; nothing is persisted.
 */
declare global {
  interface Window {
    answerFeedbackEvents: string[];
  }
}
window.answerFeedbackEvents = [];

const PENDING = [
  {
    turn_id: "t_shared",
    shared_by: "陈晓",
    question: "华东 9 月销售额同比变化多少？",
    answered_at: "2026-10-17T06:20:00Z",
    feedback: { score: -1 as const, reason_codes: ["missing_content"], comment: "没写是哪个月的数据，我得自己去查", given_at: "2026-10-17T06:20:00Z" },
  },
  {
    turn_id: "t_silent",
    shared_by: "王磊",
    question: "本月新客数比上月多多少",
    answered_at: "2026-10-18T02:05:00Z",
    feedback: { score: -1 as const, reason_codes: [], comment: null, given_at: "2026-10-18T02:05:00Z" },
  },
];

function Fixture() {
  return (
    <NextIntlClientProvider locale="zh-CN" messages={zh} timeZone="Asia/Shanghai">
      <main className="feedback-fixture">
        <h1>answer feedback</h1>
        <section aria-label="chat" className="feedback-fixture__chat">
          <p className="feedback-fixture__q">华东 9 月销售额同比变化多少？</p>
          <div className="feedback-fixture__a">
            <strong>业务增长分析师</strong>
            <p>华东区销售额为 4,812 万元，同比增长 13.6%。增长主要来自线上渠道（+21%），门店渠道基本持平。</p>
            <MessageActions answerText="华东区销售额为 4,812 万元，同比增长 13.6%。" resendText="华东 9 月销售额同比变化多少？" onResend={() => undefined} latest sessionId="s_1" turnId="t_team" />
          </div>
        </section>
        <section aria-label="owner" className="am-ci feedback-fixture__owner">
          <MemberFeedbackView
            agentId="osa_1"
            agentName="业务增长分析师"
            items={PENDING}
            hidden={new Set()}
            onDismiss={(item) => window.answerFeedbackEvents.push(`dismiss:${item.turn_id}`)}
            onImprove={async (text, markHandled) => {
              await markHandled();
              window.answerFeedbackEvents.push(`improve:${text}`);
            }}
            reload={async () => undefined}
          />
        </section>
        <ToastContainer />
      </main>
    </NextIntlClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><Fixture /></StrictMode>);
