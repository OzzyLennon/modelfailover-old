"""路由：把「任务类别 + 健康状态」变成一条有序的上游尝试链。

排序规则（先分层，层内再按优先级）：
  1. 健康 且 强项匹配任务类别
  2. 健康 且 通用能力
  3. 状态未知 且 强项匹配
  4. 状态未知 / 降级（degraded）
  5. down —— 只在前面的全挂了才用（宁可报明确错误也别悄悄降级）

「能力降级」：代码任务先走强项匹配的 provider，再走通用 provider，最后才走任意存活 provider。
"""
from __future__ import annotations

from .config import Provider
from .health import HealthState

_RANK = {"healthy": 0, "unknown": 1, "degraded": 2, "down": 3}


def _strength_match(p: Provider, task_class: str) -> bool:
    return task_class in p.strengths


def _is_general(p: Provider) -> bool:
    return "general" in p.strengths or not p.strengths


def build_chain(
    task_class: str,
    providers: list[Provider],
    health: dict[str, HealthState],
    *,
    max_attempts: int = 3,
    pinned: str | None = None,
) -> list[Provider]:
    """返回有序的 provider 列表（最多 max_attempts 个）。"""
    pool = [p for p in providers if p.enabled]
    if pinned:
        head = [p for p in pool if p.name == pinned]
        rest = [p for p in pool if p.name != pinned]
        if head:
            # 钉住的首选排最前，哪怕它现在不健康（用户明确指定了）
            return (head + rest)[:max_attempts]

    def sort_key(p: Provider):
        st = health.get(p.name)
        status = st.status if st else "unknown"
        if _strength_match(p, task_class):
            tier = 0
        elif _is_general(p):
            tier = 1
        else:
            tier = 2
        return (_RANK.get(status, 1), tier, p.priority, p.name)

    return sorted(pool, key=sort_key)[:max_attempts]


def explain_chain(
    task_class: str,
    providers: list[Provider],
    health: dict[str, HealthState],
    chain: list[Provider],
) -> list[str]:
    """给人看的路由解释，写进审计表。"""
    out = []
    for i, p in enumerate(chain, 1):
        st = health.get(p.name)
        status = st.status if st else "unknown"
        tag = "强项匹配" if _strength_match(p, task_class) else ("通用" if _is_general(p) else "旁路")
        out.append(f"#{i} {p.name}[{tag}/{status}] model={p.model}")
    return out
