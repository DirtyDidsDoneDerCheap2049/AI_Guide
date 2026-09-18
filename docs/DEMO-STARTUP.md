# 本地开发与测试

服务器安装见 [部署指南](DEPLOYMENT.md)。这里从新克隆的源码开始，不依赖维护者机器上的启动脚本或运行数据。

## 环境

准备 Python 3.13、Node 22（与 `web/Dockerfile` 对齐）、MySQL 8 和 Redis 7。先创建自己的开发库和数据库账号，编码使用 `utf8mb4`。MySQL 与 Redis 的安装方式可自行选择，不要连接生产实例。

Windows PowerShell，在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
Set-Location web
npm ci
Set-Location ..
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Linux/macOS 对应使用 `python3 -m venv .venv` 与 `.venv/bin/python`；其余 Python 模块入口一致。

## 配置

编辑根目录 `.env`：

| 项目 | 本地要求 |
|---|---|
| `APP_ENV` | `local` |
| `DATABASE_URL` | 指向刚创建的开发库和自己的数据库账号 |
| `REDIS_URL` | 指向本机开发 Redis；不要与测试 DB 共用 |
| `SESSION_SECRET` | 随机长字符串 |
| `MEDIA_ROOT` | 建议使用绝对路径，存储开发图片 |
| `PROVIDER_MODE` | `mock` 用于 fixture 流程；`real` 调用真实模型和地图服务 |
| `SESSION_COOKIE_SECURE` / `AUTH_COOKIE_SECURE` | 本机 HTTP 使用 `false` |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:5173` |
| `EMAIL_BACKEND` / `EMAIL_DUMP_PATH` | `console`；如需调试邮件，文件放入忽略的 `work/`，不要公开令牌 |

real 模式填写 `TEXT_*`、`VISION_*`、`PLACE_*` 与 `AMAP_JS_SECURITY_CODE`。在 `web/.env.local` 中填写 `VITE_AMAP_JS_KEY=CHANGE_ME`，把占位值换成自己的高德 JS Key。安全密钥只在根 `.env`，不能放到 `VITE_*`。

mock 模式的模型任务使用带 fixture 标记的结果，不代表真实内容质量。地图 JS、地点发现等直接调用外部服务的页面不会因为启用 fixture 就获得完整离线地图；真实地图仍需自己的配置。

应用读取根 `.env` 和可选 `backend/.env`，进程环境变量优先。排障时检查是否有环境变量覆盖文件，但不要把值复制到公开日志。

## 启动

在确认连接的是开发库后执行迁移，随后启动 API：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m uvicorn app.asgi:app --host 127.0.0.1 --port 8000
```

另开终端，从根目录启动 Worker：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.worker.cli worker --processes 1 --threads 2
```

再开终端启动前端：

```powershell
Set-Location web
npm run dev -- --host 127.0.0.1
```

打开 `http://127.0.0.1:5173`。Vite 将 API 和高德安全代理转发到本机后端。不要用文件管理器直接打开 `index.html`。

## 自动检查

前端：

```powershell
Set-Location web
npm run format:check
npm run build
```

仓库工具检查，在根目录：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s scripts/tests -v
.\.venv\Scripts\python.exe scripts/secret_scan.py --git-worktree --fail-on-credentials --quiet
.\.venv\Scripts\python.exe deploy/check-env-parity.py
```

后端测试必须使用独立测试库和 Redis 测试 DB。**`conftest.py` 会删除测试库中的表并清空 Redis DB**，不能使用开发数据或生产数据的连接。以下仅是专用测试资源的示例：

```powershell
$env:AI_GUIDE_TEST_DATABASE_URL = 'mysql+pymysql://test_user:CHANGE_ME@127.0.0.1:3306/ai_guide_test?charset=utf8mb4'
$env:AI_GUIDE_TEST_REDIS_URL = 'redis://127.0.0.1:6379/1'
Set-Location backend
..\.venv\Scripts\python.exe -m pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
```

自动测试不使用真实供应商密钥。真实接入测试会创建旅行/照片并可能产生调用费用，参见 `backend/scripts/real_route_probe.py`、`real_ops_probe.py` 与 `smoke.py` 的 `--help`。只对自己的测试环境执行。

## 手工检查

选择城市、创建旅行、搜索地点、加入路线、切换步行/驾车，发送一条问题并等待结果。修改问题、刷新、再次打开旅行，确认消息与地点仍在。再用两个标签页修改同一路线，检查冲突提示。

分别检查 API 请求失败和模型任务失败；页面应说明实际状态，保留用户已输入的内容。邮件需要真实 SMTP 才能验收投递。
