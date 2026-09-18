# 参与 AI-Guide 开发

先按 [开发指南](docs/DEMO-STARTUP.md) 启动，接口与数据流见 [架构说明](docs/ARCHITECTURE.md)。欢迎提交能复现的问题、交互改进、回归测试和文档修正。

## 提交问题

说明浏览器、部署方式、复现步骤、实际与预期结果。截图用示例旅行；日志只截相关错误，遮挡个人信息。不要上传 `.env`、数据库、Cookie、验证链接或供应商密钥。安全问题按 [SECURITY.md](SECURITY.md) 处理。

## 提交代码

一次改动解决一个明确问题。UI 变化附前后截图；接口变化同步前端类型与错误处理；数据变化增加 Alembic 迁移，不重写已应用迁移。说明测试结果与未验证项。

可以使用 AI 辅助开发，提交者应理解代码并亲自验证行为。不把生成报告或计划当作测试结果。

```bash
git config core.hooksPath .githooks
python scripts/secret_scan.py --root . --git-worktree --fail-on-credentials --quiet
python -m unittest discover -s scripts/tests -v
```

前端在 `web/` 执行 `npm ci`、`npm run format:check`、`npm run build`。后端集成测试需要专用 MySQL 测试库和 Redis DB，配置与命令见开发指南。测试夹具会清理测试数据，不得指向线上实例。

本地历史原型、内部任务卡、实际环境文件和运行数据不属于公开源码，保持忽略规则。暂存后钩子检查 Git 暂存内容，CI 再检查跟踪文件；请不要用强制添加绕过配置文件排除规则。
