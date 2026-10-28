# Agent 自动化：提案自动化，发布由所有者决定

[English](README.md) | 简体中文 | [全部示例](../README.zh-CN.md)

**合成场景：**团队维护一个收入复盘 Agent。仓库中的修改增加两条要求：扣除退款后计算净收入，并说明缺失的因果证据。CI 可以提交提案、评测已接受的候选版本；Agent 所有者决定接受和发布什么。

这是基于现有 API 的接入说明，不是可直接运行的 CI 流程，也不代表评测已经通过。它需要已配置的部署、真实所有者授权和可工作的评测环境。示例不提供凭据或服务地址。

## 1. 确认功能可用并创建有限授权

请部署管理员确认 Agent 版本自动化已启用。实现需要 `FEATURE_AGENT_VERSION_AUTOMATION`、依赖的 `FEATURE_AGENT_OWNER_VERSION_EVAL` 和 `FEATURE_AGENT_EVAL_M3`，Eval 与 Owner 版本治理默认开启；提供 Team OpenAPI 的部署默认开启自动化，包含 local。部署显式关闭时仍以该配置为准。同时必须提供 Team OpenAPI：local 模式提供该入口，SaaS 需要单独启用 `FEATURE_TEAM_OPENAPI_SAAS`。本示例不会开启开关，也不提供灰度计划。

通过现有界面配置：

1. Team 所有者或管理员在**团队设置 → OpenAPI**创建仅具备 `agent_versions:write` 权限的密钥；该权限包含对应的读取权限。将令牌保存在 CI 密钥管理器中，创建界面只显示一次。
2. **Agent 所有者**进入该 Agent 的版本工作台，创建并保存工作副本。
3. 该密钥需要已有的 Agent 自动化授权，只包含需要的动作：`proposal:create`、`candidate:prepare`、`eval:run` 和 `gate:read`，并限定仓库、ref 和到期时间。智能体工作台不再提供新建授权的表单，已有授权继续有效。

仓库与 ref 由调用方声明，不是请求来自 GitHub 的密码学证明。真正的权限边界是绑定密钥、限定一个 Agent、会过期且可撤销的授权。Team 管理员身份不能替代 Agent 所有权。

## 2. 读取精确的工作副本

API 基址为部署的公开地址（包含配置的路径前缀）加 `/api/openapi/v1`。使用 `Authorization: Bearer <your-token>` 认证；不要把令牌放在 URL 或提交到仓库的文件中。

先通过 `GET /scopes` 检查密钥，再调用：

```text
GET /agents/{agent_id}/version-context
query: grant_id, repository, ref
```

使用返回的 `working_copy_id`、`working_copy_row_version` 和 `base_release_id`。如果 `working_copy` 为 `null`，所有者需要先创建并保存草稿。不能编造 ID、猜测行版本，或把线上版本 ID 当作工作副本 ID。

## 3. 提交提案，等待所有者接受

```text
POST /agents/{agent_id}/version-proposals
header: Idempotency-Key
body: grant_id, repository, ref, commit_sha,
      base_release_id, working_copy_id, working_copy_row_version,
      patch_hash, operations, atomic_groups, suggested_cases?, rationale?
```

