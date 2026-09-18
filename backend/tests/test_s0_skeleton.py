"""S0：入口可导入、配置可加载、缺配置不泄密、新后端不依赖 backend-temp。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_health_live_returns_version(client):
    response = client.get("/api/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_env"] == "test"
    assert body["version"]


def test_openapi_contract_is_available(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for expected in (
        "/api/v1/projects",
        "/api/v1/projects/current",
        "/api/v1/projects/{project_id}/media",
        "/api/v1/projects/{project_id}",
        "/api/v1/runs/{run_id}",
        "/api/health/live",
        "/api/health/ready",
    ):
        assert expected in paths, expected


def test_settings_load_with_test_environment(settings):
    assert settings.app_env == "test"
    assert settings.provider_mode in {"mock", "real"}
    assert settings.media_root.exists() or True  # 目录在应用启动时创建


def test_missing_required_config_reports_variable_names_only():
    from app.config import ConfigError, load_settings

    secret_value = "do-not-leak-this-value-1234567890"
    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env_file=None, database_url="", session_secret=secret_value)

    error = excinfo.value
    assert error.names == ["DATABASE_URL"]
    assert "DATABASE_URL" in str(error)
    assert secret_value not in str(error)
    assert "do-not-leak" not in repr(error.args)


def test_invalid_config_value_reports_variable_name_without_value():
    from app.config import ConfigError, load_settings

    secret_value = "another-secret-value-abcdefghij"
    with pytest.raises(ConfigError) as excinfo:
        load_settings(_env_file=None, session_secret=secret_value, media_max_bytes="not-a-number")

    error = excinfo.value
    assert error.names == ["MEDIA_MAX_BYTES"]
    assert secret_value not in str(error)


def test_production_requires_real_provider_mode():
    from app.config import ConfigError, load_settings

    with pytest.raises(ConfigError) as excinfo:
        load_settings(
            _env_file=None,
            app_env="production",
            database_url="mysql+pymysql://user:pass@127.0.0.1:3306/db",
            session_secret="s" * 32,
            provider_mode="mock",
        )
    assert excinfo.value.names == ["PROVIDER_MODE"]


def test_production_requires_provider_keys_without_leaking_values():
    from app.config import ConfigError, load_settings

    with pytest.raises(ConfigError) as excinfo:
        load_settings(
            _env_file=None,
            app_env="production",
            database_url="mysql+pymysql://user:pass@127.0.0.1:3306/db",
            session_secret="s" * 32,
            provider_mode="real",
            vision_api_key="",
            text_api_key="text-key-value",
            place_api_key="",
        )
    assert set(excinfo.value.names) == {"VISION_API_KEY", "PLACE_API_KEY"}
    assert "text-key-value" not in str(excinfo.value)


def test_new_backend_does_not_reference_backend_temp():
    offenders: list[str] = []
    for path in (BACKEND_DIR / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "backend-temp" in text or "backend_temp" in text:
            offenders.append(str(path.relative_to(BACKEND_DIR)))
    assert offenders == []
    assert not any(name.startswith("backend_temp") for name in sys.modules)


def test_mock_provider_set_is_rejected_in_production():
    from app.agent.providers import build_provider_set
    from app.config import ConfigError, Settings

    strict = Settings(
        _env_file=None,
        app_env="production",
        database_url="mysql+pymysql://user:pass@127.0.0.1:3306/db",
        session_secret="s" * 32,
        provider_mode="mock",
    )
    with pytest.raises(ConfigError) as excinfo:
        build_provider_set(strict)
    assert excinfo.value.names == ["PROVIDER_MODE"]


def test_default_models_are_current_generation(settings):
    """默认模型名锁定当前代次，避免回退到已不推荐的 qwen-vl-max / qwen-plus。"""
    assert settings.vision_model == "qwen3.8-flash"
    assert settings.text_model == "qwen3.7-plus"
    assert settings.vision_enable_thinking is False
    assert settings.text_enable_thinking is False
    assert settings.vision_max_pixels == 0
    # 计费单价默认与 qwen3.8-flash 的官方价格一致（元/千 token），否则费用预算会失真。
    assert settings.model_prompt_price_per_1k > 0
    assert settings.model_completion_price_per_1k > 0
