# 从源码部署

本指南适用于单机 Linux、Docker Compose v2 和 Caddy 自动 HTTPS。源码、数据库和照片分别管理；更新应用不需要重新初始化数据库。

## 准备

- 安装 Docker Engine 和 Compose v2，确认 `docker run --rm hello-world` 正常。
- 域名 A 记录指向服务器公网 IPv4，开放 TCP 80/443。SSH 按自己的管理方式配置。
- 下载或克隆本仓库，在仓库根目录执行命令。数据库和 Redis 只在容器网络内开放。
- 2GB 机器使用 `deploy/compose.small.yml`：Worker 为单进程、两线程，MySQL buffer pool 为 128 MiB。所有启动和更新命令保持相同 Compose 文件组合。

## 配置

```bash
cp -n deploy/.env.production.example .env
chmod 600 .env
nano .env
```

分别运行 `openssl rand -hex 24` 生成数据库用户与 root 的独立密码，运行 `openssl rand -hex 48` 生成 `SESSION_SECRET`。填写到 `.env`，不要提交文件或粘贴到公开 Issue。

| 配置 | 填写要求 |
|---|---|
| `APP_ENV` / `PROVIDER_MODE` | `production` / `real` |
| `APP_VERSION` / `IMAGE_REGISTRY` | 自定版本标签，例如 `0.1.0`；源码部署使用 `local` |
| `MYSQL_DATABASE` / `MYSQL_USER` | 例如 `ai_guide`；新库首次启动时创建 |
| `MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD` | 两个独立密码；初始化后更改 `.env` 不会自动改数据库账号密码 |
| `DATABASE_URL` | `mysql+pymysql://ai_guide:CHANGE_ME@mysql:3306/ai_guide?charset=utf8mb4`，用户名、密码和库名与上面一致；hex 密码无需 URL 转义 |
| `REDIS_URL` | `redis://redis:6379/0` |
| `SESSION_SECRET` | 独立随机值，更新应用时保留；换值会影响现有访客会话 |
| `TEXT_*` / `VISION_*` | 对应 API 地址、密钥、实际可用模型；文本和视觉可以使用不同模型 |
| `PLACE_API_BASE_URL` / `PLACE_API_KEY` | 高德地址 `https://restapi.amap.com` 与 **Web 服务** Key |
| `VITE_AMAP_JS_KEY` | 高德 **Web 端 JS** Key，构建时进入浏览器代码；按应用域名限制来源 |
| `AMAP_JS_SECURITY_CODE` | JS 安全密钥，仅后端保存，由同源安全代理追加 |
| `SITE_ADDRESS` | 例如 `guide.example.com`，不带协议 |
| `PUBLIC_BASE_URL` | 例如 `https://guide.example.com` |
| Cookie 设置 | `SESSION_COOKIE_SECURE=true`、`AUTH_COOKIE_SECURE=true` |
| 邮件 | 真实发信使用 `EMAIL_BACKEND=smtp`；填写供应商要求的发件人、主机、端口、账号及授权凭据 |
| `EMAIL_DUMP_PATH` | 生产留空，禁止保存验证/重置令牌正文到文件 |

模型价格、区域、权限和参数支持以自己的供应商账号为准。仓库默认模型与单价是项目配置，不代表所有账号可用或最新报价。更换模型时同步调整 `TEXT_*_PRICE_PER_1K` / `VISION_*_PRICE_PER_1K`，单位为元/千 token。

当前 SMTP 实现支持普通 SMTP 加 STARTTLS（通常是 587 端口），不支持隐式 TLS 的 SMTP_SSL/465。选择供应商时确认协议；不要把 `STARTTLS=false` 当成启用 465 的方式。`console` 不发送邮件，而且当前可能返回发送成功，只适合开发排查。

高德两种 Key 不能互换。JS 安全密钥不能放到任何 `VITE_*` 变量里。地图 Key 修改后需重新构建 `web`；后端密钥修改后重建对应容器即可。

## 构建与启动

```bash
docker compose -f docker-compose.yml -f deploy/compose.small.yml config --quiet
docker compose -f docker-compose.yml -f deploy/compose.small.yml pull mysql redis
docker compose -f docker-compose.yml -f deploy/compose.small.yml build api
docker compose -f docker-compose.yml -f deploy/compose.small.yml build web
docker compose -f docker-compose.yml -f deploy/compose.small.yml up -d
docker compose -f docker-compose.yml -f deploy/compose.small.yml ps -a
```

