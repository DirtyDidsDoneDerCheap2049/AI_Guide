"""S1：图片上传的实际内容校验、数量上限、事务一致性与受控下载。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import create_project, make_image_bytes, unique_title


def _media_files(project_id: str, client: TestClient, *, fmt: str = "PNG", declared: str | None = None, start: bool = False):
    mime = declared or f"image/{fmt.lower()}"
    return client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": (f"photo.{fmt.lower()}", make_image_bytes(fmt), mime)},
        data={"start_analysis": "true" if start else "false"},
    )


def test_upload_accepts_jpeg_png_webp_without_starting_runs(client):
    project = create_project(client, title=unique_title("formats"))
    for fmt in ("JPEG", "PNG", "WEBP"):
        response = _media_files(project["id"], client, fmt=fmt)
        assert response.status_code == 201, response.text
        media = response.json()["media"]
        assert media["mime_type"] == f"image/{'jpeg' if fmt == 'JPEG' else fmt.lower()}"
        assert media["width"] == 64 and media["height"] == 64
        assert response.json()["run"] is None

    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert [item["media"]["position"] for item in snapshot["media"]] == [1, 2, 3]


def test_upload_rejects_unsupported_content(client):
    project = create_project(client, title=unique_title("badcontent"))
    response = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("fake.png", b"this is not an image at all", "image/png")},
        data={"start_analysis": "false"},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_image_content"


def test_upload_rejects_mime_mismatch(client):
    project = create_project(client, title=unique_title("mismatch"))
    response = _media_files(project["id"], client, fmt="PNG", declared="image/jpeg")
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "mime_mismatch"


def test_upload_rejects_oversize_without_touching_disk(client, settings):
    project = create_project(client, title=unique_title("oversize"))
    payload = b"\x00" * (settings.media_max_bytes + 1024)
    before = sorted(path.name for path in Path(settings.media_root).rglob("*") if path.is_file())
    response = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("big.png", payload, "image/png")},
        data={"start_analysis": "false"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large"
    after = sorted(path.name for path in Path(settings.media_root).rglob("*") if path.is_file())
    assert before == after


def test_project_media_limit_is_three(client):
    project = create_project(client, title=unique_title("limit"))
    for _ in range(3):
        assert _media_files(project["id"], client).status_code == 201
    response = _media_files(project["id"], client)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "media_limit_reached"


def test_transaction_failure_cleans_up_stored_file(app, settings, monkeypatch):
    """文件已落盘但事务失败时必须删除文件，且不留下数据库记录。"""
    import app.services.media as media_service
    from app.models import MediaAsset

    with TestClient(app, raise_server_exceptions=False) as test_client:
        project = create_project(test_client, title=unique_title("rollback"))
        files_before = {path for path in Path(settings.media_root).rglob("*") if path.is_file()}

        def boom(*args, **kwargs):
            raise RuntimeError("simulated transaction failure")

        # B1 之后上传是单事务命令，事件在服务层写入；补丁点必须跟着移动。
        monkeypatch.setattr(media_service, "append_event", boom)
        response = _media_files(project["id"], test_client)
        assert response.status_code == 500
        monkeypatch.undo()

        files_after = {path for path in Path(settings.media_root).rglob("*") if path.is_file()}
        assert files_after == files_before

        with app.state.session_factory() as db:
            assert db.query(MediaAsset).count() == 0


def test_idempotency_key_prevents_duplicate_runs(app, client, db):
    """B1 之后的语义：同 key + 同内容重放原结果；同 key + 不同内容是 409 冲突。

    旧断言（两次不同图片都返回 201、共用一个 run）正是 07 号报告 B1 描述的缺陷，
    因此这里改为按修正后的契约断言。
    """
    from app.models import AgentRun, MediaAsset

    project = create_project(client, title=unique_title("idem"))
    key = "upload-key-001"
    same_payload = {"file": ("a.png", make_image_bytes("PNG"), "image/png")}
    first = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files=same_payload,
        data={"start_analysis": "true"},
        headers={"Idempotency-Key": key},
    )
    replay = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("a.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true"},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201 and replay.status_code == 201
    assert first.json()["media"]["id"] == replay.json()["media"]["id"]
    assert first.json()["run"]["id"] == replay.json()["run"]["id"]

    conflict = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("b.png", make_image_bytes("PNG", color=(10, 20, 30)), "image/png")},
        data={"start_analysis": "true"},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"

    with app.state.session_factory() as session:
        assert session.query(AgentRun).count() == 1
        assert session.query(MediaAsset).count() == 1


def test_media_content_requires_ownership(app):
    with TestClient(app) as owner:
        project = create_project(owner, title=unique_title("content"))
        response = _media_files(project["id"], owner)
        media_id = response.json()["media"]["id"]
        content = owner.get(f"/api/v1/media/{media_id}/content")
        assert content.status_code == 200
        assert content.headers["content-type"].startswith("image/png")
        assert content.content.startswith(b"\x89PNG")

    with TestClient(app) as other:
        assert other.get(f"/api/v1/media/{media_id}/content").status_code == 404
