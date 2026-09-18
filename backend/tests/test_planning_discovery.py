"""User-visible planning and conversation management against real MySQL/Redis."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.orchestrator import execute_run, _answer_context
from app.agent.providers.base import ProviderTimeout, PlaceResult, PlaceSearchResult
from app.models import Message, AgentRun
from app.services.discovery import provider_photos, resources_for
from tests.conftest import create_project

pytestmark = pytest.mark.integration


def conversation(client, session_factory, settings, providers):
    project = create_project(client)
    base = f"/api/v1/projects/{project['id']}/messages"
    posted = client.post(base, json={'content': '杭州玩三天'}).json()
    assert execute_run(session_factory, posted['run']['id'], providers=providers, settings=settings) == 'SUCCEEDED'
    return project, base, client.get(base).json()['messages']


def test_edit_delete_search_and_context(client, session_factory, settings, providers):
    project, base, rows = conversation(client, session_factory, settings, providers)
    edited = client.patch(f"{base}/{rows[0]['id']}", json={'content': '杭州改成两天', 'expected_version': 1})
    assert edited.status_code == 200 and edited.json()['version'] == 2
    assert edited.json()['edited_at']
    assert client.patch(f"{base}/{rows[0]['id']}", json={'content': '覆盖', 'expected_version': 1}).status_code == 409
    latest = client.get(base).json()['messages']
    assert latest[1]['stale'] and latest[1]['version'] == 2
    assert client.get(base, params={'q': '两天'}).json()['messages'][0]['id'] == rows[0]['id']
    assert client.get(base, params={'q': '%'}).json()['messages'] == []
    with session_factory() as db:
        run = db.get(AgentRun, rows[0]['run_id'])
        context = _answer_context(db, run, settings)
        assert context['history'] == [{'role': 'user', 'content': '杭州改成两天'}]
    removed = client.delete(f"{base}/{rows[0]['id']}?expected_version=2")
    assert removed.status_code == 200 and removed.json()['deleted_at']
    assert removed.json()['content'] == ''
    assert client.get(base, params={'q': '两天'}).json()['messages'] == []
    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert snapshot['messages'][0]['deleted_at']
    with session_factory() as db:
        assert db.get(Message, rows[0]['id']).content == '杭州改成两天'  # recoverable soft delete
        assert _answer_context(db, db.get(AgentRun, rows[0]['run_id']), settings)['history'] == []


def test_management_permissions_and_active_run(client, app, session_factory, settings, providers):
    _, base, rows = conversation(client, session_factory, settings, providers)
    with TestClient(app) as stranger:
        assert stranger.get(base).status_code == 404
        assert stranger.patch(f"{base}/{rows[0]['id']}", json={'content': '越权', 'expected_version': 1}).status_code == 404
        assert stranger.delete(f"{base}/{rows[0]['id']}?expected_version=1").status_code == 404
    assert client.patch(f"{base}/{rows[1]['id']}", json={'content': '伪造模型回答', 'expected_version': 1}).status_code == 403
    assert client.patch(f"{base}/{rows[0]['id']}", json={'content': '   ', 'expected_version': 1}).status_code == 422
    posted = client.post(base, json={'content': '再问一次'}).json()
    blocked = client.delete(f"{base}/{rows[0]['id']}?expected_version=1")
    assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'message_run_active'
    assert execute_run(session_factory, posted['run']['id'], providers=providers, settings=settings) == 'SUCCEEDED'
    assert client.delete(f"{base}/{rows[1]['id']}?expected_version=1").status_code == 200


def test_search_finds_messages_before_snapshot_window(client, session_factory):
    project = create_project(client)
    with session_factory() as db:
        db.add_all([Message(project_id=project['id'], seq=i, role='user', content=f'第{i}条内容', status='READY') for i in range(1, 66)])
        db.commit()
    assert len(client.get(f"/api/v1/projects/{project['id']}").json()['messages']) == 30
    found = client.get(f"/api/v1/projects/{project['id']}/messages", params={'q': '第1条'}).json()['messages']
    assert len(found) == 1 and found[0]['seq'] == 1
    older = client.get(f"/api/v1/projects/{project['id']}/messages", params={'before_seq': 36, 'limit': 20}).json()['messages']
    assert [row['seq'] for row in older] == list(range(16, 36))


def test_text_cancel_retry_and_edit_after_stop(client):
    project = create_project(client)
    base = f"/api/v1/projects/{project['id']}/messages"
    posted = client.post(base, json={'content': '我要去杭州'}).json()
    run_id = posted['run']['id']
    assert client.post(f'/api/v1/runs/{run_id}/cancel').status_code == 200
    retried = client.post(f'/api/v1/runs/{run_id}/retry')
    assert retried.status_code == 202 and retried.json()['media_asset_id'] is None
    retry_id = retried.json()['run_id']
    assert retry_id != run_id
    assert client.post(f'/api/v1/runs/{run_id}/retry').json()['run_id'] == retry_id
    assert client.post(f'/api/v1/runs/{retry_id}/cancel').status_code == 200
    assert client.patch(f"{base}/{posted['message']['id']}", json={'content': '我要去上海', 'expected_version': 1}).status_code == 200
    rejected = client.post(f'/api/v1/runs/{run_id}/retry')
    assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'message_changed'


def test_discovery_cache_photos_and_failure(client, monkeypatch):
    calls = []
    class Places:
        name = 'test-amap'
        def search(self, **kwargs):
            calls.append(kwargs)
            if kwargs['name'] == '失败':
                raise ProviderTimeout('controlled')
            return PlaceSearchResult(candidates=[PlaceResult(name='西湖', address='杭州', region='杭州', latitude=30, longitude=120, provider='amap', provider_place_id='test-id', raw={'photos': [{'url': 'http://store.is.autonavi.com/showpic/test.jpg'}]})])
    monkeypatch.setattr('app.api.discovery.build_provider_set', lambda settings: SimpleNamespace(place=Places(), mode='mock'))
    response = client.get('/api/v1/discovery', params={'city': '杭州'})
    assert response.status_code == 200
    first = response.json()
    assert first['places'][0]['photos'][0]['url'].startswith('https://') and first['fixture']
    second = client.get('/api/v1/discovery', params={'city': '杭州'}).json()
    assert second['cached'] and len(calls) == 3
    failure = client.get('/api/v1/discovery', params={'city': '杭州', 'q': '失败'}).json()
    assert failure['error'] and not failure['places'] and failure['resources']
    assert client.get('/api/v1/discovery', params={'city': '  '}).status_code == 422


def test_resource_links_and_photo_hosts():
    assert any('bilibili.com/video/' in row['url'] for row in resources_for('杭州'))
    links = resources_for('苏州', '园林')
    assert all(row['is_search'] for row in links)
    assert all('抖音' not in row['source'] for row in links)
    # Placeholder credentials exercise userinfo rejection without resembling a real secret.
    assert provider_photos({'photos': [{'url': 'javascript:alert(1)'}, {'url': 'http://127.0.0.1/a'}, {'url': 'https://amap.com.evil.test/a'}, {'url': 'https://placeholder-user:placeholder-password@amap.com/a'}]}) == []


def test_discovery_limits_and_redis_outage(client, monkeypatch):
    import redis
    class Cache:
        def get(self, key): return None
        def eval(self, *args): return 201
        def close(self): pass
    monkeypatch.setattr('app.api.discovery.redis.Redis.from_url', lambda *a, **kw: Cache())
    assert client.get('/api/v1/discovery?city=杭州').status_code == 429
    class Down(Cache):
        def get(self, key): raise redis.ConnectionError()
    monkeypatch.setattr('app.api.discovery.redis.Redis.from_url', lambda *a, **kw: Down())
    assert client.get('/api/v1/discovery?city=杭州').status_code == 503


def test_message_migration_preserves_content(client, engine):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from tests.conftest import BACKEND_DIR
    project = create_project(client)
    posted = client.post(f"/api/v1/projects/{project['id']}/messages", json={'content': '迁移保留的内容'}).json()
    config = Config(str(BACKEND_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(BACKEND_DIR / 'migrations'))
    try:
        command.downgrade(config, 'd2e3f4a5b6c7')
        with engine.connect() as conn:
            assert conn.execute(text('SELECT content FROM messages WHERE id=:id'), {'id': posted['message']['id']}).scalar_one() == '迁移保留的内容'
    finally:
        command.upgrade(config, 'head')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version, stale, deleted_at FROM messages WHERE id=:id'), {'id': posted['message']['id']}).one() == (1, 0, None)
