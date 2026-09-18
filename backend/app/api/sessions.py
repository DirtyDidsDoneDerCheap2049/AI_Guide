"""匿名会话 API：页面启动时取得/创建会话，并返回公开 Demo 额度。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings_dep
from app.config import Settings
from app.limits import daily_runs_used
from app.models import DemoSession
from app.schemas import SessionOut

router = APIRouter()


@router.get("/session", response_model=SessionOut, summary="取得或创建匿名会话")
def read_session(
    session_row: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> SessionOut:
    return SessionOut(
        id=session_row.id,
        runs_started=session_row.runs_started,
        session_run_limit=settings.demo_session_run_limit,
        daily_runs_used=daily_runs_used(db),
        daily_run_limit=settings.demo_daily_run_limit,
    )
