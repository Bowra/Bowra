# Bowra 文档

[English](README.md) | 简体中文

**Verified Intelligence** 让业务成果可以被检查：观点能追溯到来源，数字能追溯到计算，结果能对应到所采用的业务定义。Bowra 将这些能力放进一个协作空间，用于分析、文档、报告和可复用的智能体。

[构建与设置](../deploy/README.zh-CN.md) · [面向外部 Agent](agent-guide.zh-CN.md) · [示例](../examples/community/README.zh-CN.md) · [项目概览](../README.zh-CN.md)

## 按目标开始

| 我想要……               | 从这里开始                                                                                        | 接着尝试                                                                   |
| ---------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| 首次运行 Bowra         | [从源码构建](../deploy/README.zh-CN.md)，然后[创建首位所有者](../deploy/community-setup.zh-CN.md) | [开始分析](help/getting-started.zh-CN.md)                                  |
| 跨文档提问             | [知识库与文档矩阵](help/knowledge-base.zh-CN.md)                                                  | [供应商评审示例](../examples/community/supplier-review/README.zh-CN.md)    |
| 解释业务表现           | [对话工作区、报告与来源](help/conversation-workbench.zh-CN.md)                                    | [收入分析示例](../examples/community/revenue-review/README.zh-CN.md)       |
| 修改并分享报告         | [修改已有报告](help/revise-report.zh-CN.md)                                                       | 分享前检查来源详情和下载结果                                               |
| 创建专业智能体         | [创建智能体](help/create-agent.zh-CN.md)与[配置能力](help/team-capabilities.zh-CN.md)             | 只绑定任务需要的工具和技能                                                 |
| 接入 MCP 工具服务      | [团队 MCP 工具](help/team-mcp-tools.zh-CN.md)或[个人 MCP 连接](help/personal-mcp.zh-CN.md)        | 检查连接权限和调用确认要求                                                 |
| 通过 CI 维护智能体版本 | [Developer API 指南](agent-guide.zh-CN.md#发现-agent-developer-api)                               | [智能体自动化示例](../examples/community/agent-automation/README.zh-CN.md) |
| 贡献代码或文档         | [贡献指南](../CONTRIBUTING.zh-CN.md)与[仓库 Agent 指令](../AGENTS.md)                             | [安全问题报告](../SECURITY.md)                                             |

## 沿着证据检查

![从已授权来源经过分析与证据检查，形成可分享的成果](assets/bowra/workflow.svg)

Verified Intelligence 提供三个检查角度：

- **来源：** 引用可以打开文档片段、查询结果或代码运行输出。来源缺失或无法访问时，应继续核查，不能据此认定观点正确。
- **数字：** 结果有数字溯源或执行记录时，可以回到输入和计算过程。除最终数字外，还要检查单位、筛选条件、日期、舍入和缺失数据。
- **定义：** 明确指标含义和适用的业务规则。即使数字算对了，范围有误也可能回答了错误的问题。

引用和成功执行有助于检查，但不保证所有生成结论都正确。[对话指南](help/conversation-workbench.zh-CN.md)说明来源卡片和报告操作；[知识库指南](help/knowledge-base.zh-CN.md)说明来源可用状态和文档矩阵审阅。

## 开始第一个任务前

当前仓库是 **Community 源码预览版**。请按照设置指南准备受支持的依赖并执行命令。Gateway 启动需要兼容的执行器配置；调用沙箱执行的工作流还需要共享可写工作目录。模型服务和可选连接需要运营者自行配置并提供凭据。

从你有权使用的材料开始，选择个人、空间或团队可见范围，并确认材料可用于目标任务。分享智能体或链接不会自动授予底层文档和账号的访问权限。参阅[权限说明](help/permissions.zh-CN.md)。

Bowra 原生文档技能与 Canvas 报告导出使用不同工具链。设置指南说明各自的要求；[智能体创建指南](help/create-agent.zh-CN.md)说明基础文档技能的能力与限制。

## 集成可用范围

[外部 Agent 指南](agent-guide.zh-CN.md)列出各项能力的输入、输出、前提和状态。MCP 让 Bowra 内的智能体调用已连接的工具。此源码预览尚未发布供外部 Agent 调用 Bowra 的受支持 MCP 服务端或通用任务委派 API。

限定权限的 **Agent Developer API** 在启用后支持版本提案与评估工作流。自动化需要主动启用；接受变更和发布版本仍由智能体所有者决定。集成前请检查实际部署生成的 API 参考。

## 语言与许可

文档默认入口为英文，重要指南另有中文版。界面语言优先使用明确保存的选择，其次使用受支持的浏览器／系统语言，最后回退到英文。参阅[语言设置](help/language.zh-CN.md)。

Community 不需要商业激活码或续期，也没有内置用户数量上限。基础设施、模型费用、权限和已配置的用量预算仍然适用。源码采用 [MIT 许可证](../LICENSE)，第三方组件参阅[许可声明](../THIRD_PARTY_NOTICES.md)。
