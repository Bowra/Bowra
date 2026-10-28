/**
 * i18n batch 3B (scattered files): after moving copy into messages, all four UI languages retrieve their own text;
 * zh-CN matches the pre-migration original verbatim; en-US / de-DE / fr-FR output contains no Han characters.
 */
import { cleanup, render, renderHook } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";

const copied: string[] = [];
const toasts: string[] = [];
vi.mock("@/lib/clipboard", () => ({
  copyTextWithFallback: vi.fn(async (_href: string, label: string) => {
    copied.push(label);
    return true;
  }),
}));
vi.mock("@/lib/use-toast", () => ({ toast: (t: { text: string }) => toasts.push(t.text) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchWorkspaces: () => new Promise(() => {}),
}));

import { UnknownSection } from "@/components/canvas/sections/unknown-section";
import { typeMeta } from "@/components/knowledge/kb-file-types";
import { ConnectPanel } from "@/components/settings/models";
import { sourceLinkedAria } from "@/lib/canvas-pm-adapter/extensions/citation-plugin";
import { formatNumValue } from "@/lib/canvas-pm-adapter/format-num";
import { createRefLinePlugin } from "@/lib/chart/ref-line-plugin";
import type { Locale } from "@/lib/locale";
import { staticTranslator } from "@/lib/locale-messages";
import { prettifyStepLabel } from "@/lib/prettify-step-label";
import { copyPermissionedReportLink } from "@/lib/report-share";
import { reportDisplayTitle } from "@/lib/report-title";
import { useSpace } from "@/lib/use-space";

const HAN = /\p{Script=Han}/u;
const LOCALES: Locale[] = ["zh-CN", "en-US", "de-DE", "fr-FR"];
const CATALOGS = { "zh-CN": zhMessages, "en-US": enMessages, "de-DE": deMessages, "fr-FR": frMessages } as const;

afterEach(() => {
  cleanup();
  document.documentElement.lang = "zh-CN";
});

function setLocale(locale: Locale) {
  document.documentElement.lang = locale;
}

function expectNoHanOutsideZh(locale: Locale, values: string[]) {
  if (locale === "zh-CN") return;
  for (const v of values) expect(v, `${locale}: ${v}`).not.toMatch(HAN);
}

