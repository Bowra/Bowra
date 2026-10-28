/**
 * Synthetic citation-source payloads shaped like real "Run Code" sandbox stdout
 * (printed pandas DataFrames, key/value summary lines, a `--- csv ---` dump).
 * Numbers are invented; only the shape mirrors production.
 */

/** `print(df.round(4).to_string(index=False))` followed by summary lines. */
export const RUN_CODE_STDOUT_ALIGNED = [
  " 月份    利润率    退款率  利润率_环比pp  退款率_环比pct",
  "  1 0.1801 0.1512       NaN        NaN",
  "  2 0.1777 0.1820   -0.2400    20.3704",
  "  3 0.1765 0.1633   -0.1200   -10.2747",
  "  4 0.1790 0.1771    0.2500     8.4507",
  "",
  "利润率 1-8月: 首=0.1801 末=0.1702 差=-0.99pp 区间[0.1702, 0.1810] 均值=0.1766",
  "退款率 1-8月: 首=0.1512 末=0.1401 差=-1.11pp 区间[0.1401, 0.1820] 均值=0.1622",
  "",
  "利润率 最大环比升: [4.0, 0.25]",
  "利润率 最大环比降: [8.0, -0.61]",
  "两序列相关系数: 0.412",
].join("\n");

/** `print(df)` with the default RangeIndex, then a CSV dump behind a marker line. */
export const RUN_CODE_STDOUT_WITH_CSV = [
  "         月份     营业额_亿    客流量_万",
  "0  2026年1月  18.250000  220.5000",
  "1  2026年2月  16.400000  205.1000",
  "2  2026年3月  13.900000  190.7500",
  "",
  "营业额 1月 18.25亿 3月 13.90亿 累计 -23.84%",
  "客流量 1月 220.50万 3月 190.75万 累计 -13.49%",
  "",
  "--- csv ---",
  "月份,营业额_亿,客流量_万",
  "2026年1月,18.25,220.5",
  "2026年2月,16.4,205.1",
  "2026年3月,13.9,190.75",
].join("\n");