[英文版的 JSON 片段](README.md#3-submit-a-proposal-wait-for-owner-acceptance)展示了真实支持的操作：替换合成 Agent 的 `/definition/system_prompt`，将修改放入一个原子组，并建议一个退款计算评测案例。替换真实 Agent 的完整提示词前，应审阅并保留仍然必要的原有指令。

`patch_hash` 是 `sha256:` 加上**仅对 `operations`** 求得的 SHA-256 十六进制摘要。序列化规则为 UTF-8 JSON、`ensure_ascii=False`、`sort_keys=True`、分隔符 `(',', ':')`。`commit_sha` 应记录你实际的仓库版本。示例片段不是完整 HTTP 请求；版本标识应从 API 获取，摘要必须由实际提交的操作计算。

拿到提案 ID 不等于修改已获接受。使用相同密钥和授权轮询：

```text
GET /agents/{agent_id}/version-proposals/{proposal_id}
query: grant_id, repository, ref
```

所有者在工作台审阅并选择原子组。接受后，`accepted_result` 才会包含下一步所需的精确工作副本 ID 和行版本。建议的评测案例仍需正常的评测计划审阅；提交建议不证明案例已执行或通过。

## 4. 准备候选版本并评测

| 动作                 | 方法和路径                                                                                    | 授权动作                          | 请求上下文                                                                                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| 准备并运行已接受提案 | `POST /agents/{agent_id}/version-proposals/{proposal_id}/run`                                 | `candidate:prepare` 和 `eval:run` | 授权、仓库、ref；可选 `release_note`；`Idempotency-Key` 请求头。服务端自己读取接受后的工作副本、空间和策略                                       |
| 准备已接受提案       | `POST /agents/{agent_id}/version-proposals/{proposal_id}/prepare`                             | `candidate:prepare`               | 授权、仓库、ref；接受后的工作副本 ID 和行版本；`workspace_id`；可选 `expected_eval_policy_revision_id`、`release_note`；`Idempotency-Key` 请求头 |
| 读取候选门禁         | `GET /agents/{agent_id}/release-candidates/{candidate_id}/gate`                               | `gate:read`                       | 授权、仓库、ref 查询参数                                                                                                                         |
| 启动对比评测         | `POST /agents/{agent_id}/release-candidates/{candidate_id}/comparisons`                       | `eval:run`                        | 公共上下文；`candidate_row_version`、`expected_control_row_version`、`candidate_hash`、`plan_id`、`plan_hash`；`Idempotency-Key` 请求头          |
| 取消本授权的评测     | `POST /agents/{agent_id}/release-candidates/{candidate_id}/comparisons/{execution_id}/cancel` | `eval:run`                        | 公共上下文；`expected_execution_row_version`                                                                                                     |

`workspace_id` 是已持久化的 API 字段，表示获准用于执行的 Space，应保留该技术名称。使用准备和门禁响应返回的标识；查看 readiness、`reason_codes` 和 `primary_action`，不要自行构造候选或计划摘要。版本或策略冲突时应刷新并重新审阅。仅在重试同一个预期操作时复用幂等键。

评测返回 `202` 只代表请求已接受或排队，不代表通过。模型或运行环境故障不能证明 Agent 质量差。记录实际结果，并让所有者查看失败或未完成的原因。

## 5. 所有者决定何时生效

自动化凭据不能接受提案、代表所有者确认风险、修改可见范围或发布版本。所有者审阅候选和评测证据后，使用工作台当前提供的操作。个人版本通过个人应用流程生效；扩大可见范围可能需要管理员审批。不要编造自动化 `/publish` 或 `/release` 端点。

你自己的接入完成记录应包含：提案 ID、所有者接受后的工作副本标识、候选及计划标识、实际对比评测结果，以及所有者随后的发布决定。本示例不提供这些线上运行结果。

撤销授权会停止该授权的自动化访问。停止外部流水线访问智能体：在团队设置 › OpenAPI 禁用或注销它用的密钥。授权到期或 Agent／所有者不再符合条件时应停止使用。

## 权威来源

- [Agent 发布权限](../../../docs/help/permissions/publish-agent.zh-CN.md)
- [当前 API 模型和路由](../../../packages/bowhead_gateway/src/bowhead_gateway/routes/team_openapi.py)
- [提案操作校验](../../../packages/bowhead_core/src/bowhead_core/agent_change_proposal_store.py)

开发客户端前，请读取实际部署的 OpenAPI 文档；自动化未启用时，文档不会列出 Agent 端点。本指南描述源码契约，不声称 CI、LLM 或生产部署已经验证成功。
