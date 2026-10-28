# Community 中性演示模板

[English](community-neutral.md) | 简体中文

## 来源与重建

`community-neutral.pptx` 为 Community 独立编写，完整源码位于 [`../scripts/build_community_template.py`](../scripts/build_community_template.py)。生成器从公开 `python-pptx` 库的 `pptx.Presentation()` 空白演示文稿开始，不读取任何现有项目演示文稿、参考幻灯片、受限 Skill、图片、字体文件、公司数据或外部服务。

所有坐标、颜色和文字均在源码中独立定义。动态文字槽初始为空。两个原生图表仅含合成分类 `Example A`、`Example B`，数值 `1`、`2`，以及系列名 `Synthetic example`。固定文字仅为建议、洞察、负责人、时间及致谢等功能标签。字体仅按名称引用 Arial，不嵌入字体文件。演示文稿及内嵌工作簿元数据经过清理，ZIP 成员顺序和时间戳固定。

在仓库根目录使用锁定依赖重建：

```bash
uv sync --all-extras
uv run python packages/bowhead_skill/catalog/canvas-pptx/scripts/build_community_template.py
uv run python packages/bowhead_skill/catalog/canvas-pptx/scripts/build_community_template.py --check
```

可复现性以仓库锁定的 `python-pptx` 及工作簿依赖版本为准；升级依赖后应重新审阅生成文件。生成器及产物遵循所在仓库许可证，第三方库仍遵循其自身许可证。

## 组装约定

独立渲染器读取固定文件 `templates/default.json`，字段仅为 `schema_version: 1` 与 `template`。Community 选择 `community-neutral.pptx`；完整私有发行版保留现有模板选择。文件缺失、未知选择或模板目录外的路径会立即失败，不通过环境变量或应用包选择模板。

未明确提供允许且存在的 `template_path` 时，标准导出仍使用按内容分页的渲染器。明确提供空演示文稿时使用本模板回退；也可将本模板放入允许的临时目录或工作目录后明确指定。Community 默认生成英文标签，`locale: "zh-CN"` 保留已支持的中文标签，不翻译报告原文。显式模板路径仍有原有槽位字数限制。

模板包含七页 16:9 幻灯片：封面、摘要、指标、柱状图洞察、趋势图洞察、建议及结束页。布局角色依次为 `COVER`、`TITLE_ONLY`、`KPI_GRID`、两个 `CHART_SLIDE`、`TITLE_ONLY`、`CLOSING`。形状索引是渲染器的兼容接口，完整槽位表见[英文版](community-neutral.md#composition-contract)，修改视觉样式时不得重排。未使用的库布局保持空白，最后一个布局供附加设计摘要使用。

## 验证

执行 `uv run --extra dev pytest packages/bowhead_skill/tests/test_community_pptx_template.py -q`。测试检查精确重建、演示文稿及内嵌工作簿元数据和关系，并从临时的 Community 专用 Skill 目录调用真实渲染器，覆盖图表替换、空模板回退、缺失视觉页删除、中文偏好和无效清单。调整坐标或字体后还需检查渲染后的 PDF；结构测试不能证明排版可读。
