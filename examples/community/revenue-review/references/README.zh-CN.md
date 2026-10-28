# 独立生成的参考产物

[English](README.md) | 简体中文 | [收入示例](../README.zh-CN.md)

这些文件确实由仓库独立的 `bowra-*` CLI 使用下列合成输入生成，没有经过模型或应用会话。它们展示这些工具支持的基础格式，不代表完整产品工作流、模板保真能力或 CI 运行结果。

| 文件                                     | 内容                                               | 输入                                       |
| ---------------------------------------- | -------------------------------------------------- | ------------------------------------------ |
| [report.pdf](report.pdf)                 | 一页收入备忘录，含两张对比表                       | [report-input.json](report-input.json)     |
| [document.docx](document.docx)           | 同一备忘录的可编辑 Word 版本                       | [report-input.json](report-input.json)     |
| [workbook.xlsx](workbook.xlsx)           | 四张纯数值工作表：源数据、季度汇总、增长贡献及说明 | [workbook-input.json](workbook-input.json) |
| [deck.pptx](deck.pptx)                   | 三页基础可编辑表格幻灯片                           | [deck-input.json](deck-input.json)         |
| [report-preview.png](report-preview.png) | PDF 唯一页面的裁剪渲染图                           | 使用 Poppler 渲染 `report.pdf`             |

![独立生成的报告首页裁剪预览](report-preview.png)

## 数字的依据

[revenue.csv](../revenue.csv) 是合成源数据。[指标定义](../metric-definitions.md)说明分母和缺失的上下文。[calculate.py](../calculate.py)使用标准库十进制计算；汇总数字还经过独立的 SQL 分组求和核验。[预期结果](../expected-results.md)提供可手工检查的计算过程。

工作簿包含**数值，不包含公式**。`bowra-xlsx` 不创建或计算公式；该文件是已核验的快照，不是修改后自动重算的财务模型。变更源数据后，应重新计算并更新 JSON。演示文稿采用新的基础布局，不保留上传模板。

## 从仓库根目录重新生成

使用仓库已安装的 Python 3.12 环境及锁定依赖。[英文版命令](README.md#regenerate-from-the-repository-root)可直接使用，不访问网络，也不需要模型密钥。每个 CLI 要求新的输出工作目录，在 `result.json` 中返回实际文件名、大小和 SHA-256。

可选的预览命令需要本机另外安装 Poppler 的 `pdftoppm`。裁剪只移除页面边距，保留标题、合成数据标识、两张表格、结论和来源说明。内容变化后应重新检查裁剪边界。文档元数据可能随运行变化，因此语义可复现不等于二进制摘要完全相同。

生成检查重新打开了四种文件，核对内容、工作表／幻灯片数量和关键数字，并人工查看 PDF 预览。这些文件是普通本地文档；直接运行 CLI 不会注册 Artifact、授予用户权限、发布报告或验证应用的沙箱集成。
