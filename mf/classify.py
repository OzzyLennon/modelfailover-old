"""任务分类：把一次请求判成 code / creative / analysis / general。

纯规则、可解释、零依赖 —— 分类结果会写进审计表，方便复盘「为什么走了这条链」。
"""
from __future__ import annotations

from typing import Any

TASK_CLASSES = ("code", "creative", "analysis", "general")

# 权重：强信号 3 分，弱信号 1 分
_RULES: dict[str, dict[str, int]] = {
    "code": {
        "代码": 3, "函数": 2, "重构": 3, "报错": 3, "bug": 3, "debug": 3, "python": 2,
        "javascript": 2, "typescript": 2, "sql": 2, "api": 1, "脚本": 2, "测试": 2,
        "部署": 2, "docker": 2, "git": 1, "仓库": 1, "编译": 2, "接口": 2, "正则": 2,
        "写个": 1, "实现": 2, "修复": 3, "重构一下": 3, "命令行": 2, "shell": 2,
    },
    "creative": {
        "文案": 3, "标题": 2, "故事": 3, "剧本": 3, "营销": 3, "朋友圈": 3, "短剧": 3,
        "推广": 2, "种草": 2, "人味": 2, "去ai味": 3, "去 ai 味": 3, "文风": 2,
        "改写": 2, "润色": 2, "小说": 3, "诗": 2, "slogan": 2, "品牌": 2,
    },
    "analysis": {
        "分析": 3, "研报": 3, "洞察": 3, "总结": 2, "报告": 2, "对比": 2, "评估": 2,
        "数据": 2, "趋势": 2, "预测": 2, "复盘": 3, "梳理": 2, "调研": 3, "解读": 3,
        "为什么": 1, "意味着": 2, "风险": 1, "市场": 1, "评论": 1,
    },
}

_MAX_SCAN = 4000  # 只扫前 4000 字符，避免长文把分类带偏


def _text_of(messages: Any) -> str:
    if isinstance(messages, str):
        return messages[:_MAX_SCAN]
    if not isinstance(messages, list):
        return ""
    # 权重偏向最后一条 user 消息，但 system 也看
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):  # 多模态 content 数组
            content = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict)
            )
        if not isinstance(content, str):
            continue
        parts.append(content if role == "system" else content)
    text = "\n".join(parts)
    return text[-_MAX_SCAN:]


def classify(messages: Any) -> tuple[str, dict[str, int]]:
    """返回 (任务类别, 各类别得分)。得分全 0 → general。"""
    text = _text_of(messages).lower()
    scores = {c: 0 for c in TASK_CLASSES}
    for task, words in _RULES.items():
        for word, weight in words.items():
            if word.lower() in text:
                scores[task] += weight
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "general", scores
    # 明显是 code/creative/analysis 的强信号优先，避免 analysis 的通用词抢走 code
    for strong in ("code", "creative", "analysis"):
        if scores[strong] >= 3 and strong != best and scores[strong] >= scores[best] - 1:
            best = strong
            break
    return best, scores
