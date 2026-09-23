# 更新到 Tripwright

本次修改包括品牌、SVG favicon、README 图片、对话模型选择与深度思考。没有数据库表结构变更。Compose 项目名、数据卷、数据库名、队列名及 Cookie 名继续使用原有标识，以便接着使用已有旅行和登录状态。

## 1. 本机提交到 GitHub

在 PowerShell 执行：

```powershell
cd D:\F_Drive\AI-Guide
git status --short
git diff --check
git add README.md README.en.md .env.example backend deploy/.env.production.example docs web
git --no-pager diff --cached --stat
git diff --cached --name-only
git commit -m "Rename to Tripwright and add model selection with thinking controls"
git push origin main
```

确认提交列表没有 `.env`、`web/.env.local`、`work/`、数据库备份或真实 Key。暂存前若还有你另外修改的文件，先审查，不要将无关改动一起提交。`--no-pager` 会直接打印结果，避免卡在分页器；已经进入分页器时按 `q` 退出。

仓库仍可叫 `AI_Guide`，不影响网站显示 Tripwright。若也要修改 GitHub 仓库名，在仓库 Settings → General → Repository name 改为 `Tripwright`。确认改名成功后，本机和服务器分别执行：

```bash
git remote set-url origin https://github.com/DirtyDidsDoneDerCheap2049/Tripwright.git
```

随后把中英文 README 的克隆地址和 `cd AI_Guide` 更新为新名称。服务器目录 `/home/ubuntu/ai-guide` 不用搬动，网站域名也可以继续用 `guide.dirtydev.cc`。

等 GitHub Actions 本次提交全部通过，再更新服务器。特别是 Docker 构建与 Compose 启动，需要 Linux CI 或服务器验收，本机单元测试不能替代。

## 2. 服务器更新前

在腾讯云 Ubuntu 的终端执行：

```bash
cd /home/ubuntu/ai-guide
git status --short
git rev-parse HEAD
sudo env COMPOSE_OVERRIDE_FILE=deploy/compose.small.yml bash deploy/backup.sh
```

记下更新前的提交 SHA。备份默认会短暂停止 API 和 Worker，执行完成后恢复；挑没有人在使用的时间运行。备份失败就停止更新。不要覆盖服务器已有 `.env`，也不要运行 `docker compose down -v`。

## 3. 拉取代码并设置模型

```bash
git pull --ff-only origin main
nano .env
```

保留数据库、会话、模型和高德原配置。在 `.env` 加入 [模型配置说明](MODELS.md) 的 `TEXT_MODEL_PROFILES`，至少配置两个确实可用的模型后，网页才有两个可选项。列表为空则只展示原 `TEXT_MODEL`，不会自动猜测账号支持的模型。

没有填新变量也能启动，默认保持原接口；Qwen 官方 DashScope 接口会自动识别开关参数，代理地址须显式声明。每个模型的计价按供应商实际设置。

## 4. 分开构建并启动

下面每一条成功后再执行下一条。在 2GB 机器上不要并行构建。

```bash
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml config --quiet
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml build api
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml build web
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml up -d
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml ps -a
curl -fsS https://guide.dirtydev.cc/api/health/ready
```

本次沿用现有镜像 tag 即可，`build` 更新本机镜像，`up -d` 依据新镜像重建容器。若只改环境变量，也需要 `up -d` 重建；`restart` 不会更新容器环境。前端代码和 favicon 都需要重建 `web`。

验收：

- 首页、工作区、标签页显示 Tripwright，新图标可见；必要时 Ctrl+F5 强制刷新。
- 旧账号、已有旅行、收藏与路线仍可打开。
- 模型下拉列表符合 `.env`，两种模型分别回答一次。
- 点档位按钮展开滑条，选择轻度、标准或深入后发送，回答旁显示深度思考；选择关闭后下一条恢复普通模式。
- 正在生成时切换模型，不会改变已发送任务；刷新后能继续查看结果。
- API healthy、迁移容器退出码 0，Worker 无持续异常。

查看错误与内存：

```bash
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml logs --tail=100 api worker web
sudo docker stats --no-stream
free -h
```

## 5. 需要回退时

本次未改表结构，可回到之前的代码重新构建，不需要还原数据库。将命令中的 `OLD_SHA` 换成第 2 步记录的值：

```bash
git switch --detach OLD_SHA
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml build api
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml build web
sudo docker compose -f docker-compose.yml -f deploy/compose.small.yml up -d
```

确认服务恢复后再处理新版本问题。修好后 `git switch main`、`git pull --ff-only origin main` 并重新构建。回退期间不要删卷或重置数据库。
