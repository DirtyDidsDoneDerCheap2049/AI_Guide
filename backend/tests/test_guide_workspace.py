"""Guide workspace: real persistence, controlled provider responses."""
import json

import pytest
from sqlalchemy import select
from app.agent.orchestrator import execute_run
from app.agent.providers.base import ModelResult, ProviderSet, ProviderTimeout
from app.agent.guide_actions import apply_route_action
from app.models import Message, RouteDraft, InvocationAttempt
from tests.conftest import create_project
from tests.test_d3_routes import STOPS

pytestmark = pytest.mark.integration


class ScriptedText:
    name = 'fixture-text'

    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def answer_question(self, *, messages):
        self.prompts.append(messages)
        return ModelResult(text=json.dumps({'answer': '[FIXTURE] 导游建议', **next(self.responses)}, ensure_ascii=False), model='fixture-text', prompt_tokens=20, completion_tokens=20)


def run_question(client, project, session_factory, settings, providers, text):
    response = client.post(f"/api/v1/projects/{project['id']}/messages", json={'content': '请帮我整理路线'})
    assert response.status_code == 201
    run_id = response.json()['run']['id']
    controlled = ProviderSet(vision=providers.vision, text=text, place=providers.place, mode='mock')
    assert execute_run(session_factory, run_id, providers=controlled, settings=settings) == 'SUCCEEDED'
    with session_factory() as db:
        reply = db.execute(select(Message).where(Message.run_id == run_id, Message.role == 'assistant')).scalar_one()
        return run_id, reply.attachments


def test_search_cards_survive_reload(client, session_factory, settings, providers):
    project = create_project(client)
    text = ScriptedText([{'search_queries': ['西湖']}, {'followups': ['还有哪些地方？']}])
    run_id, attachment = run_question(client, project, session_factory, settings, providers, text)
    assert attachment['places'] and attachment['places'][0]['fixture'] is True
    assert len(text.prompts) == 2
    assert 'place_search' in text.prompts[1][-1]['content']
    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert snapshot['messages'][-1]['attachments'] == attachment
    with session_factory() as db:
        attempts = db.execute(select(InvocationAttempt).where(InvocationAttempt.run_id == run_id)).scalars().all()
        assert len(attempts) == 3
        assert all(a.status == 'SUCCEEDED' for a in attempts)


def test_search_failure_stays_visible_without_fake_places(client, session_factory, settings, providers):
    class FailingPlace:
        name = 'fixture-place'
        def search(self, **kwargs):
            raise ProviderTimeout('controlled test failure')
    controlled = ProviderSet(vision=providers.vision, text=providers.text, place=FailingPlace(), mode='mock')
    project = create_project(client)
    text = ScriptedText([{'search_queries': ['西湖']}, {'uncertainty': '地点查询失败，不能核实地址。'}])
    _, attachment = run_question(client, project, session_factory, settings, controlled, text)
    assert attachment['places'] == []
    assert attachment['search_errors']


def test_agent_edit_uses_current_route_and_persists_atomically(client, session_factory, settings, providers):
    project = create_project(client)
    saved = client.post(f"/api/v1/projects/{project['id']}/saved-places", json={'name': '我收藏的地方', 'region': '杭州'}).json()
    response = client.post(f"/api/v1/projects/{project['id']}/routes", json={'name': '半天路线', 'mode': 'driving', 'stops': STOPS})
    assert response.status_code == 201, response.text
    draft = response.json()
    text = ScriptedText([{'route_action': {'operation': 'update', 'route_id': draft['id'], 'expected_version': 1, 'stop_indices': [0, 2], 'mode': 'driving'}}])
    _, attachment = run_question(client, project, session_factory, settings, providers, text)
    assert saved['id'] in text.prompts[0][-1]['content']
    assert draft['id'] in text.prompts[0][-1]['content']
    assert attachment['route_change']['ok']
    with session_factory() as db:
        edited = db.get(RouteDraft, draft['id'])
        assert [p['name'] for p in edited.stops] == ['起点', '终点']
        assert edited.input_version == 2 and edited.mode == 'driving'


def test_agent_route_edit_rejects_newer_version_and_other_workspace(client, session_factory):
    project = create_project(client)
    other = create_project(client)
    response = client.post(f"/api/v1/projects/{project['id']}/routes", json={'name': '路线', 'stops': STOPS})
    draft = response.json()
    known = [{'id': draft['id'], 'version': 1}]
    action = {'operation': 'update', 'route_id': draft['id'], 'expected_version': 1, 'stop_indices': [0], 'mode': 'walking'}
    with session_factory() as db:
        assert not apply_route_action(db, other['id'], action, known)['ok']
        row = db.get(RouteDraft, draft['id'])
        row.input_version = 2
        db.commit()
    with session_factory() as db:
        assert not apply_route_action(db, project['id'], action, known)['ok']
        assert len(db.get(RouteDraft, draft['id']).stops) == 3


def test_bad_stop_indices_do_not_change_route(client, session_factory):
    project = create_project(client)
    response = client.post(f"/api/v1/projects/{project['id']}/routes", json={'name': '路线', 'stops': STOPS})
    draft = response.json()
    with session_factory() as db:
        for indexes in ([0, 0], [-1], [99]):
            outcome = apply_route_action(db, project['id'], {'operation': 'update', 'route_id': draft['id'], 'expected_version': 1, 'stop_indices': indexes, 'mode': 'walking'}, [{'id': draft['id'], 'version': 1}])
            assert not outcome['ok']
        row = db.get(RouteDraft, draft['id'])
        assert row.input_version == 1 and len(row.stops) == 3
        from app.schemas import RouteAction
        partial = RouteAction(operation='update', route_id=draft['id'], expected_version=1, mode='driving')
        assert apply_route_action(db, project['id'], partial.model_dump(), [{'id': draft['id'], 'version': 1}])['ok']
        assert len(row.stops) == 3 and row.mode == 'driving'
        partial = RouteAction(operation='update', route_id=draft['id'], expected_version=2, stop_indices=[0, 2])
        assert apply_route_action(db, project['id'], partial.model_dump(), [{'id': draft['id'], 'version': 2}])['ok']
        assert len(row.stops) == 2 and row.mode == 'driving'


def test_attachment_migration_preserves_existing_message(client, engine):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from tests.conftest import BACKEND_DIR
    project = create_project(client)
    posted = client.post(f"/api/v1/projects/{project['id']}/messages", json={'content': '迁移前的原始消息'})
    message_id = posted.json()['message']['id']
    config = Config(str(BACKEND_DIR / 'alembic.ini'))
    config.set_main_option('script_location', str(BACKEND_DIR / 'migrations'))
    try:
        command.downgrade(config, 'c0d1e2f3a4b5')
        with engine.connect() as conn:
            assert conn.execute(text('SELECT content FROM messages WHERE id=:id'), {'id': message_id}).scalar_one() == '迁移前的原始消息'
    finally:
        command.upgrade(config, 'head')
    with engine.connect() as conn:
        row = conn.execute(text('SELECT content, attachments FROM messages WHERE id=:id'), {'id': message_id}).one()
        assert row[0] == '迁移前的原始消息' and row[1] is None