describe("纯函数：按当前界面语言取词", () => {
  const cases: Record<Locale, {
    types: string[];
    aria: string;
    cny: string[];
    step: string[];
    titles: string[];
    mean: string;
  }> = {
    "zh-CN": {
      types: ["文档", "表格", "演示", "图片", "文件"],
      aria: "数据 x，关联来源 3",
      cny: ["1.23亿元", "5.43万元", "999元"],
      step: ["1 项", "2 项", "查询返回 1 行", "查询返回 3 行", "运行完成", "运行出错", "搜索: abc"],
      titles: ["2026-09-01 08:05 报告", "报告 cv_abcde", "未命名报告"],
      mean: "均值 20",
    },
    "en-US": {
      types: ["Document", "Spreadsheet", "Slides", "Image", "File"],
      aria: "Data x, linked to source 3",
      cny: ["CNY 123.46M", "CNY 54.32K", "CNY 999"],
      step: ["1 item", "2 items", "1 row returned", "3 rows returned", "Run complete", "Run failed", "Search: abc"],
      titles: ["2026-09-01 08:05 Report", "Report cv_abcde", "Untitled report"],
      mean: "Mean 20",
    },
    "de-DE": {
      types: ["Dokument", "Tabellenblatt", "Folien", "Bild", "Datei"],
      aria: "Daten x, verknüpft mit Quelle 3",
      cny: ["123,46 Mio. CNY", "54.321 CNY", "999 CNY"],
      step: ["1 Eintrag", "2 Einträge", "1 Zeile zurückgegeben", "3 Zeilen zurückgegeben", "Ausführung abgeschlossen", "Ausführung fehlgeschlagen", "Suche: abc"],
      titles: ["Bericht 2026-09-01 08:05", "Bericht cv_abcde", "Unbenannter Bericht"],
      mean: "Mittelwert 20",
    },
    "fr-FR": {
      types: ["Document", "Feuille de calcul", "Diapositives", "Image", "Fichier"],
      aria: "Donnée x, liée à la source 3",
      cny: ["123,46 M CNY", "54,32 k CNY", "999 CNY"],
      step: ["1 élément", "2 éléments", "1 ligne renvoyée", "3 lignes renvoyées", "Exécution terminée", "L’exécution a échoué", "Recherche : abc"],
      titles: ["Rapport 2026-09-01 08:05", "Rapport cv_abcde", "Rapport sans titre"],
      mean: "Moyenne 20",
    },
  };

  function refLineLabel(): string {
    const labels: string[] = [];
    const ctx = {
      save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillRect() {},
      fillText: (t: string) => labels.push(t), setLineDash() {}, measureText: () => ({ width: 40 }),
      strokeStyle: "", fillStyle: "", lineWidth: 0, font: "", textAlign: "", textBaseline: "",
    };
    const chart = {
      data: { datasets: [{ data: [10, 20, 30] }] },
      isDatasetVisible: () => true,
      ctx,
      chartArea: { left: 0, right: 100, top: 0, bottom: 100 },
      scales: { y: { axis: "y", getPixelForValue: (v: number) => 100 - v } },
    };
    createRefLinePlugin([{ kind: "mean" }], { theme: {} as never, format: String }).afterDatasetsDraw!(
      chart as never, {} as never, {} as never, {} as never,
    );
    return labels[0];
  }

  it.each(LOCALES)("%s", (locale) => {
    setLocale(locale);
    const want = cases[locale];
    const got = {
      types: ["a.pdf", "b.xlsx", "c.pptx", "d.png", "e.zip"].map((n) => typeMeta(n).label),
      aria: sourceLinkedAria("x", 3),
      cny: [123456789.5, 54321, 999].map((v) => formatNumValue(String(v), "CNY")),
      step: [
        prettifyStepLabel([1]),
        prettifyStepLabel([1, 2]),
        prettifyStepLabel({ results: [1] }),
        prettifyStepLabel({ results: [1, 2, 3] }),
        prettifyStepLabel({ ok: true }),
        prettifyStepLabel({ exit_code: 2 }),
        prettifyStepLabel({ query: "abc" }),
      ],
      titles: [
        { created_at: "2026-09-01T08:05:00", id: "cv_abcdef123" },
        { created_at: null, id: "cv_abcdef123" },
        { created_at: null, id: "" },
      ].map((r) => reportDisplayTitle({ title: null, first_section_title: null, ...r } as never)),
      mean: refLineLabel(),
    };
    expect(got).toEqual(want);
    expectNoHanOutsideZh(locale, [...got.types, got.aria, ...got.cny, ...got.step, ...got.titles, got.mean]);
  });

  it.each([
    ["zh-CN", "[服务公开地址]/api/openapi/v1"],
    ["en-US", "[public-service-address]/api/openapi/v1"],
    ["de-DE", "[öffentliche-dienstadresse]/api/openapi/v1"],
    ["fr-FR", "[adresse-publique-du-service]/api/openapi/v1"],
  ] as const)("%s：OpenAPI 地址示例按普通文本显示", (locale, address) => {
    const t = staticTranslator("team_openapi", locale);
    expect(t("base_path_description")).toBe(address);
    expect(t("base_path_hint")).toContain(`${address}/users`);
  });

  it("数字插值原样输出，不加千分位", () => {
    setLocale("en-US");
    expect(prettifyStepLabel({ results: Array.from({ length: 1200 }, (_, i) => i) })).toBe("1200 rows returned");
    setLocale("zh-CN");
    expect(prettifyStepLabel({ results: Array.from({ length: 1200 }, (_, i) => i) })).toBe("查询返回 1200 行");
  });

  it.each(LOCALES)("%s：复制权限保护链接的提示与 toast", async (locale) => {
    setLocale(locale);
    copied.length = 0;
    toasts.length = 0;
    await copyPermissionedReportLink("s_1");
    const want = {
      "zh-CN": ["复制权限保护链接（仅有访问权限的人可打开）", "已复制权限保护链接：只有有权访问该报告的人可以打开"],
      "en-US": [
        "Copy access-protected link (only people with access can open)",
        "Access-protected link copied: only people with access to this report can open it",
      ],
      "de-DE": [
        "Zugriffsgeschützten Link kopieren (nur berechtigte Personen können ihn öffnen)",
        "Zugriffsgeschützter Link kopiert: Nur Personen mit Zugriff auf diesen Bericht können ihn öffnen",
      ],
      "fr-FR": [
        "Copier le lien protégé par les accès (seules les personnes autorisées peuvent l’ouvrir)",
        "Lien protégé par les accès copié : seules les personnes ayant accès à ce rapport peuvent l’ouvrir",
      ],
    }[locale];
    expect([...copied, ...toasts]).toEqual(want);
  });

  it.each(LOCALES)("%s：不在 React 树里的几条短句（引用 aria、节点兜底、万/亿量级字）", (locale) => {
    const aria = staticTranslator("citation_aria", locale);
    const node = staticTranslator("canvas_node_fallback", locale);
    const got = [
      aria("numbered", { n: 2 }),
      aria("cite_labeled", { n: 2, label: "L" }),
      node("missing_section_id", { kind: "chart" }),
    ];
    const want = {
      "zh-CN": ["引用 2", "引用 2：L", "section_chart 缺 sectionId"],
      "en-US": ["Citation 2", "Citation 2: L", "section_chart is missing sectionId"],
      "de-DE": ["Quellenverweis 2", "Quellenverweis 2: L", "Für section_chart fehlt sectionId"],
      "fr-FR": ["Citation 2", "Citation 2 : L", "section_chart ne possède pas de sectionId"],
    }[locale];
    expect(got).toEqual(want);
    expect(staticTranslator("cny_format", "zh-CN")("hundred_million", { n: "1.23" })).toBe("¥1.23亿");
    expect(staticTranslator("cny_format", "zh-CN")("ten_thousand", { n: "5.4" })).toBe("¥5.4万");
  });
});

