"""项目 API：创建匿名项目、读取当前项目与完整快照。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_optional_user, load_owned_project
from app.api.serialize import build_project_snapshot, project_to_out
from app.events import EventType, append_event
from app.models import DemoSession, GuideProject, MediaAsset, User, utcnow
from app.schemas import ProjectCreate, ProjectOut, ProjectSnapshot, ProjectUpdateRequest

router = APIRouter()


def _active_project(db: Session, session_id: str, user: User | None = None) -> GuideProject | None:
    """活动项目：登录用户看自己账号的，访客看自己会话且尚未被认领的。"""
    if user is not None:
        return db.execute(
            select(GuideProject).where(
                GuideProject.owner_user_id == user.id,
                GuideProject.status == "active",
            )
            .order_by(GuideProject.updated_at.desc(), GuideProject.id.desc()).limit(1)
        ).scalar_one_or_none()
    return db.execute(
        select(GuideProject).where(
            GuideProject.owner_session_id == session_id,
            GuideProject.owner_user_id.is_(None),
            GuideProject.status == "active",
        )
        .order_by(GuideProject.updated_at.desc(), GuideProject.id.desc()).limit(1)
    ).scalar_one_or_none()


@router.post(
    "/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建一段新旅行（历史旅行保留）",
)
def create_project(
    payload: ProjectCreate,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = GuideProject(
        owner_session_id=session_row.id,
        # D2：登录用户创建的工作区属于账号，换浏览器登录也能打开
        owner_user_id=user.id if user is not None else None,
        title=payload.title.strip(),
        city_hint=(payload.city_hint or "").strip() or None,
        status="active",
        # Keep the legacy nullable column; new trips are independently accessible.
        active_owner_key=None,
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "active_project_exists"},
        ) from None

    append_event(
        db,
        project_id=project.id,
        type=EventType.PROJECT_CREATED,
        payload={"title": project.title, "city_hint": project.city_hint},
    )
    db.commit()
    db.refresh(project)
    return project_to_out(project, media_count=0)


@router.patch("/projects/{project_id}", response_model=ProjectOut, summary="修改项目标题或城市线索")
def update_project(
    project_id: str,
    payload: ProjectUpdateRequest,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = load_owned_project(db, project_id, session_row, user)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project_not_found")

    changed: list[str] = []
    if payload.title is not None:
        title = payload.title.strip()
        if title and title != project.title:
            project.title = title
            changed.append("title")
    if payload.city_hint is not None:
        hint = payload.city_hint.strip() or None
        if hint != project.city_hint:
            project.city_hint = hint
            changed.append("city_hint")
    if not changed:
        return project_to_out(project)

    project.version += 1
    project.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.PROJECT_UPDATED,
        payload={"changed": changed, "version": project.version},
    )
    db.commit()
    db.refresh(project)
    return project_to_out(project)


@router.get("/projects/current", response_model=ProjectSnapshot, summary="当前活动项目快照")
def read_current_project(
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ProjectSnapshot:
    project = _active_project(db, session_row.id, user)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project_not_found")
    return build_project_snapshot(db, project)


@router.get("/projects/{project_id}", response_model=ProjectSnapshot, summary="按 ID 读取项目快照")
def read_project(
    project_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ProjectSnapshot:
    project = load_owned_project(db, project_id, session_row, user)
    return build_project_snapshot(db, project)


@router.post(
    "/projects/{project_id}/archive",
    response_model=ProjectOut,
    summary="归档当前项目，便于重新开始（不删除任何数据）",
)
def archive_project(
    project_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = load_owned_project(db, project_id, session_row, user)
    project.status = "archived"
    # 释放“一个会话一个活动项目”的唯一键。
    project.active_owner_key = None
    project.updated_at = utcnow()
    project.version += 1
    media_count = int(
        db.execute(select(func.count(MediaAsset.id)).where(MediaAsset.project_id == project.id)).scalar() or 0
    )
    db.commit()
    db.refresh(project)
    return project_to_out(project, media_count=media_count)
