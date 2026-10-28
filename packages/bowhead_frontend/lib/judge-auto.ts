/**
 * The evaluation (judge) model that "Auto" resolves to, mirroring the backend `_pick_heterogeneous` in
 * `bowhead_router.agent_tiers`: a chat model other than the main model, preferring another provider, otherwise
 * another model at the main model's provider. Null when the main model is the team's only chat model; evaluation
 * then falls back to the main model and the page must say so.
 */
export function autoJudgeModel(
  chatModels: readonly { providerKey: string; id: string }[],
  primary: { provider: string; model: string } | null,
): { provider: string; model: string } | null {
  if (!primary) return null;
  const pick = chatModels.find((model) => model.providerKey !== primary.provider)
    ?? chatModels.find((model) => model.providerKey === primary.provider && model.id !== primary.model);
  return pick ? { provider: pick.providerKey, model: pick.id } : null;
}