describe("组件：按界面语言渲染", () => {
  it.each(LOCALES)("%s：无法识别的段", (locale) => {
    const { container } = renderIntl(
      <UnknownSection data={{ id: "s", kind: "unknown", original_kind: "foo" } as never} />,
      { locale },
    );
    const head = container.querySelector(".v2-unknown-head")!;
    expect(head.querySelector("code")?.textContent).toBe("foo");
    expect(head.textContent).toBe(
      {
        "zh-CN": "此段无法显示 · 类型 foo",
        "en-US": "This section can't be displayed · type foo",
        "de-DE": "Dieser Abschnitt kann nicht angezeigt werden · Typ foo",
        "fr-FR": "Cette section ne peut pas être affichée · type foo",
      }[locale],
    );
  });

  it.each(LOCALES)("%s：模型连接编辑态的密钥占位", (locale) => {
    render(
      <NextIntlClientProvider locale={locale} messages={CATALOGS[locale]}>
        <ConnectPanel
          t={((k: string) => k) as never}
          draft={{
            mode: "edit", protocol: "openai_compatible", name: "", baseUrl: "", apiKey: "", messagesPath: "",
            apiVersion: "", authType: "bearer", authHeaderName: "", authPrefix: "", customChatUrl: "",
            customEmbedUrl: "", customChatContract: "openai_chat_completions", customChatQuery: "",
            customChatHeaders: "", customChatBody: "", customEmbedQuery: "", customEmbedHeaders: "",
            customEmbedBody: "", models: [{ id: "m1", caps: ["chat"], source: "manual" }], testing: false,
          } as never}
          caps={["chat"]}
          busy={false} saveEnabled saveBlocked={null} testEnabled
          capLabel={(c: string) => c}
          onPatch={vi.fn()} onToggleCap={vi.fn()} onTest={vi.fn()} onAutoFill={vi.fn()} discoveringModels={false}
          onSave={vi.fn()} onCancel={vi.fn()} v2 azureEnabled customHttpEnabled
        />
      </NextIntlClientProvider>,
    );
    const placeholder = (document.getElementById("mset-provider-api-key") as HTMLInputElement).placeholder;
    expect(placeholder).toBe(
      {
        "zh-CN": "••••••（留空保留原密钥）",
        "en-US": "•••••• (leave blank to keep the current key)",
        "de-DE": "•••••• (leer lassen, um den aktuellen Schlüssel beizubehalten)",
        "fr-FR": "•••••• (laissez vide pour conserver la clé actuelle)",
      }[locale],
    );
  });

  it.each(LOCALES)("%s：空间名加载中", (locale) => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <NextIntlClientProvider locale={locale} messages={CATALOGS[locale]}>{children}</NextIntlClientProvider>
    );
    const { result } = renderHook(() => useSpace(), { wrapper });
    expect(result.current.activeName).toBe(
      { "zh-CN": "（加载中）", "en-US": "(Loading…)", "de-DE": "(Wird geladen…)", "fr-FR": "(Chargement…)" }[locale],
    );
  });

});
