"""
lib_confidence.py — 冲突检测与多源对齐

职责：
- 当新抽取的条目和 wiki 既有页（type+slug 相同）有重叠时，决定如何处理
- 决策：replace | append | conflict
- 实现：先做规则检测（数值/日期字段差异），再让 LLM 语义比对（兜底）

调用方：scripts/ingest.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from scripts.lib_llm import LlamaCppClient, parse_json_safe

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class WikiPage:
    """加载一个 wiki md 文件的最小结构"""

    path: Path  # 相对 WIKI_ROOT
    fm: Dict[str, Any]
    body: str
    facts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return str(self.fm.get("slug", self.path.stem))

    @property
    def type(self) -> str:
        return str(self.fm.get("type", "stub"))


@dataclass
class DiffResult:
    """冲突检测结果"""

    action: str  # "no_overlap" | "replace" | "append" | "conflict"
    reason: str = ""
    conflicting_keys: List[str] = field(default_factory=list)
    appended_keys: List[str] = field(default_factory=list)
    confidence_penalty: float = 0.0  # 建议扣多少分


# ---------------------------------------------------------------------------
# wiki md 加载
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """拆分 frontmatter 和 body。"""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2).strip()


def _parse_facts_from_body(body: str) -> List[Dict[str, Any]]:
    """从 body 的"## 事实"章节抽取 facts。"""
    facts: List[Dict[str, Any]] = []
    in_facts = False
    current_key: Optional[str] = None
    current_value: Optional[str] = None
    current_evidence: Optional[str] = None

    for line in body.splitlines():
        s = line.rstrip()
        if s.startswith("## 事实"):
            in_facts = True
            continue
        if s.startswith("## ") and in_facts:
            # 进入下一章节
            in_facts = False
            if current_key:
                facts.append({
                    "key": current_key,
                    "value": (current_value or "").strip(),
                    "evidence": (current_evidence or "").strip(),
                })
            break
        if not in_facts:
            continue
        m = re.match(r"^###\s+(.+)$", s)
        if m:
            # 上一个 key 收尾
            if current_key:
                facts.append({
                    "key": current_key,
                    "value": (current_value or "").strip(),
                    "evidence": (current_evidence or "").strip(),
                })
            current_key = m.group(1).strip()
            current_value = None
            current_evidence = None
        elif s.startswith("- **值**:"):
            current_value = s.split(":", 1)[1].strip()
        elif s.startswith("- **原文依据**:"):
            current_evidence = s.split(":", 1)[1].strip()

    # 收尾
    if current_key and in_facts:
        facts.append({
            "key": current_key,
            "value": (current_value or "").strip(),
            "evidence": (current_evidence or "").strip(),
        })
    return facts


def load_wiki_page(rel_path: str) -> Optional[WikiPage]:
    """加载一个 wiki md 文件。返回 None 如果不存在。"""
    p = WIKI_ROOT / rel_path
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    facts = _parse_facts_from_body(body)
    return WikiPage(path=p.relative_to(WIKI_ROOT), fm=fm, body=body, facts=facts)


def find_existing_page(entity_type: str, slug: str, product: str = "wangzhe") -> Optional[WikiPage]:
    """根据 product + entity_type + slug 找既有 wiki 页。"""
    type_to_dir = {
        "hero": "20-英雄", "pet": "20-精灵",
        "skill": "30-技能", "mechanism": "30-机制",
        "item": "40-道具", "clothing": "40-服装",
        "summoner_spell": "80-召唤师技能", "equipment": "90-局内装备",
        "quest": "50-任务",
        "map": "60-地图",
        "overview": "10-产品概述",
        "stub": "99-待审核",
    }
    sub = type_to_dir.get(entity_type, "99-待审核")
    candidate = WIKI_ROOT / product / sub / f"{slug}.md"
    if candidate.exists():
        return load_wiki_page(str(candidate.relative_to(WIKI_ROOT)))
    return None


# ---------------------------------------------------------------------------
# 规则比对
# ---------------------------------------------------------------------------


def _is_numerical_key(key: str) -> bool:
    """判断 key 是不是数值/日期型（需要严格校验）。"""
    kl = key.lower()
    return any(kw in kl for kw in ["cooldown", "damage", "price", "date", "数值", "冷却", "伤害", "价格"])


def _extract_numbers(text: str) -> List[str]:
    return re.findall(r"\d+(?:\.\d+)?", text or "")


def rule_diff(
    new_facts: List[Dict[str, Any]],
    old_facts: List[Dict[str, Any]],
) -> DiffResult:
    """
    规则级 diff：逐 key 比对，识别冲突和补充。
    """
    if not old_facts:
        return DiffResult(action="no_overlap", reason="wiki 既有页无事实可比较")
    if not new_facts:
        return DiffResult(action="no_overlap", reason="新抽取无事实可比较")

    old_map = {f.get("key", ""): f for f in old_facts}
    conflicting: List[str] = []
    appended: List[str] = []

    for nf in new_facts:
        key = nf.get("key", "")
        new_value = str(nf.get("value", "")).strip()
        if not key or not new_value:
            continue
        if key not in old_map:
            # 新 key：append
            appended.append(key)
            continue
        old_value = str(old_map[key].get("value", "")).strip()
        if new_value == old_value:
            continue
        # 数值字段：进一步比数字
        if _is_numerical_key(key):
            old_nums = _extract_numbers(old_value)
            new_nums = _extract_numbers(new_value)
            if old_nums and new_nums and old_nums == new_nums:
                continue
        conflicting.append(key)

    if not conflicting and not appended:
        return DiffResult(action="replace", reason="所有事实一致")
    if not conflicting and appended:
        return DiffResult(
            action="append",
            reason=f"新增字段: {', '.join(appended[:5])}{'...' if len(appended) > 5 else ''}",
            appended_keys=appended,
        )
    if conflicting and not appended:
        return DiffResult(
            action="conflict",
            reason=f"字段值冲突: {', '.join(conflicting[:5])}{'...' if len(conflicting) > 5 else ''}",
            conflicting_keys=conflicting,
            confidence_penalty=0.2,
        )
    return DiffResult(
        action="conflict",
        reason=f"既有冲突 ({len(conflicting)} 个) 又有新增 ({len(appended)} 个)",
        conflicting_keys=conflicting,
        appended_keys=appended,
        confidence_penalty=0.2,
    )


# ---------------------------------------------------------------------------
# LLM 兜底：语义比对
# ---------------------------------------------------------------------------


_LLM_DIFF_PROMPT = """你是"产品知识库"的多源对齐审核员。
你将收到：
1. **wiki 既有页**（来自上一次成功的摄入）
2. **新抽取的事实**（来自当前这次摄入）

