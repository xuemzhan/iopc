"""来源分级 + 令牌化。

纯规则、无模型、可在任何 LLM 调用前同步执行。
原则：
- 命中 `[policy].keywords` 的文本视为敏感；
- 敏感文本送模型前把机构名/人名换成 `ORG_n` / `PERSON_n`；
- 映射表只活在内存里（不落盘、不进日志、不进 prompt），回来后 `unmask` 还原。
"""
from __future__ import annotations

import re
from typing import Any

from .config import cfg

# 常见误判词：这些不是人名，不能因为后面跟着「表示/指出」就被替换
_STOP = {
    "我们", "大家", "会议", "报告", "团队", "领导", "专家", "公司", "部门", "单位",
    "集团", "这里", "其中", "同时", "因此", "但是", "如果", "通过", "关于", "对于",
    "目前", "当前", "工作", "问题", "情况", "内容", "方面", "系统", "项目", "技术",
    "研究", "分析", "总结", "建议", "要求", "指出", "强调", "认为", "介绍", "谈到",
    "负责", "牵头", "验收", "主持", "组织", "参会", "与会", "现场", "会上", "会后",
}

_PERSON_VERB = re.compile(
    r"([\u4e00-\u9fa5]{2,3})(?=(?:表示|指出|强调|提出|认为|介绍|谈到|坦言|直言|称|说道))"
)
_PERSON_ROLE = re.compile(
    r"(?<=由)([\u4e00-\u9fa5]{2,3})(?=(?:负责|牵头|验收|主持|组织|统筹))"
)
_TOKEN_RE = re.compile(r"\b(?:ORG|PERSON)_\d+\b")


def enabled() -> bool:
    return bool(cfg().get("policy.sensitive_enabled", True))


def keywords() -> list[str]:
    raw = cfg().get("policy.keywords", []) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(k) for k in raw if str(k).strip()]


def classify(text: str, title: str = "") -> bool:
    """标题或正文命中任一关键词 → 敏感。"""
    if not enabled():
        return False
    kws = keywords()
    if not kws:
        return False
    hay = f"{title}\n{text or ''}"
    return any(k in hay for k in kws)


def _mask_with(text: str, mapping: dict[str, str], counters: dict[str, int]) -> str:
    def token(kind: str, original: str) -> str:
        for tok, orig in mapping.items():
            if orig == original:
                return tok
        counters[kind] += 1
        tok = f"{kind}_{counters[kind]}"
        mapping[tok] = original
        return tok

    out = text
    for kw in sorted(keywords(), key=len, reverse=True):
        if kw and kw in out:
            out = out.replace(kw, token("ORG", kw))

    names: list[str] = []
    for pattern in (_PERSON_VERB, _PERSON_ROLE):
        for m in pattern.finditer(out):
            name = m.group(1)
            if name not in _STOP and not _TOKEN_RE.fullmatch(name) and name not in names:
                names.append(name)
    for name in names:
        out = out.replace(name, token("PERSON", name))
    return out


def new_state() -> dict[str, Any]:
    """一次请求内的脱敏状态：跨多次 mask_many 复用同一映射（令牌一致）。"""
    return {"mapping": {}, "counters": {"ORG": 0, "PERSON": 0}}


def mask_many(texts: list[str], state: dict[str, Any] | None = None
              ) -> tuple[list[str], dict[str, str]]:
    """一次调用内共享计数器：同一原文在任何字段里都是同一个令牌。

    传入 `state` 可跨多次调用延续映射（例如先脱敏问题、再脱敏检索上下文）。
    """
    if not enabled():
        return list(texts), (state or {}).get("mapping", {})
    state = state if state is not None else new_state()
    mapping: dict[str, str] = state["mapping"]
    counters: dict[str, int] = state["counters"]
    return [_mask_with(t or "", mapping, counters) for t in texts], mapping


def mask(text: str) -> tuple[str, dict[str, str]]:
    """把机构名/人名替换成稳定令牌。同一原文在同一请求内永远是同一个令牌。"""
    if not text or not enabled():
        return text, {}
    out, mapping = mask_many([text])
    return out[0], mapping


def unmask(text: str, mapping: dict[str, str]) -> str:
    if not text or not mapping:
        return text
    out = text
    # 长令牌先换，避免 ORG_1 命中 ORG_10 的前缀
    for tok in sorted(mapping, key=len, reverse=True):
        out = out.replace(tok, mapping[tok])
    return out


def mask_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """一次性脱敏整组消息，返回共享映射（跨分块复用）。"""
    masked, mapping = mask_many([str(m.get("content", "")) for m in messages])
    return [{**m, "content": masked[i]} for i, m in enumerate(messages)], mapping
