"""API DTO 与 Provider 结构化输出模型。

模型输出必须先通过这些 Pydantic Schema 才能进入数据库成果表（S2 要求）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- 通用


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    version: str
    app_env: str
    checks: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    id: int
    seq: int = 0
    type: str
    run_id: str | None = None
    media_asset_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --------------------------------------------------------------------------- 会话


class SessionOut(BaseModel):
    id: str
    runs_started: int
    session_run_limit: int
    daily_runs_used: int
    daily_run_limit: int


# --------------------------------------------------------------------------- 项目


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    city_hint: str | None = Field(default=None, max_length=120)


class ProjectOut(BaseModel):
    id: str
    title: str
    city_hint: str | None
    status: str
    version: int
    media_count: int = 0
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- 媒体


class MediaAssetOut(BaseModel):
    id: str
    project_id: str
    position: int
    status: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    original_filename: str
    content_url: str
    created_at: datetime
    # B5/D4：用户笔记（模型不覆盖）、媒体版本、软删除标记、当前活动运行
    note: str | None = None
    version: int = 1
    deleted: bool = False
    active_run_id: str | None = None


class MediaNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class PlaceChangeRequest(BaseModel):
    """用户改地点（B5）：可带条件版本；regenerate=true 时同时新建重新生成运行。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=400)
    region: str | None = Field(default=None, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)
    regenerate: bool = False


class MediaOperationResponse(BaseModel):
    """媒体操作统一响应：返回更新后的媒体，必要时带地点与新运行。"""

    media: "MediaAssetOut"
    place: "PlaceOut | None" = None
    run: "RunOut | None" = None


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    city_hint: str | None = Field(default=None, max_length=60)


class CandidateOut(BaseModel):
    id: str
    rank: int
    name: str
    address: str | None
    region: str | None
    rationale: str
    confidence: float | None
    uncertainty: str | None
    status: str
    run_id: str


class PlaceOut(BaseModel):
    id: str
    name: str
    address: str | None
    region: str | None
    latitude: float | None
    longitude: float | None
    provider: str
    provider_place_id: str | None
    query_at: datetime | None
    confirmed_by: str
    # B4：地点身份是否经过供应商核实；未核实时不返回地址与坐标
    match_status: str = "pending"
    match_note: str | None = None
    source: str = "user"
    version: int = 1


class ProviderPlaceCandidateOut(BaseModel):
    """供应商（高德）地点候选：歧义/冲突时给用户选择用。"""

    id: str
    rank: int
    name: str
    address: str | None
    region: str | None
    latitude: float | None
    longitude: float | None
    provider: str
    provider_place_id: str | None
    match_kind: str
    status: str


class GuideCardSection(BaseModel):
    heading: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)


class GuideCardOut(BaseModel):
    id: str
    run_id: str
    title: str
    summary: str
    sections: list[GuideCardSection]
    tips: list[str] = Field(default_factory=list)
    place_facts: dict[str, Any] | None = None
    model_name: str
    partial: bool
    created_at: datetime
    # B5：地点被改动后旧讲解保留但标记过期
    stale: bool = False
    stale_reason: str | None = None


class RunOut(BaseModel):
    id: str
    project_id: str
    # 非图片任务（intent=answer_question）没有 media
    media_asset_id: str | None
    status: str
    current_step: str
    trigger: str
    attempt: int
    error_code: str | None
    error_message: str | None
    used_steps: int
    used_tool_calls: int
    used_tokens: int
    used_cost: float
    budget_max_steps: int
    budget_max_tool_calls: int
    budget_max_tokens: int
    budget_max_cost: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class MediaSnapshot(BaseModel):
    media: MediaAssetOut
    candidates: list[CandidateOut] = Field(default_factory=list)
    # B4：供应商候选独立于模型候选展示，避免混为一谈
    provider_candidates: list[ProviderPlaceCandidateOut] = Field(default_factory=list)
    place: PlaceOut | None = None
    card: GuideCardOut | None = None
    runs: list[RunOut] = Field(default_factory=list)
    active_run: RunOut | None = None


