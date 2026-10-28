import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import {
  ReferenceParameterPanel,
  type ReferenceParameterIntent,
  type ReferenceParameterTarget,
} from "../components/chat/reference-parameter-panel";
import type { InputDocument } from "../lib/composer-input-document";
import en from "../messages/en-US.json";
import zh from "../messages/zh-CN.json";

const scope = {
  userId: "u_test",
  teamId: "t_test",
  workspaceId: "ws_test",
  requestedSessionId: null,
  authSessionGeneration: "auth_test",
};
const resource = {
  kind: "skill" as const,
  source_kind: "user" as const,
  id: "profitability",
  version: `sha256:${"a".repeat(64)}`,
};
const nodeId = "11111111-1111-1111-1111-111111111111";
const document: InputDocument = {
  schema_version: 1,
  nodes: [
    {
      type: "reference",
      node_id: nodeId,
      resource,
      label: "杜邦分析 · Profitability analysis",
      parameters: { detail: { units: 3 } },
    },
  ],
};
const schema = {
  type: "object",
  additionalProperties: false,
  required: ["company"],
  properties: {
    company: {
      type: "string",
      title: "Company / 公司",
      description:
        "用于本次盈利能力分析的公司。The company for this profitability analysis.",
      default: "Example",
    },
    count: {
      type: "integer",
      title: "Years / 年数",
      minimum: 1,
      maximum: 20,
      default: 3,
    },
    date: {
      type: ["string", "null"],
      title: "End Date / 截止日期",
      format: "date",
    },
    enabled: { type: "boolean", title: "Compare / 对比", default: false },
    period: {
      type: "string",
      title: "Period / 周期",
      enum: ["year", "quarter"],
      default: "year",
    },
    note: { type: ["string", "null"], title: "Note / 备注" },
    detail: {
      type: "object",
      title: "Calculation Details / 计算细节",
      properties: { units: { type: "integer" } },
      additionalProperties: false,
    },
  },
};
type State = {
  scope: ReferenceParameterTarget["scope"];
  document: InputDocument;
  target: ReferenceParameterTarget | null;
  locale: "zh-CN" | "en-US";
  readOnly: boolean;
};
declare global {
  interface Window {
    referenceParameterPanel: {
      commits: ReferenceParameterIntent[];
      dismissals: string[];
      keys: string[];
      update: (patch: Partial<State>) => void;
    };
  }
}
const controller = {
  commits: [] as ReferenceParameterIntent[],
  dismissals: [] as string[],
  keys: [] as string[],
  update: (_patch: Partial<State>) => {},
};
window.referenceParameterPanel = controller;
function Harness() {
  const [state, setState] = useState<State>({
    scope,
    document,
    target: null,
    locale: "zh-CN",
    readOnly: false,
  });
  controller.update = (patch) => setState((value) => ({ ...value, ...patch }));
  return (
    <NextIntlClientProvider
      locale={state.locale}
      messages={state.locale === "zh-CN" ? zh : en}
    >
      <main onKeyDown={(event) => controller.keys.push(event.key)}>
        <h1>Parameter panel contract</h1>
        <button
          id="open-parameters"
          type="button"
          onClick={() =>
            setState((value) => ({
              ...value,
              target: {
                scope: value.scope,
                document: value.document,
                resource,
                nodeId,
                editId: crypto.randomUUID(),
                parameterSchema: schema,
              },
            }))
          }
        >
          Edit parameters
        </button>
        <ReferenceParameterPanel
          {...state}
          onDismiss={(editId) => {
            controller.dismissals.push(editId);
            setState((value) =>
              value.target?.editId === editId
                ? { ...value, target: null }
                : value,
            );
          }}
          onCommitIntent={(intent) => {
            controller.commits.push(intent);
            setState((value) =>
              value.target?.editId === intent.editId
                ? {
                    ...value,
                    target: null,
                    document: {
                      ...value.document,
                      nodes: value.document.nodes.map((node) =>
                        node.type === "reference" &&
                        node.node_id === intent.nodeId
                          ? { ...node, parameters: intent.parameters }
                          : node,
                      ),
                    },
                  }
                : value,
            );
          }}
        />
      </main>
    </NextIntlClientProvider>
  );
}
createRoot(window.document.getElementById("root")!).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
