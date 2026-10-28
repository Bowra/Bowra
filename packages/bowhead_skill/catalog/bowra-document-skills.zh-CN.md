# Bowra 文档 Skills

[English](bowra-document-skills.md) | 简体中文

四个独立编写的文档包，为当前受限目录包提供可单独分发的源码替代。

| 包                                | 支持生成                             | 支持检查                           |
| --------------------------------- | ------------------------------------ | ---------------------------------- |
| [bowra-docx](bowra-docx/SKILL.md) | 可编辑标题、段落、项目列表和表格     | 按正文顺序读取文本及顶层表格       |
| [bowra-pdf](bowra-pdf/SKILL.md)   | 自动分页的文本、表格报告             | 按页读取嵌入文本；不包含 OCR       |
| [bowra-pptx](bowra-pptx/SKILL.md) | 可编辑的 16:9 标题、要点、表格幻灯片 | 顶层幻灯片文本和表格               |
| [bowra-xlsx](bowra-xlsx/SKILL.md) | 值、日期、多表、数字格式和筛选       | 单元格值及明确标注未计算的公式文本 |

每个包包含英文指引、封闭 JSON Schema、一个独立 CLI、合成示例、评测提示、已测试的直接依赖、MIT LICENSE 和来源记录。脚本调用公开库，不导入旧 Skill 包或 Bowhead 私有模块。

## 运行示例

在已同步并安装现有依赖的工作区内：

```bash
uv run --no-sync python packages/bowhead_skill/catalog/bowra-docx/scripts/run.py \
  --input packages/bowhead_skill/catalog/bowra-docx/examples/create.json \
  --workdir /tmp/bowra-docx-example
```

输出目录必须是新的目录。把两个包路径同时换成其他包即可运行对应示例。离开本工作区使用时，创建 Python 3.12 环境并安装包内 `requirements.txt`；脚本执行期间不下载依赖。

生成结果返回真实文件名、大小和 SHA-256。检查文件时，先在 workdir 内放置已授权的副本，再传入 `{"operation":"inspect","source":"inputs/source.docx"}`。预算和不支持的对象见各包 reference。

## 集成状态

这些是位于 `catalog/` 的标准内置 Skills，现有 loader 按独立 `bowra-*` 名称发现，仍遵循启用状态和精确资源授权。此次没有修改默认绑定、Tool 权限或已有 Agent 发布版本。脚本兼容现有 `--input/--workdir` 协议；Artifact 注册及源文件准备由调用平台负责。

旧受限包继续列为私有排除项。后续 Community 源码组装需要显式选择新包、审查精确内容和哈希、经拥有者确认更新 Agent 绑定，并验证安装后的依赖闭包。沙箱代码和镜像保持现状，源码替代不会自动移除镜像已复制的 helper。

这批基础实现不覆盖旧包的全部功能。模板保真、Office 渲染、OCR、修订、复杂对象和公式计算需要独立实现或使用已授权的现有工作流。不得把 CLI 当作平台中绑定原始材料的草稿确认或发布流程。

## 维护

各包 MIT 仅覆盖新编写文件。保留外部依赖许可，并按实际分发环境生成 notices。后续修改以公开 API 和独立需求为依据，不把旧受限 helper 迁入新目录。

运行针对性功能测试：

```bash
uv run --no-sync pytest packages/bowhead_skill/tests/test_community_document_skills.py
```

包内提供后续模型评测提示；当前自动检查验证 CLI 行为与文档内容，不声称已完成大模型对比评测。
