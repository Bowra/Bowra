import { PRODUCT_NAME } from "@/lib/product-identity";
import type { ErrorDiagnosticsLabels } from "@/components/error-notice";
import type { ClientBoundaryFailureMessages } from "@/lib/error-presentation";
import { buildLocale } from "@/lib/locale";

export type BoundaryErrorLocale = "zh-CN" | "en-US";

export interface BoundaryErrorCopy {
  recoveryLabel: string;
  routeTitle: string;
  globalTitle: string;
  chunkTitle: string;
  retry: string;
  reload: string;
  home: string;
  failureMessages: ClientBoundaryFailureMessages;
  /** Same keys as messages errors.diagnostics (including the short Copy Diagnostics copy for Toast). */
  diagnostics: ErrorDiagnosticsLabels & { copyShort: string; copied: string; copyShortFailed: string };
}

const COPY: Record<BoundaryErrorLocale, BoundaryErrorCopy> = {
  "en-US": {
    recoveryLabel: "Recovery mode",
    routeTitle: "This page could not be displayed",
    globalTitle: `${PRODUCT_NAME} hit an unexpected error`,
    chunkTitle: "Some page resources could not be loaded",
    retry: "Try again",
    reload: "Reload page",
    home: "Go home",
    failureMessages: {
      routeRenderFailed:
        "This page could not be displayed. Try again or reload the page. If the problem continues, contact your administrator.",
      globalRenderFailed:
        `${PRODUCT_NAME} hit an unexpected error. Reload the page. If the problem continues, contact your administrator.`,
      chunkLoadFailed:
        "Some page resources could not be loaded. Reload the page.",
      retry: "Try again",
      reload: "Reload page",
    },
    diagnostics: {
      surfaceNames: { global: "Application", route: "Current page" },
      summary: "View technical details",
      surface: "Surface",
      version: "Interface version",
      observedAt: "Observed at",
      httpStatus: "HTTP status",
      errorCode: "Error code",
      diagnosticCode: "Diagnostic code",
      requestId: "Server request ID",
      serverRequestIdUnavailable: "Unavailable (no HTTP response)",
      occurrenceId: "Error reference",
      digest: "Next.js digest",
      traceId: "Trace ID",
      turnId: "Turn ID",
      operationId: "Operation ID",
      safeDetail: "Published detail",
      retryable: "Can retry",
      retryableYes: "Yes",
      retryableNo: "No",
      copy: "Copy diagnostic information",
      copySuccess: "Diagnostic information copied",
      copyFailed: "Could not copy automatically",
      copyFallbackPrompt: "Copy this diagnostic information:",
      copyShort: "Copy diagnostics",
      copied: "Copied",
      copyShortFailed: "Couldn't copy",
    },
  },
  "zh-CN": {
    recoveryLabel: "恢复模式",
    routeTitle: "这个页面暂时无法显示",
    globalTitle: `${PRODUCT_NAME} 遇到了意外错误`,
    chunkTitle: "页面资源未能加载",
    retry: "重试",
    reload: "刷新页面",
    home: "回到首页",
    failureMessages: {
      routeRenderFailed:
        "这个页面暂时无法正常显示。请重试或刷新页面；如问题持续，请联系管理员。",
      globalRenderFailed:
        `${PRODUCT_NAME} 遇到意外错误。请重新加载；如问题持续，请联系管理员。`,
      chunkLoadFailed:
        "页面所需资源未能加载，请刷新页面。",
      retry: "重试",
      reload: "刷新页面",
    },
    diagnostics: {
      surfaceNames: { global: "应用", route: "当前页面" },
      summary: "查看技术详情",
      surface: "发生位置",
      version: "界面版本",
      observedAt: "浏览器观测时间",
      httpStatus: "HTTP 状态",
      errorCode: "错误代码",
      diagnosticCode: "诊断代码",
      requestId: "服务端请求 ID",
      serverRequestIdUnavailable: "不可用（未收到 HTTP 响应）",
      occurrenceId: "错误编号",
      digest: "Next.js 摘要",
      traceId: "追踪 ID",
      turnId: "对话轮次 ID",
      operationId: "操作 ID",
      safeDetail: "已发布详情",
      retryable: "是否可重试",
      retryableYes: "是",
      retryableNo: "否",
      copy: "复制诊断信息",
      copySuccess: "诊断信息已复制",
      copyFailed: "无法自动复制",
      copyFallbackPrompt: "请复制以下诊断信息：",
      copyShort: "复制诊断",
      copied: "已复制",
      copyShortFailed: "复制失败",
    },
  },
};

export function boundaryBuildLocale(): BoundaryErrorLocale {
  // The provider-free dictionary has only English and Chinese: every other build
  // language (German and any language added later) uses English, never Chinese.
  return buildLocale() === "zh-CN" ? "zh-CN" : "en-US";
}

export function boundaryErrorCopy(
  locale: BoundaryErrorLocale,
): BoundaryErrorCopy {
  return COPY[locale];
}
