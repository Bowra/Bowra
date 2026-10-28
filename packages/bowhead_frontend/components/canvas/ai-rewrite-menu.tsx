/**
 * AI rewrite preset contract.
 *
 * WP3 (2026-06-13): the standalone Notion-style AIRewriteMenu was merged into the selection toolbar
 * (FormattingToolbar's ✦ rewrite/shorten/formal actions) and block-level BlockAIBar (free-form instructions). Retain only
 * the preset types shared by useAIRewrite / FormattingToolbar / AIRewriteDiffBar here.
 *
 * For preset → backend instruction mapping, see PRESET_INSTRUCTIONS in lib/use-ai-rewrite.ts.
 */

export type RewritePreset =
  | "concise"
  | "professional"
  | "custom"
  | "grammar"
  | "different_angle";
