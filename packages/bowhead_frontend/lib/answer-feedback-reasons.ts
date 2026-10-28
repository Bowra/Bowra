/**
 * The reasons a thumbs-down can carry (2026-10-19 decision), in display order. Mirrors
 * `agent_eval_conversation_cases.ANSWER_FEEDBACK_REASONS`; the server rejects any other code.
 */
export const ANSWER_FEEDBACK_REASONS = ["fact_wrong", "not_answered", "missing_content", "too_brief", "too_verbose", "presentation", "other"] as const;
export type AnswerFeedbackReason = (typeof ANSWER_FEEDBACK_REASONS)[number];
