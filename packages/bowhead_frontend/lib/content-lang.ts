/**
 * Detects CJK model output so its container can carry a `lang` attribute (P2 batch 6, user decision
 * 2026-10-01). The content line-length cap is keyed off `:lang()`: CJK text keeps 40 characters per line,
 * Latin text about 75. A Chinese answer inside an English UI is still recognisable from its script, so it
 * gets the CJK measure; Latin text cannot be told apart reliably (en / de / fr), so it returns null and the
 * container follows the document (UI) language.
 *
 * Only the first `SAMPLE_LENGTH` characters are inspected, which keeps streaming re-renders cheap.
 */

const SAMPLE_LENGTH = 2000;

export type CjkContentLang = "zh" | "ja" | "ko";

export function detectCjkContentLang(text: string | null | undefined): CjkContentLang | null {
  if (!text) return null;
  const sample = text.slice(0, SAMPLE_LENGTH);
  const han = sample.match(/\p{Script=Han}/gu)?.length ?? 0;
  const kana = sample.match(/[\p{Script=Hiragana}\p{Script=Katakana}]/gu)?.length ?? 0;
  const hangul = sample.match(/\p{Script=Hangul}/gu)?.length ?? 0;
  const latin = sample.match(/\p{Script=Latin}/gu)?.length ?? 0;
  const cjk = han + kana + hangul;
  // One CJK character carries roughly a word; a Latin word averages about five letters. Text counts as CJK
  // when its CJK characters outnumber its Latin words, so product names and numbers in Chinese prose do
  // not flip it, while an English answer quoting a few Chinese terms stays Latin.
  if (cjk === 0 || cjk * 5 < latin) return null;
  if (hangul > han + kana) return "ko";
  if (kana > 0 && kana * 4 >= han) return "ja";
  return "zh";
}
