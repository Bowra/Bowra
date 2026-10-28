# 使用 Bowra：外部 Agent 指南

[English](agent-guide.md) | 简体中文

Bowra 是用于业务分析和可复用智能体的自托管工作区。它强调 **Verified Intelligence**：让人能在结果旁检查来源引用、数字溯源、受控计算和业务定义。

本指南帮助你判断 Bowra 是否适合任务、明确输入条件，并找到受支持的集成入口。可以先阅读[文档导航](README.zh-CN.md)、[源码设置指南](../deploy/README.zh-CN.md)或[示例](../examples/community/README.zh-CN.md)。

## 能力地图

下表的“Community”表示源码预览包含该能力，不表示运营者已配置所需服务或启用可选工作流。

| 能力                  | 输入                                                   | 输出                                       | 前提                                                                              | 状态与入口                                                                                                                            |
| --------------------- | ------------------------------------------------------ | ------------------------------------------ | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| 知识检索              | 已授权文档和问题                                       | 带检索片段引用的回答                       | 已建立索引且可用的文件、合适的模型配置和文件权限                                  | Community；[知识库指南](help/knowledge-base.zh-CN.md)                                                                                 |
| 文档矩阵              | 选定文件和可复用问题集                                 | 按文件和问题组织的表格，包含回答与来源引用 | 已处理的文档、受支持的提取方式和每份文件的访问权限                                | Community；[供应商评审示例](../examples/community/supplier-review/README.zh-CN.md)                                                    |
| 业务分析              | 问题、指标定义、时间范围、筛选条件和已授权数据         | 可审阅的计算、图表、表格与解释             | 已配置模型和数据工具；当前治理数据集成为 Datarc；使用沙箱的计算还需要兼容执行环境 | Community；[收入分析示例](../examples/community/revenue-review/README.zh-CN.md)                                                       |
| 报告                  | 分析结果、证据、目标读者和内容范围                     | 工作区报告及受支持的下载格式               | 已启用报告工作流；Canvas 下载需要其渲染工具链                                     | Community；[报告修改指南](help/revise-report.zh-CN.md)；原位修改取决于部署                                                            |
| 原生文档技能          | 结构化内容和明确的文件格式要求                         | 基础 DOCX、PDF、PPTX 或 XLSX 文件          | 已选择相应 `bowra-*` 技能、文件权限和本地依赖                                     | Community；[文档技能指南](help/create-agent.zh-CN.md)；这些基础技能不提供模板保真、OCR 或公式计算                                     |
| 专业智能体与 MCP 工具 | 指令、选定能力和已授权连接                             | 可复用智能体与工具辅助成果                 | 所有者配置、工具权限、连接凭据和必要的调用确认                                    | Community；[创建智能体](help/create-agent.zh-CN.md)、[团队 MCP](help/team-mcp-tools.zh-CN.md)、[个人 MCP](help/personal-mcp.zh-CN.md) |
| 智能体版本 CI/CD      | 限定权限的 API 凭据、单个智能体的授权和明确的 Git 变更 | 提案、候选版本、评估执行与就绪信息         | 已启用自动化／评估工作流和 Developer API 传输入口；需要所有者接受变更             | 已实现，需主动启用；[Developer API 发现](#发现-agent-developer-api)与[示例](../examples/community/agent-automation/README.zh-CN.md)   |

## 输出中的 Verified Intelligence

让证据贴近观点。文档引用指向片段；查询和代码运行来源可以显示结果表与执行输出。有相应记录时，数字溯源将指标连接到输入和计算。说明指标定义、单位、时间范围、筛选条件与假设，让审阅者判断计算是否回答了目标问题。

明确说明证据缺失或来源不可用的情况。引用标记、已完成的运行或导出的文件，都不能单独证明结论正确。保留不确定性，区分提供的事实、计算结果和你的解释。不要为了填补空缺而编造查询结果或来源引用。

## 集成边界

- **已连接的 MCP 工具：** Bowra 内的智能体调用外部 MCP 服务提供的工具。个人和团队连接各自有访问与确认规则。
- **Agent Developer API：** 外部自动化可以参与下述限定范围的智能体版本工作流。这不会授予对话、知识库、报告或其他用户资源的一般访问权限。
- **外部通用任务执行：** 此源码预览尚未发布供其他 Agent 调用 Bowra 的受支持 MCP 服务端或通用任务委派 API。不要根据浏览器内部路由或开发工具推断存在这类接口。

交互式分析任务应引导已授权用户进入配置好的 Bowra 工作区，并阅读相应指南。修改仓库时，遵循 [AGENTS.md](../AGENTS.md) 和[贡献指南](../CONTRIBUTING.zh-CN.md)。

## 发现 Agent Developer API

使用部署运营者提供的公开服务地址，其中应包含配置的路径前缀，例如 `/bowhead`。本指南不假设存在托管的 Bowra 服务地址。

1. 由团队所有者或管理员进入 **团队设置 → OpenAPI**。该页面管理 Community 的智能体版本凭据，并提供当前部署的文档。
2. 阅读 `<service-public-address>/api/openapi/v1/reference.md`。生成的参考说明当前开放的端点和请求字段。同一服务地址下的 `/api/v1/team-openapi/documentation` 提供所有者／管理员可读的 JSON 文档。
3. 使用已授权密钥，带上 `Authorization: Bearer <token>` 调用 `GET /api/openapi/v1/scopes`，获取当前已授予且已安装的权限范围。仅存在某个 scope 不代表版本自动化已启用；还要检查生成的参考是否列出所需端点。
4. 使用智能体所有者的自动化授权，它绑定允许的密钥、智能体、操作、仓库／ref 声明和有效期。智能体工作台不再提供新建授权的表单，已有授权继续有效。如果端点不存在，先由运营者检查可用状态。

Developer API 传输入口在本地部署可用；SaaS 部署需要运营者另外主动启用。版本自动化和评估也需要主动启用。Community 包含相关代码，并不代表任意部署中的 CI/CD 集成都已经验证可运行。

### 版本工作流

下列路径均相对于 `/api/openapi/v1`。准确的请求体、身份字段和当前可用状态，以生成的参考及响应为准。

| 步骤                         | 端点                                                                    | 所需 scope 与授权操作                        |
| ---------------------------- | ----------------------------------------------------------------------- | -------------------------------------------- |
| 读取当前草稿身份             | `GET /agents/{agent_id}/version-context`                                | `agent_versions:read` + `gate:read`          |
| 提交提案供所有者审阅         | `POST /agents/{agent_id}/version-proposals`                             | `agent_versions:write` + `proposal:create`   |
| 读取提案状态和已接受草稿身份 | `GET /agents/{agent_id}/version-proposals/{proposal_id}`                | `agent_versions:read` + `gate:read`          |
| 准备所有者接受的草稿         | `POST /agents/{agent_id}/version-proposals/{proposal_id}/prepare`       | `agent_versions:write` + `candidate:prepare` |
| 运行候选版本比较             | `POST /agents/{agent_id}/release-candidates/{candidate_id}/comparisons` | `agent_versions:write` + `eval:run`          |
| 读取就绪状态与下一步身份信息 | `GET /agents/{agent_id}/release-candidates/{candidate_id}/gate`         | `agent_versions:read` + `gate:read`          |

![外部提案经过所有者审阅、候选版本评估，再由人决定发布](assets/bowra/agent-lifecycle.svg)

按实际接口约定保存返回的 ID、行版本、哈希和幂等键。仓库／ref 声明用于记录来源和减少操作错误，不是独立的仓库所有权证明。真正的授权边界是有效凭据、可撤销的单个智能体授权，以及允许执行的操作。

自动化不能接受自己的提案、确认风险或代替所有者发布版本。接受什么变更、发布什么版本，由所有者决定；要求管理员批准时仍须批准。停止外部流水线访问智能体：在团队设置 › OpenAPI 禁用或注销它用的密钥。需要审阅代码时可查看[路由实现](../packages/bowhead_gateway/src/bowhead_gateway/routes/team_openapi.py)；集成仍应从实际部署的参考开始。

## 规划第一个采用任务

选择一个[示例](../examples/community/README.zh-CN.md)，使用有权访问的输入，并在运行前约定可审阅的输出。通过[设置指南](../deploy/README.zh-CN.md)确认来源权限、模型／工具配置及执行或导出依赖。扩大使用范围前，检查实际输出及其证据。

将凭据保存在运营者认可的密钥存储中。不要把 API 密钥、私有文档或真实客户数据放入贡献或示例。Community 不需要商业激活，也没有用户数量上限；模型与基础设施费用以及已配置的用量预算仍然适用。
