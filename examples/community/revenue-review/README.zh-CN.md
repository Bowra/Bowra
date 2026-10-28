# 收入复盘：增长背后的利润率提醒

[English](README.md) | 简体中文 | [全部示例](../README.zh-CN.md)

八行合成数据足以检查报告标题、数据表和建议是否一致。

## 开始任务

1. 附加 [revenue.csv](revenue.csv) 和[指标定义](metric-definitions.md)，必要时也可粘贴内容。
2. 在已配置的 Bowra 聊天中粘贴 [task.zh-CN.md](task.zh-CN.md)。
3. 对照[预期结果](expected-results.md)检查回答。
4. 如果所需 `bowra-*` Skill 可用，可要求生成新的基础文档。分享前应打开实际下载文件，核对数字。

此任务不假定已经配置模型、计算运行环境或文档 Skill。资源不可用时，Agent 应说明限制。标准报告下载采用独立的 Canvas 导出流程，见[报告帮助](../../../docs/help/revise-report.zh-CN.md)。

## 查看并复算参考结果

净收入从 **127,000 美元增长至 138,000 美元**，但贡献率从 **39.45% 降至 38.91%**。South / Direct 是需要进一步调查的分组，现有数据不能证明原因。

![独立生成的合成收入 PDF 首页裁剪预览](references/report-preview.png)

[下载 DOCX、PDF、XLSX 和 PPTX 参考文件](references/README.zh-CN.md)。它们由独立文档 CLI 使用核对后的 JSON 直接生成，不是应用或 LLM 对话记录、客户案例或完整部署验证。

使用 Python 3.12 即可在本地复算，无需额外依赖：

```bash
python3 examples/community/revenue-review/calculate.py
```

计算器读取同目录 CSV，输出汇总指标和分组变化。参考工作簿是**数值快照**，不包含公式模型；更改输入后应重新读取 CSV 计算。
