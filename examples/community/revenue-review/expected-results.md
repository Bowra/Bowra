# Expected results and independent checks

These are calculations from the synthetic CSV, not recorded Agent answers. Run [calculate.py](calculate.py) with Python 3.12 to reproduce the values without a model or third-party packages. Decimal outputs are JSON strings to retain precision. The calculator is intentionally scoped to this fixed two-quarter example.

## Quarter totals

| Metric                  |  2026-Q1 |  2026-Q2 |
| ----------------------- | -------: | -------: |
| Orders                  |    1,100 |    1,200 |
| Gross revenue           | $132,000 | $144,000 |
| Refunds                 |   $5,000 |   $6,000 |
| Net revenue             | $127,000 | $138,000 |
| Variable cost           |  $76,900 |  $84,300 |
| Contribution            |  $50,100 |  $53,700 |
| Contribution margin     |   39.45% |   38.91% |
| Refund rate, by dollars |    3.79% |    4.17% |
| Net revenue per order   |  $115.45 |  $115.00 |

Hand-check the core results:

- Q1 net revenue: `132,000 − 5,000 = 127,000`.
- Q2 net revenue: `144,000 − 6,000 = 138,000`.
- Net revenue growth: `(138,000 − 127,000) ÷ 127,000 = 8.66%`.
- Q2 contribution: `138,000 − 84,300 = 53,700`; up `$3,600` from Q1.
- Margin change: `(53,700 ÷ 138,000 − 50,100 ÷ 127,000) × 100 = −0.54 percentage points` after rounding. Do not subtract rounded percentages before calculating.

## Reconcile the drivers

| Region / channel | Q1 net revenue | Q2 net revenue |   Change |
| ---------------- | -------------: | -------------: | -------: |
| North / Direct   |        $46,000 |        $52,200 |  +$6,200 |
| North / Partner  |        $35,000 |        $42,000 |  +$7,000 |
| South / Direct   |        $29,000 |        $24,000 |  −$5,000 |
| South / Partner  |        $17,000 |        $19,800 |  +$2,800 |
| Total            |       $127,000 |       $138,000 | +$11,000 |

`6,200 + 7,000 − 5,000 + 2,800 = 11,000`. North / Partner provides the largest positive change. South / Direct is the only declining segment: net revenue falls **17.24%**, orders fall from **250 to 220**, and its refund-dollar ratio rises from **3.33% to 9.09%**.

## Interpret carefully

A defensible headline is: **Revenue and contribution grew, but margin softened and South / Direct needs investigation.**

Ask for refund reasons and sales/order detail in South / Direct. The aggregate CSV does not establish that refunds caused the order decline, that a campaign failed, or that customers churned. There are no targets, prior-year data or fixed costs. A chart should reproduce the four segment values above, without an invented monthly trend.

The [reference files](references/README.md) are one compact presentation of these checked figures. Wording and layout need not match them; the metric definitions, arithmetic and evidence boundaries should.
