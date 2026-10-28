import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { type ReactNode, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";
import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import type { TeamTemplateDTO } from "@/lib/kb-matrix/team-template-api";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  listDocTypes: vi.fn(),
  listMatrices: vi.fn(),
  listTeam: vi.fn(),
  publish: vi.fn(),
  revoke: vi.fn(),
  copy: vi.fn(),
}));

vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({
  useIsMobile: () => false,
}));

vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return {
    ...actual,
    listDocTypesWithStats: (...args: unknown[]) =>
      mocks.listDocTypes(...args),
  };
});

vi.mock("@/lib/kb-matrix/snapshot-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-matrix/snapshot-api")>();
  return {
    ...actual,
    listMatrices: (...args: unknown[]) => mocks.listMatrices(...args),
  };
});

vi.mock("@/lib/kb-matrix/team-template-api", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/lib/kb-matrix/team-template-api")>();
  return {
    ...actual,
    listTeamTemplates: (...args: unknown[]) => mocks.listTeam(...args),
    publishTemplateToTeam: (...args: unknown[]) => mocks.publish(...args),
    revokeTeamTemplate: (...args: unknown[]) => mocks.revoke(...args),
    copyTeamTemplate: (...args: unknown[]) => mocks.copy(...args),
  };
});

import { TemplateManager } from "./template-manager";

const docType: KbDocTypeDTO = {
  doc_type_id: "dt_contract",
  workspace_id: "ws_target",
  name: "合同审阅",
  description: "",
  fields: [
    {
      name: "金额",
      type: "currency",
      description: "",
      required: false,
      cross_verify: false,
      extraction_hint: "",
    },
  ],
  record_count: 0,
  extractor_model: "qwen-plus",
  cross_verify_enabled: true,
  created_by: "u_me",
  created_at: "2026-07-28T00:00:00Z",
  updated_at: "2026-07-28T00:00:00Z",
  kind: "template",
};

const teamEntry: TeamTemplateDTO = {
  entry_id: "ot_contract",
  source_doc_type_id: docType.doc_type_id,
  name: docType.name,
  description: "",
  field_count: 1,
  field_preview: [{ name: "金额", type: "currency" }],
  source_workspace_id: "ws_source",
  source_workspace_name: "法务",
  published_by: "u_publisher",
  published_by_name: "发布者",
  published_at: "2026-07-28T00:00:00Z",
  updated_at: "2026-07-28T00:00:00Z",
  is_own_space: false,
  already_copied: false,
};

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    unpublish: "撤销发布",
    publish:
      "模板体积过大，无法发布。请精简字段定义后重试",
    revoke: "只有模板创建者、发布者或管理员可以执行此操作",
    copy:
      "此团队模板的字段定义已损坏，无法复制。请联系发布者重新发布",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    unpublish: "Unpublish",
    publish:
      "This template is too large to publish. Simplify its field definitions and try again.",
    revoke:
      "Only the template creator, publisher, or an administrator can do this.",
    copy:
      "This team template has invalid field definitions and can't be copied. Ask the publisher to republish it.",
  },
] as const;

function Harness({ entry }: { entry: (typeof locales)[number] }) {
  const [toast, setToast] = useState<ReactNode>("");
  return (
    <NextIntlClientProvider locale={entry.locale} messages={entry.messages}>
      <div data-testid="team-template-error-toast">{toast}</div>
      <TemplateManager
        workspaceId="ws_target"
        canEdit
        uid="u_me"
        isAdmin={false}
        onApplyTemplate={() => undefined}
        onOpenMatrix={() => undefined}
        toast={(_kind, content) => setToast(content)}
        onNewMatrix={() => undefined}
        onViewRecords={() => undefined}
      />
    </NextIntlClientProvider>
  );
}

function renderManager(entry: (typeof locales)[number]) {
  return render(<Harness entry={entry} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listDocTypes.mockResolvedValue({
    items: [docType],
    stats: {},
  });
  mocks.listMatrices.mockResolvedValue([]);
  mocks.listTeam.mockResolvedValue([teamEntry]);
});

afterEach(() => {
  cleanup();
});

describe.each(locales)("TemplateManager errors — $locale", (entry) => {
  it("localizes publish validation without rendering backend prose", async () => {
    mocks.listTeam.mockResolvedValue([]);
    mocks.publish.mockRejectedValue(
      new MatrixApiError(
        422,
        "schema_too_large",
        "RAW_SCHEMA_DIAGNOSTIC",
        {},
      ),
    );

    renderManager(entry);
    fireEvent.click(
      await screen.findByTestId("kb-otpl-publish-dt_contract"),
    );
    fireEvent.click(screen.getByTestId("kb-otpl-publish-confirm"));

    expect(
      await screen.findByText(entry.publish),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("schema_too_large");
    expect(document.body.textContent).not.toContain(
      "RAW_SCHEMA_DIAGNOSTIC",
    );
  });

  it("localizes revoke authorization without rendering backend prose", async () => {
    mocks.revoke.mockRejectedValue(
      new MatrixApiError(
        403,
        "revoke_not_allowed",
        "RAW_AUTHZ_DIAGNOSTIC",
        {},
      ),
    );

    renderManager(entry);
    fireEvent.click(
      await screen.findByRole("button", { name: entry.unpublish }),
    );
    fireEvent.click(screen.getByTestId("kb-otpl-unpublish-confirm"));

    expect(await screen.findByText(entry.revoke)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("revoke_not_allowed");
    expect(document.body.textContent).not.toContain(
      "RAW_AUTHZ_DIAGNOSTIC",
    );
  });

  it("localizes invalid copied schema without rendering backend prose", async () => {
    mocks.copy.mockRejectedValue(
      new MatrixApiError(
        422,
        "invalid_template_schema",
        "RAW_CORRUPT_SCHEMA",
        {},
      ),
    );

    renderManager(entry);
    fireEvent.click(
      await screen.findByTestId("kb-otpl-copy-ot_contract"),
    );

    await waitFor(() =>
      expect(
        screen.getByTestId("team-template-error-toast"),
      ).toHaveTextContent(entry.copy),
    );
    expect(document.body.textContent).not.toContain(
      "invalid_template_schema",
    );
    expect(document.body.textContent).not.toContain("RAW_CORRUPT_SCHEMA");
  });
});
