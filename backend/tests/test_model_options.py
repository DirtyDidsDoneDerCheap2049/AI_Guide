"""Model allowlist, request dialects and task snapshots. No real provider calls."""
import json

import httpx
import pytest

from app.config import Settings, ConfigError
from app.services.model_options import profiles, select_model, settings_for_selection, ModelSelectionError
from app.agent.providers.http import OpenAICompatibleTextProvider
from app.agent.providers.base import ProviderInvalidResponse


def configured(**changes):
    return Settings(_env_file=None, text_api_key="test-only", **changes)


def test_profile_validation_and_secret_exclusion():
    with pytest.raises(ConfigError):
        profiles(configured(text_model_profiles='[{"secret":"do-not-echo"}]'))
    selection = select_model(configured(), None, True)
    assert "test-only" not in json.dumps(selection)
    assert selection["thinking"] is True
    with pytest.raises(ModelSelectionError, match="model_unavailable"):
        select_model(configured(), "user-injected-model", False)
    with pytest.raises(ModelSelectionError, match="thinking_not_supported"):
        select_model(configured(text_api_base_url="https://example.invalid/v1"), None, True)


def test_selection_is_independent_and_uses_profile_prices(monkeypatch):
    profile = dict(id="other", label="Other", model="other-model", base_url="https://example.invalid/v1",
                   api_key_env="OTHER_MODEL_KEY", thinking_mode="deepseek", prompt_price=0.1, completion_price=0.2)
    settings = configured(text_model_profiles=json.dumps([profile]))
    monkeypatch.setenv("OTHER_MODEL_KEY", "test-other-only")
    selection = select_model(settings, "other", True)
    resolved = settings_for_selection(settings, selection)
    assert resolved.text_model == "other-model"
    assert resolved.text_api_key == "test-other-only"
    assert resolved.text_prompt_price_per_1k == 0.1
    assert settings.text_model != resolved.text_model
    assert "test-other-only" not in json.dumps(selection)


@pytest.mark.parametrize("mode,enabled,expected", [
    ("none", False, {}),
    ("qwen", True, {"enable_thinking": True, "thinking_budget": 4096}),
    ("qwen", False, {"enable_thinking": False}),
    ("deepseek", True, {"thinking": {"type": "enabled"}}),
    ("deepseek", False, {"thinking": {"type": "disabled"}}),
    ("reasoning_effort", True, {"reasoning_effort": "high"}),
    ("reasoning_effort", False, {"reasoning_effort": "none"}),
])
def test_protocol_dialects(monkeypatch, mode, enabled, expected):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}}], "usage": {}})
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = configured(text_api_base_url="https://example.invalid/v1", text_thinking_mode=mode, text_enable_thinking=enabled)
    result = OpenAICompatibleTextProvider(settings).answer_question(messages=[{"role": "user", "content": "JSON please"}])
    assert result.text == '{"answer":"ok"}'
    payload = requests[0]
    actual = {key: payload[key] for key in ("enable_thinking", "thinking_budget", "thinking", "reasoning_effort") if key in payload}
    assert actual == expected
    assert ("temperature" in payload) is not enabled


def test_empty_answer_is_failure(monkeypatch):
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"choices": [{"message": {"content": None, "reasoning_content": "not an answer"}}]})
    ), **kw))
    with pytest.raises(ProviderInvalidResponse):
        OpenAICompatibleTextProvider(configured()).answer_question(messages=[])


@pytest.mark.parametrize("level,budget", [("off", 0), ("low", 1024), ("medium", 4096), ("high", 8192)])
def test_qwen_effort_levels_are_real_budgets(level, budget):
    settings = configured()
    selection = select_model(settings, None, None, level)
    assert selection['thinking_level'] == level
    assert selection['thinking_budget'] == budget
    resolved = settings_for_selection(settings, selection)
    assert resolved.text_enable_thinking == (level != 'off')
    if budget:
        assert resolved.thinking_budget == budget


def test_unsupported_level_and_inconsistent_toggle_rejected():
    settings = configured(text_thinking_mode='deepseek')
    with pytest.raises(ModelSelectionError, match='thinking_level_not_supported'):
        select_model(settings, None, None, 'low')
    with pytest.raises(ModelSelectionError, match='thinking_selection_conflict'):
        select_model(settings, None, False, 'high')


@pytest.mark.parametrize("mode,level,value", [
    ('qwen', 'low', 1024), ('qwen', 'medium', 4096), ('qwen', 'high', 8192),
    ('reasoning_effort', 'low', 'low'), ('reasoning_effort', 'medium', 'medium'), ('reasoning_effort', 'high', 'high'),
])
def test_selected_level_reaches_http_payload(monkeypatch, mode, level, value):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"answer":"ok"}'}}]})
    client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    settings = configured(text_thinking_mode=mode)
    selection = select_model(settings, None, None, level)
    provider = OpenAICompatibleTextProvider(settings_for_selection(settings, selection))
    provider.reasoning_effort = selection['thinking_level']
    provider.answer_question(messages=[{'role': 'user', 'content': 'JSON please'}])
    assert requests[0]['thinking_budget' if mode == 'qwen' else 'reasoning_effort'] == value
    if mode == 'qwen':
        assert 'response_format' not in requests[0]
