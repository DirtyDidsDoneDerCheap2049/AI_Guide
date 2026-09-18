# AI-Guide v0.1 后端镜像（api / worker / migrate 共用）
#
# 构建上下文：仓库根目录（见 docker-compose.yml 的 build.context: .）
#   docker build -f backend/Dockerfile -t ai-guide-api:0.1.0 .
# 依赖排除规则在仓库根 .dockerignore（.env、work/、backend/tests/、docs/ 等不进镜像）。
#
# 依赖优先级：backend/requirements.lock.txt > backend/requirements.txt
# 开发与测试依赖见 docs/DEMO-STARTUP.md；运行镜像使用锁文件。

FROM python:3.13.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

# 运行期只依赖 wheel 包（cryptography / pillow 在 PyPI 提供 manylinux + cp313 wheel），
# 因此不安装 gcc / libmysqlclient 等编译工具链。
WORKDIR /app

# 先装依赖再拷代码，代码改动不会让依赖层缓存失效。
COPY backend/requirements*.txt /app/
RUN set -eux; \
    if [ -f /app/requirements.lock.txt ]; then \
        echo "install from locked requirements: requirements.lock.txt"; \
        pip install --no-cache-dir -r /app/requirements.lock.txt; \
    else \
        echo "install from requirements.txt (no lock file present)"; \
        pip install --no-cache-dir -r /app/requirements.txt; \
    fi

# 只拷贝运行进程真正需要的代码：应用包、迁移脚本、Alembic 配置。
COPY backend/app /app/app
COPY backend/migrations /app/migrations
COPY backend/alembic.ini /app/alembic.ini

# 非 root 运行；媒体目录与容器内统一路径一致（compose 里 MEDIA_ROOT=/var/lib/ai-guide/media）。
RUN set -eux; \
    groupadd --system --gid 10001 appuser; \
    useradd --system --uid 10001 --gid appuser --home-dir /home/appuser --create-home appuser; \
    mkdir -p /var/lib/ai-guide/media; \
    chown -R appuser:appuser /app /var/lib/ai-guide /home/appuser

USER appuser

EXPOSE 8000

# 生产启动方式：不带 --reload，也不在启动时建表（迁移由 compose 的一次性 migrate 服务执行）。
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
