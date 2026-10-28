# Synthetic revenue data: metric definitions

This eight-row dataset is invented for the Bowra example. It contains no real transactions or customer information. Each row is one **quarter × region × channel** aggregate; that key is unique. Both quarters are complete. All amounts are USD, with no tax or currency conversion.

| Field               | Meaning                                                                            |
| ------------------- | ---------------------------------------------------------------------------------- |
| `period`            | Complete calendar quarter: 2026-Q1 or 2026-Q2                                      |
| `region`            | Synthetic North or South sales region                                              |
| `channel`           | Direct or Partner sales channel                                                    |
| `orders`            | Orders placed in the quarter, including orders with refunds; not unique customers  |
| `gross_revenue_usd` | Booked revenue before the refunds shown here                                       |
| `refunds_usd`       | Refund dollars assigned to the same period as the originating sale                 |
| `variable_cost_usd` | Variable costs for that quarter's sales, already net of cost recoveries on refunds |

## Calculations

- **Net revenue** = gross revenue − refunds.
- **Contribution** = net revenue − variable cost. This is not operating profit: fixed costs, financing and taxes are absent.
- **Contribution margin** = total contribution ÷ total net revenue. Aggregate numerators and denominators before division; do not average row margins.
- **Refund rate** = total refund dollars ÷ total gross revenue. This is a dollar ratio, not the fraction of orders returned.
- **Net revenue per order** = total net revenue ÷ total orders. It is not revenue per customer.
- **Quarter-over-quarter growth** = (Q2 − Q1) ÷ Q1. Margin change is reported in percentage points.
- **Segment growth contribution** = Q2 segment net revenue − Q1 segment net revenue. The four differences must add to the total change.

If a denominator is zero, report the ratio as undefined and explain it. Preserve full precision during calculation; round only displayed currency to two decimals and displayed percentages to two decimals.

## Boundaries

No targets, monthly dates, customer IDs, campaign spend, refunds by reason or fixed costs are provided. The data cannot prove seasonality, customer retention, campaign effectiveness or a causal reason for refunds. Treat follow-up explanations as hypotheses to investigate.
