# 安全反馈

[English](SECURITY.md) | 简体中文

## 报告漏洞

本 GitHub 仓库提供私密反馈时，使用 **Security → Report a vulnerability**。若没有此选项，可以创建 Issue 请求安全联系方式，但不要包含漏洞细节、凭据、客户数据或利用材料。等待维护者建立私密渠道后，再提供敏感信息。

私密报告中请说明受影响的源码修订、配置、影响和最小复现步骤，使用合成账号与数据。未经对方授权，不要测试他人的部署。

## 支持范围

本仓库为社区源码预览，不承诺响应时限、长期支持周期或托管服务。依赖某个构建前请查看仓库当前版本说明。运营方负责更新依赖和运行时镜像、保护实例密钥、限制外部集成并维护备份。

不涉及敏感信息的普通问题可以通过仓库 Issue 流程反馈。提交修复时保留第三方安全声明及署名。

## ChromaDB 依赖安全公告

截至 2026-11-10，锁定的 `chromadb` 版本为 `1.5.9`。Python 包审计报告四项不同的公开安全公告：[CVE-2026-45829](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c)、[CVE-2026-45830](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr)、[CVE-2026-45831](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) 和 [CVE-2026-45833](https://github.com/advisories/GHSA-36p7-vc44-83pf)。其中一项在扫描输出中重复出现。上游尚未列出修复版本，详见[上游版本状态记录](https://github.com/chroma-core/chroma/issues/7909)。这些发现仍未关闭。

Bowra 使用本地 `PersistentClient`、应用自行提供的向量，以及自身的 Team 和 Space 访问检查。应用构建配方不启动 Chroma 独立 HTTP 服务，也不使用其 HTTP 授权提供器。这限制了服务端公告所述路径，但不代表该依赖没有漏洞。Chroma 客户端也可加载嵌入配置，因此不要将本安装连接到不可信 Chroma 服务、导入不可信 Chroma 存储或配置，或启用远程模型代码。限制本地 Chroma 目录仅供应用和可信运营人员写入；改变这些边界或另行部署 Chroma 服务前，须重新评估公告影响。