请判断新事实和既有页的关系，输出严格 JSON：

```json
{
  "action": "no_overlap|append|replace|conflict",
  "reason": "简短说明决策原因",
  "conflicting_keys": ["key1", "key2"],
  "appended_keys": ["key3"]
}
```

判断规则：
- `no_overlap`：两边讲的是不同主题
- `append`：新事实补充了旧页未覆盖的字段（无矛盾）
- `replace`：新事实覆盖了旧页的同字段，且**严格更好**（更新/更准确/更详细）—— 这种情况下给出建议
- `conflict`：存在字段值不一致；新值与旧值都"看起来对"且无法判定谁对谁错

注意：
- 数值/日期类字段（如冷却、价格、上线日期）的细微差异要特别小心
- 措辞不同但**含义相同**的不算 conflict
- 宁可 conflict 也不要误判 replace

下面是输入：
"""


def llm_diff(
    existing: WikiPage,
    new_extracted: Dict[str, Any],
    client: Optional[LlamaCppClient] = None,
) -> DiffResult:
    """LLM 兜底判定。"""
    if client is None:
        client = LlamaCppClient()

    # 把既有页和新抽取都序列化成纯文本
    existing_text = (
        f"## 既有 wiki 页（type={existing.type}, slug={existing.slug}）\n"
        f"frontmatter: {existing.fm}\n"
        f"facts: {existing.facts}"
    )
    new_text = (
        f"## 新抽取事实\n"
        f"entity_type: {new_extracted.get('entity_type')}\n"
        f"slug: {new_extracted.get('slug')}\n"
        f"title: {new_extracted.get('title')}\n"
        f"facts: {new_extracted.get('facts')}"
    )
    user = f"{existing_text}\n\n{new_text}\n\n请输出 JSON："

    try:
        resp = client.generate_with_system(
            _LLM_DIFF_PROMPT, user, temperature=0.1, max_tokens=1500
        )
        parsed = parse_json_safe(resp.content)
        if isinstance(parsed, dict):
            action = str(parsed.get("action", "conflict"))
            if action not in {"no_overlap", "append", "replace", "conflict"}:
                action = "conflict"
            return DiffResult(
                action=action,
                reason=str(parsed.get("reason", ""))[:500],
                conflicting_keys=list(parsed.get("conflicting_keys", []) or []),
                appended_keys=list(parsed.get("appended_keys", []) or []),
                confidence_penalty=0.2 if action == "conflict" else 0.0,
            )
    except Exception as e:
        print(f"  [diff] LLM 兜底失败: {e}")
    # 兜底：标记 conflict
    return DiffResult(action="conflict", reason="LLM 兜底失败，默认按冲突处理", confidence_penalty=0.2)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def decide_action(
    new_extracted: Dict[str, Any],
    client: Optional[LlamaCppClient] = None,
    use_llm_fallback: bool = True,
    product: str = "wangzhe",
) -> Tuple[DiffResult, Optional[WikiPage]]:
    """
    对一个新抽取的条目，决定它和 wiki 既有页的关系。
    返回 (DiffResult, existing_page_or_None)
    """
    entity_type = str(new_extracted.get("entity_type", "stub"))
    slug = str(new_extracted.get("slug", "")).strip().lower()
    if not slug:
        return DiffResult(action="no_overlap", reason="无 slug"), None

    existing = find_existing_page(entity_type, slug, product=product)
    if existing is None:
        return DiffResult(action="no_overlap", reason="无既有页"), None

    # 1) 规则比对
    rule_result = rule_diff(new_extracted.get("facts", []) or [], existing.facts)
    if rule_result.action != "conflict":
        return rule_result, existing

    # 2) 规则判定 conflict 时，LLM 兜底
    if use_llm_fallback:
        llm_result = llm_diff(existing, new_extracted, client=client)
        return llm_result, existing

    return rule_result, existing
