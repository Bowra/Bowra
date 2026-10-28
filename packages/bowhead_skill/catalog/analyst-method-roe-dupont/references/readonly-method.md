# DuPont profitability analysis

Use this self-contained method to explain the drivers of return on equity from supplied or already authorized financial data. It does not require the package's scripts, another Skill, a new tool, or a new connection. Use available authorized calculation tools when useful; this method grants no tool or script permissions. Large or unverifiable calculations need a suitable authorized calculation tool before a numerical conclusion can be reported.

## Establish the inputs

Identify the company, period, reporting currency/unit, and accounting scope. Optional `company` and `period` parameters describe the intended analysis; resolve conflicts with the user's task before calculating. Keep consolidated and parent-only figures separate. Do not mix periods, currencies, or units.

The three-factor decomposition requires net income, revenue, average total assets, and average shareholders' equity for the same period. Prefer average balance-sheet values for period income; if only closing balances are supplied, label the resulting ratios as closing-balance approximations. Do not invent opening balances, annualize a partial period without agreement, or silently replace shareholders' equity with registered capital.

When a required value or definition is missing, identify exactly what is missing. Ask through the existing clarification mechanism and wait for the answer. Do not fill gaps from examples, assume equity equals assets, or report a completed ROE decomposition without its denominator. Preserve the known inputs so an answer supplies the missing value instead of restarting the analysis. If the user declines, return only the computable ratios and the unresolved gap.

## Calculate and reconcile

| Measure           | Formula                               | Meaning                                 |
| ----------------- | ------------------------------------- | --------------------------------------- |
| Net profit margin | Net income / Revenue                  | Profit retained per unit of revenue     |
| Asset turnover    | Revenue / Average total assets        | Revenue generated per unit of assets    |
| Equity multiplier | Average total assets / Average equity | Balance-sheet leverage                  |
| Return on assets  | Net income / Average total assets     | Income relative to assets               |
| Return on equity  | Net income / Average equity           | Income relative to shareholders' equity |

Show each requested formula, substitute the supplied values, and give its result. Express profit margin, ROA, and ROE as percentages; express turnover and the equity multiplier as multiples. Keep unrounded values through the calculation and round only for display.

Check both identities with the same inputs:

- ROE = net profit margin × asset turnover × equity multiplier.
- ROE = ROA × equity multiplier.

A zero denominator makes the associated ratio undefined; report that condition without division. Negative equity can produce a signed arithmetic ratio but makes ordinary profitability/leverage comparisons misleading; explain this before interpreting ROE. Losses and nonpositive intermediate profits also limit the economic interpretation of the five-factor ratios. Never describe an undefined ratio as zero or successful value creation.

## Extend only when the task and data support it

For five-factor DuPont analysis, use EBIT and earnings before tax (EBT) from the same accounting scope:

ROE = (Net income / EBT) × (EBT / EBIT) × (EBIT / Revenue) × (Revenue / Average total assets) × (Average total assets / Average equity).

These factors describe tax burden, interest/non-operating burden, operating margin, asset turnover, and leverage. EBT may include non-operating items besides interest. Derive EBT as EBIT minus interest expense only when that simplifying assumption is supported and explicitly stated. A low inferred effective tax rate is a signal to investigate; it does not prove a tax holiday, deferred-tax benefit, or misconduct.

For value creation, require a supported operating tax rate, a consistent invested-capital definition, and a comparable WACC:

- NOPAT = EBIT × (1 − operating tax rate).
- ROIC = NOPAT / invested capital.
- EVA = NOPAT − invested capital × WACC.

State any averaging convention. ROIC above WACC indicates a positive spread under those definitions; below WACC indicates a negative spread. High ROE alone proves neither. If these inputs are missing, mark value creation as unassessed rather than requesting unrelated data for a task that asked only for three-factor DuPont.

Only add sensitivity scenarios requested by the user or justified by the task. State the changed input and what remains fixed. Raising WACC by 100 basis points changes EVA by −invested capital × 0.01 under fixed NOPAT/capital; it does not directly change accounting ROE. An interest-rate shock needs debt and financing assumptions before estimating net income or ROE.

## Deliver a useful answer

Present the requested formulas and results, then explain the principal driver: margin, asset efficiency, or leverage. For period comparisons, use consistent definitions and distinguish observed changes from causal explanations. Do not invent industry thresholds or claim a precise contribution breakdown without a stated attribution method.

Identify the source of every input, distinguish user-provided figures from retrieved evidence, and name unresolved definitions or limitations. Do not fabricate citations, claim a script ran, or claim access to a data source that was not used. Follow the original task's requested language and output format.

## Method references

These sources explain the analytical definitions; they are not sources for the user's company figures and need not be fetched to apply this entry.

- [CFA Institute: Financial Analysis Techniques](https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/financial-analysis-techniques) describes DuPont components and the limitations of isolated ratios.
- [Aswath Damodaran, NYU Stern: Economic Value Added](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/invfables/eva.htm) defines economic profit from after-tax operating income and the cost of invested capital.
