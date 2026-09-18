"""SQLAlchemy 模型：MySQL 是业务、运行状态和事件的唯一事实来源。

约定：
- 主键统一使用 36 位 UUID 字符串（便于在 SSE 事件和日志中安全引用），事件表使用自增 bigint 作为 SSE 事件 ID。
- 所有时间列存 UTC naive，由应用侧生成，避免 MySQL 会话时区差异。
- 所有查询条件列（owner、status、version、幂等键）都是普通列并建立索引；结构化载荷才放 JSON 列。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def new_id() -> str:
    return str(uuid.uuid4())


class RunStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    PARTIAL = "PARTIAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    TERMINAL = frozenset({SUCCEEDED, PARTIAL, FAILED, CANCELLED})
    ALL = frozenset({QUEUED, RUNNING, WAITING_USER, PARTIAL, SUCCEEDED, FAILED, CANCELLED})


class Intent:
    """受控意图：决定编排器走图片链路还是文字链路（D1-h）。"""

    ANALYZE_IMAGE = "analyze_image"
    ANSWER_QUESTION = "answer_question"


class StepName:
    ANALYZE_IMAGE = "analyze_image"
    # 非图片任务（工作区对话）的唯一步骤：不调用视觉模型
    ANSWER_QUESTION = "answer_question"
    WAIT_FOR_PLACE_CONFIRMATION = "wait_for_place_confirmation"
    # B4：供应商返回同名多候选/城市冲突时，回到用户消歧（与首次确认分开记账）
    WAIT_FOR_PLACE_DISAMBIGUATION = "wait_for_place_disambiguation"
    LOOKUP_PLACE = "lookup_place"
    GENERATE_GUIDE_CARD = "generate_guide_card"


class MediaStatus:
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    ANALYZING = "ANALYZING"
    WAITING_USER = "WAITING_USER"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DemoSession(Base):
    __tablename__ = "demo_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    runs_started: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 仅保存不可逆的客户端指纹（用于公开 Demo 的限流），不保存 IP 原文。
    client_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class GuideProject(Base):
    __tablename__ = "guide_projects"
    __table_args__ = (
        UniqueConstraint("active_owner_key", name="uq_projects_active_owner"),
        Index("ix_projects_owner_status", "owner_session_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False
    )
    # D2：登录用户拥有的工作区；访客工作区为 NULL，认领后写入
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    city_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # 仅当项目处于 active 时等于 owner_session_id，用于“一个会话一个活动项目”的唯一约束。
    active_owner_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    media_assets: Mapped[list["MediaAsset"]] = relationship(
        back_populates="project", order_by="MediaAsset.position", cascade="all, delete-orphan"
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("project_id", "position", name="uq_media_project_position"),
        Index("ix_media_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=MediaStatus.UPLOADED, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # 软删除：移出相册后可撤销；旧 run 不得把内容写回当前界面
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 乐观并发：多标签同时改同一张照片时用 If-Match 冲突
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 用户笔记独立于模型卡片，模型不得覆盖
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    project: Mapped[GuideProject] = relationship(back_populates="media_assets")
    candidates: Mapped[list["PlaceCandidate"]] = relationship(
        back_populates="media_asset", order_by="PlaceCandidate.rank", cascade="all, delete-orphan"
    )
    place: Mapped["Place | None"] = relationship(back_populates="media_asset", uselist=False)
    card: Mapped["GuideCard | None"] = relationship(back_populates="media_asset", uselist=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_runs_project_idempotency"),
        Index("ix_runs_status_lease", "status", "lease_expires_at"),
        Index("ix_runs_media_created", "media_asset_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
    )
    # B5/D1：文字任务不绑图片，因此 media 可空；图片任务仍带 media_asset_id。
    media_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED, nullable=False)
    current_step: Mapped[str] = mapped_column(String(48), default=StepName.ANALYZE_IMAGE, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="initial", nullable=False)
    # 受控意图：analyze_image / answer_question / lookup_place / guide_card / reroute ...
    intent: Mapped[str] = mapped_column(String(32), default="analyze_image", nullable=False)
    # 输入上下文版本（改地点、改笔记后旧建议失效）
    input_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 恢复操作创建新 run 并引用来源 run，终态 run 不复活
    source_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # B2：租约代次（fencing token）。每次认领 +1，业务写回必须匹配自己的 epoch。
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # B3：调用前原子预留、调用后结算；预留字段使并发调用无法超额。
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    budget_max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_max_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    used_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    steps: Mapped[list["RunStep"]] = relationship(
        back_populates="run", order_by="RunStep.sequence", cascade="all, delete-orphan"
    )


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (UniqueConstraint("run_id", "name", name="uq_run_steps_run_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    run: Mapped[AgentRun] = relationship(back_populates="steps")


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (Index("ix_tool_invocations_run", "run_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("run_steps.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_prompt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_completion: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    price_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    # B3：用量是否已知。未知时 tokens/cost 为 NULL，禁止写 0 冒充免费。
    usage_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class PlaceCandidate(Base):
    __tablename__ = "place_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "rank", name="uq_candidates_run_rank"),
        Index("ix_candidates_media_status", "media_asset_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    media_asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(400), nullable=True)
    region: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="model", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    media_asset: Mapped[MediaAsset] = relationship(back_populates="candidates")


class Place(Base):
    __tablename__ = "places"
    __table_args__ = (UniqueConstraint("media_asset_id", name="uq_places_media_asset"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False)
    media_asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(400), nullable=True)
    region: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    provider_place_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    query_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    # B4：供应商匹配状态（matched / matched_name_only / ambiguous / conflict / no_result /
    # user_selected / provider_error / unverified_by_user / pending）。
    # 只有 matched / matched_name_only / user_selected 允许存在供应商地址与坐标。
    match_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    # 匹配说明（例如"仅按名称匹配，未校验城市"），供界面展示，不参与逻辑判断。
    match_note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 地点版本：用户改地点后 +1，受影响讲解标记过期但保留历史
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 来源：user（用户确认）/ amap（供应商匹配成功）
    source: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    media_asset: Mapped[MediaAsset] = relationship(back_populates="place")


class PlaceMatchCandidate(Base):
    """B4：供应商（高德）返回的地点候选，独立于用户确认身份保存。

    选中的一条不会覆盖用户确认的名称；歧义/冲突时由用户在界面上选择。
    """

    __tablename__ = "place_match_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "rank", name="uq_match_candidates_run_rank"),
        Index("ix_match_candidates_media", "media_asset_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    media_asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(400), nullable=True)
    region: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_place_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # exact / prefix / other：与用户确认名称的匹配强度
    match_kind: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # CANDIDATE / SELECTED / REJECTED
    status: Mapped[str] = mapped_column(String(16), default="CANDIDATE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class GuideCard(Base):
    __tablename__ = "guide_cards"
    __table_args__ = (
        UniqueConstraint("media_asset_id", name="uq_cards_media_asset"),
        Index("ix_cards_project", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False)
    media_asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    place_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("places.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    tips: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    place_facts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    partial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # B5：地点被用户改动后，旧讲解保留但标记过期，界面必须能看出"这不是当前地点的讲解"
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    media_asset: Mapped[MediaAsset] = relationship(back_populates="card")


class ProjectEventCounter(Base):
    """B7：每项目事件序号分配器。

    序号在写事件的事务内分配（行锁持有到提交），因此**序号顺序等于提交顺序**：
    较小序号的事件一定先提交，读者只要记住游标就不会漏事件。
    回滚会留下空洞，但不会出现"小序号后提交"。
    """

    __tablename__ = "project_event_counters"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), primary_key=True
    )
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"
    __table_args__ = (
        Index("ix_events_project_id", "project_id", "id"),
        UniqueConstraint("project_id", "seq", name="uq_events_project_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # B7：项目内事件游标（与提交顺序一致）；SSE 的 Last-Event-ID 用的是这个值
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    media_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        Index("ix_usage_day", "day"),
        Index("ix_usage_session_day", "session_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    units: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    price_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False, default=lambda: utcnow().date())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class User(Base):
    """登录账号（D2）。

    口令只存 Argon2id 哈希；邮箱小写归一化后唯一。账号删除采用软删除（status=disabled），
    以便审计与可追溯清理。
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class AuthSession(Base):
    """登录会话：数据库只存令牌摘要，撤销即置 revoked_at（D2）。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token"),
        Index("ix_auth_sessions_user", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EmailToken(Base):
    """邮箱验证 / 密码重置令牌（只存摘要，一次性使用）。"""

    __tablename__ = "email_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_email_tokens_token"),
        Index("ix_email_tokens_user_purpose", "user_id", "purpose"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class WorkspaceClaim(Base):
    """访客工作区认领记录：一个工作区只能被认领一次（幂等重试返回同一条）。"""

    __tablename__ = "workspace_claims"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_claims_project"),
        Index("ix_claims_user", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    guest_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SavedPlace(Base):
    """收藏地点（D3-b）：属于工作区，可由照片地点一键加入，也可手工添加。

    去重键：同一工作区内 provider_place_id 相同视为同一收藏；
    没有 provider_place_id（用户手工输入）时按 (name, region) 去重。
    """

    __tablename__ = "saved_places"
    __table_args__ = (
        UniqueConstraint("project_id", "dedupe_key", name="uq_saved_places_project_dedupe"),
        Index("ix_saved_places_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
    )
    place_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    media_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(400), nullable=True)
    region: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    provider_place_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 收藏来源：photo（由照片地点加入）/ manual（手工）/ route（路线停留点）
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class RouteDraft(Base):
    """路线草稿（D3-b）：停留点列表是用户资产，永不因为算路失败而丢失。

    ``current_revision_id`` 指向最新一次成功的算路结果；失败只写入 FAILED 修订，
    草稿本身的 stops 不变。
    """

    __tablename__ = "route_drafts"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_route_drafts_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # walking / driving（高德步行与驾车两个接口）
    mode: Mapped[str] = mapped_column(String(16), default="walking", nullable=False)
    # 停留点：[{"saved_place_id": ..., "name": ..., "latitude": ..., "longitude": ...}]
    stops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # 输入版本：stops/mode 每次改动 +1，用于丢弃过期的算路结果
    input_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    revisions: Mapped[list["RouteRevision"]] = relationship(
        back_populates="draft", order_by="RouteRevision.seq.desc()", cascade="all, delete-orphan"
    )


class RouteRevision(Base):
    """一次算路尝试的结果（成功或失败都留痕，便于"失败可重试、成功不被过期结果覆盖"）。"""

    __tablename__ = "route_revisions"
    __table_args__ = (
        Index("ix_route_revisions_draft_created", "draft_id", "created_at"),
        UniqueConstraint("draft_id", "seq", name="uq_route_revisions_draft_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 稳定排序键：created_at 只到秒，同一秒内两次算路无法靠时间区分；
    # 序号在同一草稿的行锁内分配（见 services/places_routes.compute_route）
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("route_drafts.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 产生该修订的输入版本（与草稿当前的 input_version 不一致即为过期结果）
    input_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # OK / FAILED
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_route_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [{"from": 0, "to": 1, "distance_meters": ..., "duration_seconds": ..., "polyline": "lng,lat;..."}]
    legs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    geometry: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    draft: Mapped[RouteDraft] = relationship(back_populates="revisions")


class UploadCommand(Base):
    """B1：把“上传照片 + 可选创建任务”当作一条幂等命令。

    同一个 (owner_session_id, idempotency_key) 只允许一条记录：请求指纹相同则重放原结果，
    指纹不同返回 409。记录与业务写入在同一事务，避免“有照片但永远无法开始”。
    """

    __tablename__ = "upload_commands"
    __table_args__ = (
        UniqueConstraint("owner_session_id", "idempotency_key", name="uq_upload_cmd_owner_key"),
        Index("ix_upload_cmd_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="COMPLETED", nullable=False)
    media_asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class InvocationAttempt(Base):
    """B3：每一次真实的 Provider 请求单独记录，包括失败、JSON 修复与重试。

    - 调用前写 RESERVED（含预算预留），调用后按实际用量结算为 SUCCEEDED/FAILED。
    - 用量未知时 tokens/cost 保持 NULL 并标记 usage_known=False，不写 0 冒充免费。
    """

    __tablename__ = "invocation_attempts"
    __table_args__ = (
        Index("ix_attempts_run", "run_id", "started_at"),
        Index("ix_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)
    step_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("run_steps.id", ondelete="SET NULL"), nullable=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="RESERVED", nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_prompt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_completion: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    price_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QuotaBucket(Base):
    """B6：并发安全的额度桶。按 (scope, bucket_key, day) 条件扣减，先扣后做。"""

    __tablename__ = "quota_buckets"
    __table_args__ = (
        UniqueConstraint("scope", "bucket_key", "bucket_day", name="uq_quota_scope_key_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # global | session | ip
    bucket_key: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket_day: Mapped[date] = mapped_column(Date, nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Message(Base):
    """D1：持久对话消息。用户消息与服务端回答都在同一工作区内按 seq 递增。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_messages_project_seq"),
        Index("ix_messages_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="READY", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    media_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    place_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
