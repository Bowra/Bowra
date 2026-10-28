# Bowra 前端

[English](README.md) | 简体中文

Community 界面采用 Next.js、React 和 TypeScript。请先阅读仓库的[构建与初始化指南](../../deploy/README.zh-CN.md)和[贡献指南](../../CONTRIBUTING.zh-CN.md)。

## 开发

使用 Node.js 24 和 npm，在仓库根目录运行：

```sh
make install-frontend
make run-frontend
```

另开终端，通过 `make run-gateway` 启动配置好的网关。开发服务器将 API 请求代理至 `http://localhost:8080`，浏览器会话 Cookie 保持在前端来源下。

## 版本组装

`build-manifest.json` 固定选择 Community 版本。`npm run dev`、`npm run build`、`npm run typecheck` 和 `npm test` 会先组装已提交的 Community 模板和消息目录。生成文件已忽略，不应直接编辑；请修改 `edition/community/` 或 `edition/shared-messages/`。

Community 构建不需要 Business 源码目录。界面显示的产品名为 **Bowra**。现有 `bowhead_*` 标识和 `BOWHEAD_*` 配置名保持兼容。

## 验证

```sh
make typecheck-frontend
make build-frontend
cd packages/bowhead_frontend
npm test
```

静态构建输出至 `out/`。公开账户页面和导出页面使用独立布局，不挂载已登录应用组件。聊天渲染的修改必须保持实时事件、持久化消息与回放会话一致。

用户帮助文档位于 `docs/help/`，由 `lib/help-content.ts` 注册。修改可见行为时，同步英文和中文的现行规则，并维护其他已支持的翻译。
