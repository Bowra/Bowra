# Bowra Community 常见问题

[English](faq.md) | 简体中文

## Bowra 是什么？

Bowra 是一个采用 MIT 许可、可自托管的 AI Agent 平台，用于业务数据和文档分析。它把专业 Agent、知识、计算和报告放在共享工作空间里。审阅者可以通过已有引用和计算证据核对授权范围内的输入。参见[项目介绍](../README.zh-CN.md)和[文档导航](README.zh-CN.md)。

## 可以用示例数据试什么？

- **比较供应商方案。** 用同一组问题检查三份合成方案，在文档矩阵里比较价格和交付条件，再打开引用查看原文。[供应商审阅示例](../examples/community/supplier-review/README.md)
- **解释收入变化。** 从合成 CSV 开始，按地区与渠道核对汇总，并对照指标定义审阅报告。[收入分析示例](../examples/community/revenue-review/README.md)
- **审阅 Agent 更新。** 启用相关流程后，从变更提案开始，依次进行所有者审阅、候选评估和发布。[Agent 自动化示例](../examples/community/agent-automation/README.md)

## 如何运行 Bowra Community？

从公开源码构建。需要 Python 3.12、uv、Node.js 24、npm，并在启动 Gateway 前配置兼容的沙箱 executor。完成首位所有者设置并连接模型后，可以开始发送任务。基础设施、凭据及各工作流所需的额外运行环境由部署者提供。参见[源码安装指南](../deploy/README.zh-CN.md)和[首位所有者设置](../deploy/community-setup.md)。

## Community 包含什么？

Community 包含共享工作空间、知识和文档工作流、报告编辑、权限控制以及可配置的 Agent 工作流。源码采用 [MIT 许可](../LICENSE)，没有内置人数上限。部署和模型使用成本及配置的用量预算仍然适用。Bowhead Business 单独分发，提供组织品牌、企业身份与组织管理 API 等企业能力。参见[版本比较](../README.zh-CN.md)。

## 可以连接哪些工具和 API？

Bowra Agent 可以使用已配置的 MCP 工具服务。受管业务数据查询使用已授权的 Datarc 连接。启用后，作用域受限的 Agent Developer API 支持版本提案与评估。当前源码提供上述接入和 Agent 版本工作流。参见[接入与能力指南](agent-guide.zh-CN.md)。

## 怎么核对分析结果？

打开可用的来源引用、数据查询结果和代码执行记录。对照问题核对时间范围、单位、筛选条件与指标定义，再判断证据是否支持结论。参见[证据核对说明](README.zh-CN.md)。

## 去哪里看当前版本和安全状态？

仓库首页链接到当前源码版本、Community 发布说明和升级指南。安全政策记录部署边界与漏洞报告方式。[当前源码版本](../README.md#current-source-snapshot) · [安全政策](../SECURITY.zh-CN.md)