class ProjectSnapshot(BaseModel):
    project: ProjectOut
    media: list[MediaSnapshot] = Field(default_factory=list)
    # B7：last_event_seq 是 SSE 游标（项目内序号）；last_event_id 仅诊断用
    last_event_id: int = 0
    last_event_seq: int = 0
    # D1-h：持久对话（最近 N 条，随快照一起恢复）
    messages: list[MessageOut] = Field(default_factory=list)
    last_message_seq: int = 0


# --------------------------------------------------------------------- D2 账户


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None
    email_verified: bool
    created_at: datetime


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=200)
    display_name: str | None = Field(default=None, max_length=60)
    claim_project_id: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    claim_project_id: str | None = None


class RegisterResponse(BaseModel):
    user: UserOut
    claimed_project_id: str | None = None
    email_verification_required: bool = True
    email_sent: bool = True


class MeResponse(BaseModel):
    user: UserOut


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=10, max_length=200)


class PasswordForgotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class AuthSessionOut(BaseModel):
    id: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime | None
    current: bool
    user_agent: str | None


class AuthSessionListOut(BaseModel):
    sessions: list[AuthSessionOut] = Field(default_factory=list)


class AccountStatusOut(BaseModel):
    status: str
    sessions_revoked: int = 0


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str


class ClaimResponse(BaseModel):
    project_id: str
    claimed: bool
    already_claimed: bool


class TripListOut(BaseModel):
    trips: list[ProjectOut] = Field(default_factory=list)


# --------------------------------------------------------------------- D3-b 收藏与路线


class SavedPlaceOut(BaseModel):
    id: str
    project_id: str
    place_id: str | None
    media_asset_id: str | None
    name: str
    address: str | None
    region: str | None
    latitude: float | None
    longitude: float | None
    provider: str
    provider_place_id: str | None
    source: str
    note: str | None
    position: int
    created_at: datetime


class SavedPlaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=400)
    region: str | None = Field(default=None, max_length=200)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    provider: str = Field(default="user", max_length=32)
    provider_place_id: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=500)
    # 从照片地点加入时传 media_asset_id；届时坐标与核实状态以照片上的地点为准
    media_asset_id: str | None = None


class SavedPlaceListOut(BaseModel):
    saved_places: list[SavedPlaceOut] = Field(default_factory=list)


class RouteStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    saved_place_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)


class RouteDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    mode: Literal["walking", "driving"] = "walking"
    stops: list[RouteStop] = Field(default_factory=list, max_length=20)


class RouteDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    mode: Literal["walking", "driving"] | None = None
    stops: list[RouteStop] | None = Field(default=None, max_length=20)
    expected_version: int | None = Field(default=None, ge=1)


class RouteLegOut(BaseModel):
    index_from: int
    index_to: int
    distance_meters: int
    duration_seconds: int
    polyline: str = ""


class RouteRevisionOut(BaseModel):
    id: str
    draft_id: str
    input_version: int
    mode: str
    status: str
    provider: str
    distance_meters: int | None
    duration_seconds: int | None
    legs: list[dict[str, Any]] = Field(default_factory=list)
    geometry: str | None
    error_code: str | None
    error_message: str | None
    stale: bool
    computed_at: datetime
    # 是否等于草稿当前输入（false 表示这是历史结果，不能当现状）
    is_current: bool = False


class RouteDraftOut(BaseModel):
    id: str
    project_id: str
    name: str
    mode: str
    stops: list[RouteStop] = Field(default_factory=list)
    input_version: int
    current_revision: RouteRevisionOut | None = None
    latest_revision: RouteRevisionOut | None = None
    # 停留点还在，但当前没有可用路线（例如算路失败或结果已过期）
    route_unavailable_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class RouteDraftListOut(BaseModel):
    drafts: list[RouteDraftOut] = Field(default_factory=list)


class PlaceSearchItem(BaseModel):
    rank: int
    name: str
    address: str | None
    region: str | None
    latitude: float | None
    longitude: float | None
    provider: str
    provider_place_id: str | None
    match_kind: str = "other"


