# View and allocate credits

Community has no commercial activation or user-count cap. Credits remain administrator-controlled usage budgets; the deployment operator pays model and infrastructure costs separately. A plan name here describes a usage tier, not a purchased Community subscription.

Use this guide when you see an exhausted-credit message or need to allocate credits to a member. Team owners and administrators can allocate team credits.

## How credits work

- Analysis consumes credits for messages, continued clarification, connected-channel questions, flow test runs, and scheduled tasks.
- Charges are based on the content actually processed in a turn and are applied when that turn finishes.
- New work counts input, cache-hit input, and output tokens separately for each model. Each count is multiplied by the rate captured for that model call; the weighted counts are added across the work, divided by 1,000, and rounded up once. The default multipliers are all 1. Older entries keep their original calculation. A provider that reports cached tokens separately from input may show a different charge under the new calculation even with all three multipliers at 1.
- A person needs both personal credit and credit remaining in the team pool to start new analysis.
- Monthly allowances reset with the plan. Unused credit does not carry over.

## Check and recover from exhausted credit

The top-right indicator shows your remaining credit. Below 20% of the allowance it changes colour and shows the share left, and below 5% it turns to the warning colour and says the credit is almost out. Select the indicator (or press Enter while it has focus) to open a summary: your plan, a bar showing how much of this period's allowance you have used, the date the allowance resets, and a note when credit is running low. Press Esc or select elsewhere to close it. On a phone or narrow window, the indicator is at the bottom of the navigation menu and the summary opens from the bottom of the screen. If your role can view team credits, the summary also links to your own usage details. **Personal settings → Account** shows the monthly balance and allowance. When credit is exhausted, new work cannot start. In a conversation that is already running, agents stop at their next step, report what they finished and what remains, and you can continue after credit is added; usage up to that point can still make the displayed balance slightly negative. After credit is allocated, retry the same clarification card or failed follow-up; you do not need to recreate the question.

Contact a team owner or administrator to request credit.

## Allocate credit to a member

For team owners and administrators:

1. Open **Administration Console → Credits and billing**.
2. Find the member in **Users** and choose **Allocate credits**.
3. Enter the amount, optionally record a reason, and confirm. The amount is deducted from the team pool.
4. If the team pool is insufficient, choose **Adjust team credits**, set the new allowance and reason, then return to allocation.

You can also change a member's allowance, plan, or balance directly. Confirm that the member's **Balance / allowance** is updated. A negative balance is paid down first. A team pool at zero blocks new analysis for everyone, even where an individual balance remains.

To keep a copy of a member's credit history, open their details from **Users** and select **Download credit ledger CSV**. The file contains that member's ledger; the **Download CSV** menu on the main credits page has team-wide exports.

The usage details show the recorded model for each new model charge. Expand a row to see the input, cached input, and output counts and the multipliers used. Work that used several models lists each model separately. If a call cannot be matched to a confirmed model receipt, its remaining usage is shown as unknown and uses multipliers of 1; older records may also have no model. The member ledger CSV includes model names and the recorded model usage.

Related: [Team roles and permissions](permissions/team-roles.en-US.md).
