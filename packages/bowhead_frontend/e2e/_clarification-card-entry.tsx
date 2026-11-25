/**
 * Question card fixtures for clarification-card-narrow.spec.ts: every footer and head variant of the chat
 * ClarificationCard at a fixed card width (`?width=`), in the four product locales (`?locale=`).
 */
import "./_process-shim";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { ClarificationCard } from "../components/chat/step-list/ClarificationCard";
import type { ClarificationAnswer, ThetaClarificationQuestion } from "../lib/types";
import de from "../messages/de-DE.json";
import en from "../messages/en-US.json";
import fr from "../messages/fr-FR.json";
import zh from "../messages/zh-CN.json";

const MESSAGES = { "zh-CN": zh, "en-US": en, "de-DE": de, "fr-FR": fr } as const;
type Locale = keyof typeof MESSAGES;

const query = new URLSearchParams(location.search);
const locale: Locale = (query.get("locale") as Locale | null) ?? "en-US";
const width = Number(query.get("width") ?? 640);

const COPY = {
  "zh-CN": {
    single: "你想按哪个口径统计 10 月的销售额？",
    singleOptions: ["含税成交额", "不含税成交额", "扣除退货后的净销售额"],
    workspace: "这次查询使用哪个 Datarc 工作区？",
    workspaceOptions: ["华东零售", "全国门店"],
    multi: [
      "统计哪个时间范围？",
      "需要拆分哪些维度？",
      "还有其他要求吗？",
    ],
    multiOptions: [["最近 30 天", "本季度", "今年至今"], ["门店", "品类", "渠道", "会员等级"]],
  },
  "en-US": {
    single: "Which revenue definition should October's sales use?",
    singleOptions: ["Gross sales including tax", "Net sales excluding tax", "Net sales after returns"],
    workspace: "Which Datarc workspace should this query use?",
    workspaceOptions: ["East China retail", "All stores"],
    multi: [
      "Which period should the analysis cover?",
      "Which dimensions should the breakdown use?",
      "Anything else to keep in mind?",
    ],
    multiOptions: [["Last 30 days", "This quarter", "Year to date"], ["Store", "Category", "Channel", "Member tier"]],
  },
} as const;
const copy = COPY[locale === "zh-CN" ? "zh-CN" : "en-US"];

const single: ThetaClarificationQuestion[] = [
  { question: copy.single, options: [...copy.singleOptions], type: "single_select" },
];
const workspace: ThetaClarificationQuestion[] = [
  { question: copy.workspace, options: [...copy.workspaceOptions], type: "single_select" },
];
const multi: ThetaClarificationQuestion[] = [
  { question: copy.multi[0], options: [...copy.multiOptions[0]], type: "single_select" },
  { question: copy.multi[1], options: [...copy.multiOptions[1]], type: "multi_select" },
  { question: copy.multi[2], options: [], type: "free_text" },
];
const firstAnswered: ClarificationAnswer[] = [
  { question: copy.multi[0], source: "candidate", answer: copy.multiOptions[0][1], option_index: 1 },
];

const noop = () => {};
const handlers = { onSubmitSingle: noop, onSubmitMulti: noop, onSkip: noop, onCancel: noop };

function Case({ id, children }: { id: string; children: ReactNode }) {
  return (
    <section data-case={id} style={{ width, marginBottom: 24 }}>
      <h2 style={{ font: "600 12px system-ui", margin: "0 0 4px", color: "var(--c-ink-3)" }}>{id}</h2>
      <div className="bh-bot-block">{children}</div>
    </section>
  );
}

function Host() {
  return (
    <NextIntlClientProvider locale={locale} messages={MESSAGES[locale]}>
      <main>
        <Case id="single">
          <ClarificationCard questions={single} {...handlers} />
        </Case>
        <Case id="single-answered">
          <ClarificationCard
            questions={single}
            restoredAnswers={[{ question: copy.single, source: "candidate", answer: copy.singleOptions[0], option_index: 0 }]}
            {...handlers}
          />
        </Case>
        <Case id="multi">
          <ClarificationCard questions={multi} {...handlers} />
        </Case>
        <Case id="multi-answered">
          <ClarificationCard questions={multi} restoredAnswers={firstAnswered} {...handlers} />
        </Case>
        <Case id="remember">
          <ClarificationCard questions={workspace} showRemember allowCustomAnswers={false} {...handlers} />
        </Case>
        <Case id="document">
          <ClarificationCard questions={single} documentDraft {...handlers} />
        </Case>
        <Case id="error">
          <ClarificationCard
            questions={multi}
            restoredAnswers={firstAnswered}
            error={locale === "zh-CN" ? "提交失败：问题已过期。请刷新后重新回答。" : "Could not submit: the question expired. Refresh and answer again."}
            {...handlers}
          />
        </Case>
        <Case id="pending">
          <ClarificationCard
            questions={single}
            unavailableReason={MESSAGES[locale].clarification.pause_pending}
            {...handlers}
          />
        </Case>
      </main>
    </NextIntlClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(<Host />);