低内存机器分开构建。`api`、`worker`、`migrate` 共用后端镜像；`migrate` 成功退出后 API 和 Worker 才启动。Caddy 等待 API 就绪后提供页面并申请证书。

```bash
curl -fsS https://guide.example.com/api/health/ready
docker stats --no-stream
free -h
```

将示例域名替换为自己的域名。健康检查确认 MySQL、Redis、迁移版本；不检查模型配额、地图 Key、SMTP 或 Worker 完成任务的能力。还需实际完成创建旅行、地图搜索、添加路线、一次 Agent 回复、刷新重开和登录。

## 更新

1. 保存上一版本源码和镜像标签，先完成数据库与媒体备份。
2. 将新源码放入部署目录，保留服务器 `.env`、数据卷和备份。若部署目录是 Git 克隆，使用明确的发布提交或标签更新。
3. 把 `.env` 的 `APP_VERSION` 改成新标签，再依次 `build api`、`build web`。
4. 有新迁移时，阅读迁移代码，在维护窗口停止 API/Worker，运行 `docker compose -f docker-compose.yml -f deploy/compose.small.yml run --rm migrate`。
5. 使用相同两个 `-f` 执行 `up -d`，验证健康检查和一条真实用户流程。

使用旧镜像回退前确认数据库仍兼容。不要仅改回 `APP_VERSION` 就假定数据模型已经回退；不要通过 `down -v` 更新应用。

`deploy/deploy.sh` 和 `rollback.sh` 面向已有镜像仓库的部署，不是源码首次安装入口。仓库 CI 默认检查和构建，不自动发布镜像或访问服务器。前端 JS Key 在构建期注入，公共无 Key 镜像不适合直接替换已配置地图的站点。

## 备份与恢复

备份包括 MySQL、照片持久卷以及版本与迁移记录；`.env` 单独加密保管。备份应复制到服务器之外。Redis 队列不代替 MySQL 备份。

仓库提供 `deploy/backup.sh` 和 `deploy/restore.sh`。它们会进入维护窗口停止写入，并生成/校验 manifest。脚本新增或更新后应先在独立环境恢复一次，再用于自己的正式数据。本项目的线上备份恢复演练尚未记录为通过。

小内存部署调用这些脚本时设置覆盖文件：

```bash
COMPOSE_OVERRIDE_FILE=deploy/compose.small.yml bash deploy/backup.sh --dry-run
COMPOSE_OVERRIDE_FILE=deploy/compose.small.yml bash deploy/backup.sh
```

恢复会覆盖目标库的表，先保留当前备份、明确目标。使用 `restore.sh --help` 查看参数；先 `--dry-run`，核对同一批次的 SQL、媒体和 manifest，正式恢复前需要人工确认。

## 排查

```bash
docker compose -f docker-compose.yml -f deploy/compose.small.yml logs --tail=80 api worker web
docker compose -f docker-compose.yml -f deploy/compose.small.yml ps -a
```

| 现象 | 先检查 |
|---|---|
| 首页打开但地图不可用 | JS Key 构建参数、域名限制、`AMAP_JS_SECURITY_CODE` 与 `/_AMapService` 代理 |
| 每次刷新变成新访客 | 实际地址是否 HTTPS、Cookie 是否发回、会话密钥是否变动 |
| 任务一直等待 | Worker 日志、Redis 连通性、运行租约与恢复进程 |
| Agent 失败 | 运行错误码、供应商权限和额度、模型参数、超时；不要只看 API 健康状态 |
| 发信显示成功却收不到 | 是否仍为 `console`；SMTP 协议、发件人和供应商记录 |
| 所有访客共享 IP 限额 | 代理来源与 `TRUSTED_PROXY_IPS` 是否匹配；当前按确切代理 IP 信任，地址变更需同步验证 |

公开日志前去除凭据、邮箱、Cookie、验证链接和私人旅行内容。不要公开 `docker compose config` 的完整输出，它会展开环境变量；使用 `config --quiet` 检查即可。
