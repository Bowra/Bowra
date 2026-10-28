# 从源码构建 Bowra

[English](README.md) | 简体中文

需要 Python 3.12、`uv`、Node.js 24 和 npm。所有命令从独立的社区版仓库根目录执行。已提交的 Gateway 与前端清单都必须选择 `community`；版本选择是构建输入。

## 安装与配置

```sh
make install
make install-frontend
cp .env.example .env
```

首次启动前，只生成一次实例密钥。以下命令将密钥写入私有 `.env`，不会将其打印出来：

```sh
uv run python - <<'PYTHON'
from pathlib import Path
import secrets
from cryptography.fernet import Fernet

path = Path('.env')
text = path.read_text()
for name, value in {
    'BOWHEAD_FERNET_KEY': Fernet.generate_key().decode(),
    'BOWHEAD_JWT_SECRET': secrets.token_urlsafe(48),
}.items():
    placeholder = name + '=\n'
    if placeholder not in text:
        raise SystemExit(f'{name} is already configured; preserve existing keys')
    text = text.replace(placeholder, name + '=' + value + '\n', 1)
path.write_text(text)
path.chmod(0o600)
PYTHON
```

检查 `.env`。默认数据库相对路径为 `.data/sqlite/bowhead.db`，请保持工作目录稳定。升级和恢复时保存两个密钥与全部数据库、对象数据。删除凭据账本独立保存在 `.data/privacy-ledger/deletions.db`；恢复应用数据库时必须保留该账本当前的持久状态，不能随旧数据库一起回退到旧账本，否则可能让已删除的数据重新出现。不要把商业版数据库用于首次 Owner 初始化。

## 运行时前提

启动 Gateway 前，必须在 `.env` 中配置可访问、兼容的沙箱执行器，包括 `BOWHEAD_SANDBOX_EXECUTOR_URL` 和 `BOWHEAD_SANDBOX_EXECUTOR_TOKEN`。缺少执行器配置时无法启动。令牌由执行器维护者提供；在执行器端轮换后需要同步更新此部署，错误令牌会导致执行失败。此源码预览不提供已发布的受支持沙箱镜像或完整运行时部署。

调用沙箱执行的工作流还要求 `BOWHEAD_SKILL_WORKDIR_ROOT` 在 Gateway、执行器和 Docker 宿主机中指向同一个可写目录，并使用完全相同的绝对路径。执行器将每次运行的子目录挂载到沙箱内。远程接口可访问或 SSH 隧道连接成功不代表文件系统已经共享。开放这些工作流前，应验证真实执行、隔离与输出文件下载。原生 `bowra-docx`、`bowra-pdf`、`bowra-pptx` 和 `bowra-xlsx` 文档 Skill 使用应用环境中已安装的依赖执行，不要求沙箱共享目录。

## 构建与启动

```sh
make typecheck-frontend
make build-frontend
make lint
make run-gateway
```

前端默认构建英文。`deploy/Dockerfile.app` 的应用镜像会构建全部受支持的界面语言；以上源码命令为本地开发构建一种语言。已保存的语言选择优先；未设置时，在存在相应语言构建的前提下跟随浏览器或系统语言。

`make run-gateway` 导出私有 `.env` 配置，检查安装来源，完成受支持的迁移与回填准备，然后在 8080 端口启动 Uvicorn。打开 `http://localhost:8080/activate/`，在另一个终端、同一目录和配置下读取待初始化凭证：

```sh
uv run python -m bowhead_gateway.installation setup-token
```

完成[社区版初始化](community-setup.zh-CN.md)，登录并配置团队模型凭据。模型服务、数据源、邮件和 MCP 连接分别需要相应凭据及权限。社区版不要求商业激活、不内置用户数上限；账号权限和用量预算仍然生效。

发送系统邮件时，请配置 SMTP 服务及服务商授权的 `BOWHEAD_SMTP_FROM` 发件地址，或在团队邮件服务中保存已授权的发件地址。社区版不提供默认发件域名，缺少发件地址时不会投递。在对话中发信必须配置团队邮件服务。

前端开发需要热更新时，在 Gateway 运行期间执行 `make run-frontend`，API 请求会代理至 8080 端口。如果修改 Gateway 端口，同时调整 `BOWHEAD_GATEWAY_URL`、`BOWHEAD_EXPORT_FRONTEND_BASE_URL` 和开发代理目标。这些是内部回调地址；导出渲染器必须能访问已构建的前端。

## 执行运行时与发布

Canvas 导出需要独立的本地渲染工具链：Node.js、Poppler（`pdftoppm`）、LibreOffice（`soffice`，用于 PowerPoint 和电子表格导出）、锁定的前端 npm 依赖以及 Playwright Chromium。先安装适合操作系统的软件包，再从前端目录安装浏览器：

```sh
cd packages/bowhead_frontend
npx playwright install chromium
```

Linux 还需按所用发行版的 Playwright 系统依赖说明安装浏览器所需的系统库。就绪端点检查部分可执行文件和脚本路径，不能证明浏览器已安装或导出能够成功。请使用实际工具链分别测试 PDF、PowerPoint 和电子表格导出。本地 Canvas 渲染与沙箱 Skill 执行的依赖不同。

可以通过 `docker build -f deploy/Dockerfile.app -t bowra-app:local .` 构建应用镜像。应用镜像只是部署组件之一；该命令不会构建沙箱、发布镜像或构成完整生产部署。运行时组成必须使用独立审查过的组件和持久配置。

应用构建使用 Node.js 24.22.1 和更新后的前端依赖锁。沙箱及 HTML 默认限制现在遵循团队设置，并受部署上限约束；普通沙箱请求默认 120 秒，部署默认上限为 600 秒。Gateway 与独立管理的 executor 限制必须一致；旧 executor 可能仍执行较小上限。源码预览不提供沙箱镜像；使用自定义 JavaScript Skill 前，请在运营者提供的运行时验证 Node.js 24 与隔离边界。

## 开发检查

参见[贡献说明](../CONTRIBUTING.zh-CN.md)。按修改范围选择测试，保持生成的主题登记表与 `config/theme_surface_coverage.yaml` 一致，保留第三方声明。升级已初始化的实例前先备份完整恢复材料。

## 升级已有实例

首次启动升级版本前，停止写入并建立恢复集：全部八个应用数据库、存储文件和当前实例配置，包括原有加密密钥与 JWT 密钥。单独保留当前注销回执账本，不能用旧账本覆盖它。

修订 `0190` 删除 Agent 版本 Webhook 的订阅、事件和投递数据。自动化客户端应通过有范围授权的 Developer API 查询状态；如果需要这些历史记录，请保留升级前的备份。

本快照通过一个前向修订，将数据库从 Community 1.6.3 的 `0195` 升级到 `0196`。该修订约束对精确已保存评估用例的修改建议，不重写早期用例。更早的已初始化实例仍需应用之前的迁移链。修订 `0192` 已退役未被读取的用量来源密文列，同时保留用量回执。诊断记录和检查点使用已有加密根密钥，必须保留。正常启动准备会先迁移，再提供请求服务。修订 `0187` 删除已退役的术语别名及其快照；空间指令取代旧功能。如需保留历史内容，请保存原始备份。旧应用代码不能打开升级后的数据库。回滚时，恢复与旧源码版本配套的数据库和文件，保留当前注销账本，并执行受支持的隐私准备流程。先在恢复集的副本上验证升级。