class PlaceSearchOut(BaseModel):
    query: str
    provider: str
    candidates: list[PlaceSearchItem] = Field(default_factory=list)
    # 是否处于 fixture 模式（fixture 结果不能当作真实地点数据）
    fixture: bool = False


class MessageOut(BaseModel):
    version: int = 1
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    stale: bool = False
    attachments: dict[str, Any] | None = None
    id: str
    seq: int
    role: str
    intent: str | None
    status: str
    content: str
    media_ids: list[str] = Field(default_factory=list)
    place_ids: list[str] = Field(default_factory=list)
    run_id: str | None
    error_code: str | None
    created_at: datetime


class CreateMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=2000)
    # 可选：把问题限定在某张照片的范围（不指定就是工作区范围）
    media_asset_id: str | None = None
    intent: Literal["answer_question"] = "answer_question"


class MessageEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)


class CreateMessageResponse(BaseModel):
    message: MessageOut
    run: RunOut
    idempotent_replay: bool = False


class MessageListOut(BaseModel):
    messages: list[MessageOut] = Field(default_factory=list)
    last_seq: int = 0


class CreateRunResponse(BaseModel):
    run_id: str
    status: str
    media_asset_id: str | None
    project_id: str
    idempotent_replay: bool = False


class MediaUploadResponse(BaseModel):
    media: MediaAssetOut
    run: RunOut | None = None


class ConfirmPlaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirm", "correct", "reject"] = "confirm"
    candidate_id: str | None = None
    # B4：消歧阶段从供应商候选中选定具体 POI
    provider_candidate_id: str | None = None
    name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=400)
    region: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- Provider 结构化输出（模型返回必须通过校验）


class PlaceCandidateModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=400)
    region: str | None = Field(default=None, max_length=200)
    rationale: str = Field(min_length=1, max_length=2000, description="视觉依据：画面中支持该判断的线索")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    uncertainty: str | None = Field(default=None, max_length=1000)


class ImageAnalysisOutput(BaseModel):
    """多模态模型的输出结构；candidates 至少 1 条，最多 5 条。"""

    model_config = ConfigDict(extra="ignore")

    scene_summary: str = Field(min_length=1, max_length=2000)
    candidates: list[PlaceCandidateModel] = Field(min_length=1, max_length=5)
    needs_user_confirmation: bool = True


class GuideCardOutput(BaseModel):
    """文本模型的导游卡片输出结构。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    sections: list[GuideCardSection] = Field(min_length=1, max_length=6)
    tips: list[str] = Field(default_factory=list, max_length=8)


class RouteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["create", "update"]
    route_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    stop_indices: list[int] | None = Field(default=None, max_length=20)
    saved_place_ids: list[str] = Field(default_factory=list, max_length=20)
    mode: Literal["walking", "driving"] | None = None


class AnswerOutput(BaseModel):
    """工作区对话回答的结构化输出（D1-h）。

    约束"不确定时不编地点"：地点事实只能来自上下文里已确认的地点，
    模型无法确认时必须写在 uncertainty 里，而不是编一个地名。
    """

    model_config = ConfigDict(extra="ignore")

    answer: str = Field(min_length=1, max_length=4000)
    followups: list[str] = Field(default_factory=list, max_length=3)
    used_place_ids: list[str] = Field(default_factory=list, max_length=10)
    uncertainty: str | None = Field(default=None, max_length=500)
    needs_place_choice: bool = False
    search_queries: list[str] = Field(default_factory=list, max_length=3)
    selected_place_ids: list[str] = Field(default_factory=list, max_length=4)
    route_action: RouteAction | None = None


class PlaceLookupResult(BaseModel):
    """地点 Provider 返回结果（供应商事实，不是模型生成内容）。"""

    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=400)
    region: str | None = Field(default=None, max_length=200)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    provider: str = Field(min_length=1, max_length=32)
    provider_place_id: str | None = Field(default=None, max_length=128)
    raw: dict[str, Any] = Field(default_factory=dict)


# 前向引用解析（MediaAssetOut / PlaceOut / RunOut 定义在上方）
MediaOperationResponse.model_rebuild()
