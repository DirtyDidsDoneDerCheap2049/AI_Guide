"""地点身份判定（B4）。

问题：地名搜索天然返回多条同名 POI，旧实现取 ``pois[0]`` 直接覆盖用户确认过的
地点身份，可能生成一份"看起来已核实"的错误卡片。

本模块把"用户确认的身份"与"供应商匹配结果"分开处理：

* 判定输入：用户确认的名称、用户/项目给出的城市线索、已知 POI ID、供应商候选列表。
* 判定结果：``matched`` / ``matched_name_only`` / ``ambiguous`` / ``conflict`` /
  ``no_result``，只有前两种允许写回供应商事实；歧义与冲突必须回到用户确认。
* 供应商候选一律完整保存（调用方写入 ``place_match_candidates``），不因为选中一条就丢弃其余候选。

本模块是纯函数，不访问数据库与网络，便于直接回归。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.agent.providers.base import PlaceResult

# 判定状态
MATCHED = "matched"
MATCHED_NAME_ONLY = "matched_name_only"
AMBIGUOUS = "ambiguous"
CONFLICT = "conflict"
NO_RESULT = "no_result"
USER_SELECTED = "user_selected"

# 允许写回供应商事实的状态（用户显式选择也允许）
WRITABLE = frozenset({MATCHED, MATCHED_NAME_ONLY, USER_SELECTED})
# 需要回到用户确认的状态
NEEDS_USER = frozenset({AMBIGUOUS, CONFLICT})

_SPACE = re.compile(r"\s+")


def normalize_name(value: str | None) -> str:
    """归一化地名：去掉空白、全角空格与常见后缀噪声，便于比较。"""
    text = _SPACE.sub("", value or "")
    text = text.replace("\u3000", "")
    return text.strip().lower()


def name_kind(user_name: str, candidate_name: str | None) -> str:
    """名称匹配强度：exact（完全相同）/ prefix（互为前缀）/ other（模糊或不相关）。"""
    left = normalize_name(user_name)
    right = normalize_name(candidate_name)
    if not left or not right:
        return "other"
    if left == right:
        return "exact"
    if left in right or right in left:
        return "prefix"
    return "other"


def region_matches(hint: str | None, region: str | None) -> bool | None:
    """城市/行政区线索是否与候选地一致。

    True 一致，False 明确不一致，None 信息不足（无法判定，不作为冲突）。
    """
    left = normalize_name(hint)
    right = normalize_name(region)
    if not left or not right:
        return None
    if left in right or right in left:
        return True
    # 只比较到"市/区/县"级别的短线索，避免把长地址误判为不一致
    left_core = re.split(r"[省市区县州盟]", left)[0]
    right_core = re.split(r"[省市区县州盟]", right)[0]
    if left_core and right_core and (left_core in right_core or right_core in left_core):
        return True
    return False


@dataclass
class PlaceMatch:
    """一次地点搜索的判定结果。"""

    status: str
    reason: str
    candidate: PlaceResult | None = None
    candidates: list[PlaceResult] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def needs_user(self) -> bool:
        return self.status in NEEDS_USER

    @property
    def writable(self) -> bool:
        return self.status in WRITABLE

    def matched_name(self) -> str | None:
        return self.candidate.name if self.candidate else None

    def to_payload(self) -> dict[str, Any]:
        """写进事件/运行快照的结构（不含密钥，只保留判定所需字段）。"""
        return {
            "status": self.status,
            "reason": self.reason,
            "hints": self.hints,
            "matched": (
                {
                    "name": self.candidate.name,
                    "address": self.candidate.address,
                    "region": self.candidate.region,
                    "latitude": self.candidate.latitude,
                    "longitude": self.candidate.longitude,
                    "provider": self.candidate.provider,
                    "provider_place_id": self.candidate.provider_place_id,
                }
                if self.candidate
                else None
            ),
            "candidates": [
                {
                    "rank": index,
                    "name": item.name,
                    "address": item.address,
                    "region": item.region,
                    "latitude": item.latitude,
                    "longitude": item.longitude,
                    "provider": item.provider,
                    "provider_place_id": item.provider_place_id,
                    "kind": self.kinds[index - 1] if index - 1 < len(self.kinds) else "other",
                }
                for index, item in enumerate(self.candidates, start=1)
            ],
        }


def resolve_place_match(
    *,
    user_name: str,
    candidates: Iterable[PlaceResult],
    city_hints: Iterable[str | None] = (),
    provider_place_id: str | None = None,
) -> PlaceMatch:
    """按名称 / 城市 / 已选 POI ID 判定供应商候选是否等于用户确认的地点。"""
    items = [item for item in candidates if item is not None]
    hints = [hint for hint in city_hints if hint and normalize_name(hint)]
    kinds = [name_kind(user_name, item.name) for item in items]

    if not items:
        return PlaceMatch(status=NO_RESULT, reason="provider_returned_no_candidate", hints=hints)

    # 1) 已选 POI ID 优先：用户此前明确选过某个 POI，ID 相同即为同一地点。
    if provider_place_id:
        id_hits = [item for item in items if item.provider_place_id == provider_place_id]
        if len(id_hits) == 1:
            index = items.index(id_hits[0])
            return PlaceMatch(
                status=MATCHED,
                reason="provider_place_id",
                candidate=id_hits[0],
                candidates=items,
                kinds=kinds,
                hints=hints,
            )
        if len(id_hits) > 1:
            return PlaceMatch(
                status=AMBIGUOUS,
                reason="provider_place_id_duplicated",
                candidate=None,
                candidates=id_hits,
                kinds=[kinds[items.index(item)] for item in id_hits],
                hints=hints,
            )

    exact = [item for item in items if name_kind(user_name, item.name) == "exact"]
    if not exact:
        # 供应商没有返回同名结果：模糊结果不能直接采信，交回用户确认。
        return PlaceMatch(
            status=AMBIGUOUS,
            reason="no_exact_name_match",
            candidate=None,
            candidates=items,
            kinds=kinds,
            hints=hints,
        )

    if len(exact) == 1:
        item = exact[0]
        verdict = _region_verdict(hints, item.region)
        if verdict is False:
            return PlaceMatch(
                status=CONFLICT,
                reason="region_conflict",
                candidate=item,
                candidates=items,
                kinds=kinds,
                hints=hints,
            )
        return PlaceMatch(
            status=MATCHED if verdict is True else MATCHED_NAME_ONLY,
            reason="exact_name_and_region" if verdict is True else "exact_name_region_unknown",
            candidate=item,
            candidates=items,
            kinds=kinds,
            hints=hints,
        )

    # 多条同名：即使城市线索能缩小到一条，也交回用户确认——
    # 同名 POI 是最容易被静默选错的情形（B4 的回归样例之一）。
    # 城市一致的候选排在前面，界面按此顺序提示。
    ranked = sorted(
        exact,
        key=lambda item: 0 if _region_verdict(hints, item.region) is True else 1,
    )
    if hints and not any(_region_verdict(hints, item.region) is True for item in exact):
        return PlaceMatch(
            status=CONFLICT,
            reason="region_conflict_multiple",
            candidate=None,
            candidates=ranked,
            kinds=[kinds[items.index(item)] for item in ranked],
            hints=hints,
        )
    return PlaceMatch(
        status=AMBIGUOUS,
        reason="multiple_same_name",
        candidate=None,
        candidates=ranked,
        kinds=[kinds[items.index(item)] for item in ranked],
        hints=hints,
    )


def _region_verdict(hints: list[str], region: str | None) -> bool | None:
    """综合多个城市线索：任一明确不一致即冲突；任一一致即通过。"""
    verdicts = [region_matches(hint, region) for hint in hints]
    if any(item is False for item in verdicts):
        return False
    if any(item is True for item in verdicts):
        return True
    return None


__all__ = [
    "AMBIGUOUS",
    "CONFLICT",
    "MATCHED",
    "MATCHED_NAME_ONLY",
    "NEEDS_USER",
    "NO_RESULT",
    "USER_SELECTED",
    "WRITABLE",
    "PlaceMatch",
    "name_kind",
    "normalize_name",
    "region_matches",
    "resolve_place_match",
]
