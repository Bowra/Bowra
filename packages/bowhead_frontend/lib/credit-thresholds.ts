/**
 * Single frontend source for low-credit-balance criteria.
 *
 * Needed because criteria once diverged: `user-badge.tsx` used absolute `balance < 1000`, while design
 * (`docs/credit-usage-activation-model.md` §4/§7.1) and backend aggregation (`user_store.py::credits_summary`
 * with `balance_pct < 0.20`) use **relative ratios**. Equivalent for pro (quota 5000), masking the issue;
 * for ultra (quota 30000), absolute 1000 is only 3.3%, so backend already considered the balance low while the badge
 * kept its normal color—a heavy user on 116 with 70% of quota used fell into this gap.
 *
 * Any new balance surface (composer warning, profile, settings…) must use this criterion, never another local number.
 */

/** Low-balance threshold: remaining balance / period quota. Matches backend `credits_summary` / `_CREDIT_ATTENTION_PROTECTED_MIN_BALANCE_PCT`. */
export const LOW_BALANCE_PCT = 0.2;

/** Critical threshold: matches backend `credit_activation.py` critical_balance_active_user. */
export const CRITICAL_BALANCE_PCT = 0.05;

/**
 * Remaining ratio. `quota <= 0` returns null—unseeded/anomalous data, not 0%;
 * callers explicitly decide presentation, avoiding divide-by-zero Infinity/NaN in UI.
 */
export function balancePct(balance: number, quota: number): number | null {
  if (!Number.isFinite(balance) || !Number.isFinite(quota) || quota <= 0) return null;
  return balance / quota;
}

/** Whether to warn of low balance. `quota <= 0` falls back to whether balance is exhausted. */
export function isLowBalance(balance: number, quota: number): boolean {
  const pct = balancePct(balance, quota);
  return pct === null ? balance <= 0 : pct < LOW_BALANCE_PCT;
}

/** Whether balance is critical. Also falls back to exhausted balance when `quota <= 0`. */
export function isCriticalBalance(balance: number, quota: number): boolean {
  const pct = balancePct(balance, quota);
  return pct === null ? balance <= 0 : pct < CRITICAL_BALANCE_PCT;
}
