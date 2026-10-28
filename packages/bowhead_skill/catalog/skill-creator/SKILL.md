---
name: skill-creator
description: "创建或修改可复用的 Bowhead Skill。用于创建技能、把已验证流程保存为技能、修改技能正文或附属文件。"
version: 1.1.0
title: "Skill creation guide"
title_zh: "Skill 创建向导"
category: platform
---

# Skill 创建与修改

## 1. 确定目标和来源

先复用用户本轮要求和已有对话中的信息：要解决的问题、触发场景、输入、输出、可用工具。
只有缺少影响实现的必要信息时才提问；不要把已经明确的需求重新拆成固定四轮问答。

修改已有 Skill 时先调用 `skill_read(name="目标名称")`，查看 `origin`、`body_raw`、`scripts`、`files`、`references` 和 `edit_hint`。
目录名称来自 `skill_list` 或用户明确指定，不凭空猜测。个人版优先于同名团队版，修改前必须确认读到的是用户要改的来源。
团队版先在设置中「复制为我的」，完整保留正文、脚本和参考资料，再改个人版并提交更新；保存个人 Skill 不会直接修改团队版本。

## 2. 起草或修改清单

新建时写完整 `SKILL.md`：

```yaml
---
name: my-skill
description: "解决什么问题、什么时候使用、需要什么输入"
version: 0.1.0
---
```

名称为小写字母、数字和短横线，最多 64 字符，不能以短横线开头。description 最多 2048 字符。
正文写清执行步骤、输入来源、工具调用、输出要求和关键边界；长资料放 references，通过明确相对路径按需读取。
描述是 Agent 选择 Skill 的依据，应具体说明适用场景，不能只写「分析数据」。工具声明不会授予实际权限。

修改时以 `body_raw` 为基础保留所有 frontmatter 字段；`body` 是展示文本，不能作为完整清单保存。
就地修改不改变 name。用户明确要另存为新 Skill 时才创建新名称，并完整复制所需资料和脚本。

## 3. 按需读取和修改文件

- `skill_read(name="目标名称", file="scripts/nested/run.py")` 读取脚本源码。
- `skill_read(name="目标名称", ref="nested/rules.json")` 读取参考资料，支持嵌套 UTF-8 文本。
- `skill_read(name="目标名称", file="evals/evals.json")` 读取评估样本。
- 文件名必须取自清单或用户指定；路径相对 Skill 根目录，不能使用绝对路径、上级目录或符号链接。

需要确定性计算时再增加脚本。Python 脚本须通过语法校验，每份最多 1 MB；整包最多 16 MB。
用户脚本只在沙箱执行。支持的语言和命令方式以当前工具 schema、`script_execution` 和 `command_usage` 为准。
使用 Skill 本身记录的输入与输出协议，不给任意上传脚本强加同一个命令行协议。
`script_execution=disabled` 表示当前部署不能执行用户脚本；不能把保存成功说成运行成功，也不要重复尝试执行。

## 4. 保存

当前工具列表含 `skill_save_generated` 时，使用准确的工具名调用：

```text
skill_save_generated(body_md="完整 SKILL.md", overwrite=true)
```

新建省略 overwrite；已有个人版修改才传 true。遇到非预期的同名冲突时先读取旧版，不能机械覆盖。
只改正文时省略 `scripts`。省略保留旧脚本；显式 `scripts=[]` 删除整个脚本目录；提供 scripts 列表会整体替换，必须包含所有要保留的脚本。

如果当前工具 schema 提供 `files`，优先用逐文件补丁：

```json
{
  "body_md": "完整 SKILL.md",
  "overwrite": true,
  "files": [
    { "path": "references/nested/rules.json", "content": "新的参考资料文本" },
    { "path": "scripts/run.py", "content": "print('ok')\n" }
  ]
}
```

未列出的文件保留。`content: null` 明确删除该文件，仅在用户要求删除时使用。
不要同时通过 scripts 和 files 改脚本。未开放 files 时，配置页仍可编辑已有文件；不调用未暴露参数或虚构文件写入工具。
校验失败先修复提示中的问题，再保存；权限失败不能换接口绕过。

## 5. 读回并验证

保存结果 `ok=true` 后重新 `skill_read`，检查正文和已修改文件，并确认未改文件仍在。
需要执行验证时，在已有用户授权和有效输入范围内按 Skill 的文档调用，检查真实工具结果与产物。
没有执行条件时准确说明「已保存，尚未试跑」，不把上传校验、审批或旧试跑徽标当成新内容的验证。

最终说明保存到个人 Skill 库、改了什么、验证了什么。团队同步走「提交更新」和审批，不声称个人保存已全团队生效。
