"""集中配置：环境变量 -> 强类型 Settings。

安全约定（S0 要求）：
- 配置缺失或非法时的错误消息只包含变量名，绝不包含变量值。
- ``hide_input_in_errors=True`` 保证 pydantic 自身的报错也不会带出输入值。
- ``PROVIDER_MODE=mock`` 只允许在 local/test 使用，生产环境必须 real。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_ENV_FILES = (str(REPO_ROOT / ".env"), str(BACKEND_DIR / ".env"))

REQUIRED_ALWAYS = ("DATABASE_URL", "SESSION_SECRET")


class ConfigError(RuntimeError):
    """配置错误。``names`` 只包含变量名，便于安全日志与测试断言。"""

    def __init__(self, names: list[str], reason: str = "invalid_configuration") -> None:
        self.names = sorted({name.upper() for name in names})
        self.reason = reason
        super().__init__(f"{reason}: " + ", ".join(self.names))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # --- 运行环境 ---
    app_env: Literal["local", "test", "production"] = "local"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # --- 基础设施 ---
    database_url: str = Field(default="", description="MySQL connection URL supplied through DATABASE_URL")
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_queue_name: str = "ai_guide_runs"

    # --- 匿名会话 ---
    session_secret: str = Field(default="", description="签名 Cookie 用密钥")
    session_cookie_name: str = "ai_guide_session"
    session_ttl_seconds: int = 60 * 60 * 24 * 7
    session_cookie_secure: bool | None = None
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # --- 媒体 ---
    media_root: Path = BACKEND_DIR / "var" / "media"
    media_max_bytes: int = 8 * 1024 * 1024
    media_max_per_project: int = 3
    media_allowed_mime: str = "image/jpeg,image/png,image/webp"

    # --- Provider ---
    provider_mode: Literal["mock", "real"] = "mock"
    provider_timeout_seconds: float = 45.0
    vision_api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_api_key: str = ""
    # 当前项目的视觉模型配置；实际可用模型、参数和价格以供应商账号为准。
    vision_model: str = "qwen3.8-flash"
    text_api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    text_api_key: str = ""
    text_model: str = "qwen3.7-plus"
    place_api_base_url: str = "https://restapi.amap.com"
    place_api_key: str = ""
    # B4：一次地点搜索取回多少候选（用于同名/跨城消歧），1–20。
    place_search_limit: int = 5
    # D3-b：驾车策略（高德 strategy），默认 0=速度优先；步行接口不需要该参数
    route_driving_strategy: int = 0
    # ---- D2 账户体系 ----
    # 特性开关：关闭后认证接口返回 503 auth_disabled（已创建的账号与数据不受影响）
    auth_enabled: bool = True
    # 登录 Cookie：本地 http 开发需要 AUTH_COOKIE_SECURE=false，生产会被强制为 true
    auth_cookie_name: str = "ai_guide_auth"
    auth_cookie_secure: bool = False
    auth_session_days: int = 30
    auth_token_ttl_minutes: int = 60
    # Argon2id 参数（OWASP 建议档：19 MiB / 2 次迭代 / 1 并行）
    argon2_time_cost: int = 2
    argon2_memory_cost: int = 19456
    argon2_parallelism: int = 1
    # 认证接口限流（按 IP + 邮箱）
    auth_rate_limit_window_seconds: int = 900
    auth_login_attempt_limit: int = 10
    auth_register_attempt_limit: int = 5
    auth_email_attempt_limit: int = 5
    # 邮件：console（只写日志/可选落盘，不联网）/ smtp
    email_backend: str = "console"
    email_from: str = "no-reply@example.invalid"
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_password: str = ""
    email_smtp_starttls: bool = True
    # 仅开发/测试：把邮件正文追加写入该文件，便于取出验证/重置令牌
    email_dump_path: str = ""
    # 邮件里的链接前缀（生产填真实域名）
    public_base_url: str = "http://127.0.0.1:8000"
    # ---- 高德 JS API 2.0 安全代理 ----
    # 浏览器只用网页端 JS Key（可在高德控制台按来源域名限制）；JS 安全密钥只留在服务端，
    # 由 /_AMapService 代理在转发时追加 jscode，前端不持有该值。
    amap_js_security_code: str = ""
    amap_js_proxy_path: str = "/_AMapService"
    # 白名单：只允许转发到这些主机，禁止任意 URL 转发。
    amap_js_proxy_hosts: str = "restapi.amap.com,webapi.amap.com,vdata.amap.com"
    amap_js_proxy_timeout_seconds: float = 10.0
    # 仅用于本地协议桩测试；生产保持 https。
    amap_js_proxy_scheme: str = "https"
    # Qwen3.5/3.6/3.7/3.8 与 qwen3-vl-* 是混合思考模型：
    # 思考模式默认开启，识别这类结构化抽取任务并不需要，关闭可显著降低延迟与输出 token。
    vision_enable_thinking: bool = False
    text_enable_thinking: bool = False
    # 思考模式下的最大推理 token（仅在 enable_thinking=true 时发送）。
    thinking_budget: int = 4096
    # 视觉分辨率：默认按服务端策略压缩；打开高分辨率或放宽 max_pixels 可保留招牌/小字细节，
    # 代价是更多视觉 token。max_pixels=0 表示不发送该参数（使用服务端默认 2621440）。
    vision_high_resolution_images: bool = False
    vision_max_pixels: int = 0

    # --- Agent 预算与租约 ---
    run_max_steps: int = 12
    run_max_tool_calls: int = 6
    run_max_tokens: int = 12000
    # 运行费用上限。单位与 MODEL_*_PRICE_PER_1K 一致（默认人民币元）。
    run_max_cost: float = 0.5
    run_lease_seconds: int = 120
    run_step_max_attempts: int = 2
    run_json_repair_attempts: int = 2
    # 单价按实际供应商账单填写；默认值是项目预算配置，不代表最新报价。
    # 注意：数据库列名 history 沿用 *_usd，单位由这里决定，字段名重命名留到 v0.2 迁移处理。
    # 视觉与文本是不同模型，必须分开计价（B3）。单位与厂商账单一致（元/千 token）。
    model_prompt_price_per_1k: float = 0.0008
    model_completion_price_per_1k: float = 0.0027
    vision_prompt_price_per_1k: float = 0.0008
    vision_completion_price_per_1k: float = 0.0027
    text_prompt_price_per_1k: float = 0.002
    text_completion_price_per_1k: float = 0.008
    place_call_price: float = 0.0
    # 每次模型调用的预算预留（token）。预留是防超额的近似；实际用量以结算为准。
    run_reserve_tokens_per_call: int = 2000
    # 价格版本：计价口径变化时必须递增，历史调用记录保留旧版本号。
    price_version: str = "v1"
    # 费用币种（与厂商账单一致）
    cost_currency: str = "CNY"

    # --- 公开 Demo 额度 ---
    demo_session_run_limit: int = 6
    demo_daily_run_limit: int = 200
    # 入口 IP 维度的每日新任务上限（B6）。同一 IP 的并发请求由数据库额度桶保证不超额。
    demo_ip_daily_limit: int = 30
    # 可信反向代理地址（逗号分隔）。只有来自这些地址的请求才信任 X-Forwarded-For；
    # 默认空 = 永不信任转发头，直接使用 TCP 对端地址。
    trusted_proxy_ips: str = ""

    # --- SSE ---
    sse_heartbeat_seconds: float = 15.0
    sse_poll_interval_seconds: float = 0.5
    sse_max_stream_seconds: float = 900.0

    # --- 其他 ---
    allowed_origins: str = ""
    worker_recovery_interval_seconds: int = 60
    # 仅测试使用：为 fixture Provider 注入故障行为，例如 "place=timeout,text=invalid_json"。
    # 生产环境禁止使用 fixture（见 providers/__init__.py）。
    fixture_behaviors: str = ""

    # ------------------------------------------------------------------ helpers
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def trusted_proxy_set(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.trusted_proxy_ips.split(",") if part.strip())

    @property
    def amap_proxy_host_list(self) -> list[str]:
        return [part.strip().lower() for part in self.amap_js_proxy_hosts.split(",") if part.strip()]

    @property
    def allowed_mime_set(self) -> frozenset[str]:
        return frozenset(part.strip().lower() for part in self.media_allowed_mime.split(",") if part.strip())

    @property
    def cors_origins(self) -> list[str]:
        return [part.strip() for part in self.allowed_origins.split(",") if part.strip()]

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    @property
    def auth_cookie_secure_effective(self) -> bool:
        """登录 Cookie 的 Secure 属性：显式配置优先，否则生产默认开启。"""
        if self.is_production and not self.auth_cookie_secure:
            # 生产环境不允许把登录 Cookie 降级为裸 http；显式配置为 false 时按"未设置"处理
            return True
        return self.auth_cookie_secure or self.is_production

    def missing_runtime_config(self) -> list[str]:
        """返回缺失的必需变量名（只有变量名）。"""
        missing: list[str] = []
        if not self.database_url.strip():
            missing.append("DATABASE_URL")
        if not self.session_secret.strip():
            missing.append("SESSION_SECRET")
        return missing

    def missing_provider_config(self) -> list[str]:
        """real 模式下缺失的 Provider 变量名（只有变量名）。"""
        if self.provider_mode != "real":
            return []
        missing: list[str] = []
        if not self.vision_api_key.strip():
            missing.append("VISION_API_KEY")
        if not self.text_api_key.strip():
            missing.append("TEXT_API_KEY")
        if not self.place_api_key.strip():
            missing.append("PLACE_API_KEY")
        return missing

    def public_summary(self) -> dict[str, Any]:
        """可安全写入日志或健康检查的配置摘要，不含任何密钥值。"""
        return {
            "app_env": self.app_env,
            "app_version": self.app_version,
            "provider_mode": self.provider_mode,
            "vision_model": self.vision_model,
            "text_model": self.text_model,
            "media_max_bytes": self.media_max_bytes,
            "media_max_per_project": self.media_max_per_project,
            "run_max_steps": self.run_max_steps,
            "run_max_tool_calls": self.run_max_tool_calls,
            "run_max_tokens": self.run_max_tokens,
        }


def _validation_error_names(exc: ValidationError) -> list[str]:
    names: list[str] = []
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", ()) if part not in ("__root__",)]
        name = loc[-1] if loc else "unknown"
        names.append(name)
    return names


def load_settings(**overrides: Any) -> Settings:
    """加载配置；失败时抛 ``ConfigError``，消息只含变量名。"""
    env_file = overrides.pop("_env_file", DEFAULT_ENV_FILES)
    try:
        settings = Settings(_env_file=env_file, **overrides)
    except ValidationError as exc:
        raise ConfigError(_validation_error_names(exc), "invalid_configuration") from None

    missing = settings.missing_runtime_config()
    if missing:
        raise ConfigError(missing, "missing_configuration")

    if settings.is_production:
        if settings.provider_mode != "real":
            raise ConfigError(["PROVIDER_MODE"], "invalid_configuration_for_production")
        provider_missing = settings.missing_provider_config()
        if provider_missing:
            raise ConfigError(provider_missing, "missing_configuration")

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内缓存的配置。测试可用 ``reset_settings_cache()`` 清理。"""
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
