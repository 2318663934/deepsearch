"""
lint.py — wiki 健康检查

5 类检查（来自 CLAUDE.md §8）：
1. 矛盾检测：同 slug 文件的相同字段值冲突
2. 过期检测：last_verified 超过 90 天的文件 → 警告
3. 孤立页：不被任何 related 引用、且不被 00-索引/CLAUDE.md 列出的页面
4. 缺失交叉引用：出现英雄/机制名但未在 related 中建立链接
5. frontmatter 完整性：必填字段缺失

不自动修复，只输出可读告警。

用法：
  python -m scripts.lint            # 完整检查 + 文本报告
  python -m scripts.lint --json    # JSON 输出
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"

STALE_DAYS = 90  # last_verified 超过 90 天视为过期

REQUIRED_FM_FIELDS = [
    "title", "type", "slug", "sources", "confidence",
    "created", "updated", "last_verified", "status", "version",
]

TYPE_TO_DIR = {
    "hero": "20-英雄",
    "skill": "30-技能机制",
    "overview": "10-产品概述",
    "stub": "99-待审核",
}

# 反向映射：目录名 → type（用于从路径推断 type）
DIR_TO_TYPE = {v: k for k, v in TYPE_TO_DIR.items()}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    level: str  # "error" | "warning" | "info"
    category: str  # "contradiction" | "stale" | "orphan" | "missing_ref" | "fm_incomplete"
    path: str  # 相对 WIKI_ROOT
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WikiFile:
    rel_path: str
    fm: Dict[str, Any]
    body: str
    facts: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _split_fm(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2).rstrip()


def _parse_facts(body: str) -> List[Dict[str, Any]]:
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
            if current_key:
                facts.append({"key": current_key, "value": (current_value or "").strip(), "evidence": (current_evidence or "").strip()})
            in_facts = False
            current_key = None
            continue
        if not in_facts:
            continue
        m = re.match(r"^###\s+(.+)$", s)
        if m:
            if current_key:
                facts.append({"key": current_key, "value": (current_value or "").strip(), "evidence": (current_evidence or "").strip()})
            current_key = m.group(1).strip()
            current_value = None
            current_evidence = None
        elif s.startswith("- **值**:"):
            current_value = s.split(":", 1)[1].strip()
        elif s.startswith("- **原文依据**:"):
            current_evidence = s.split(":", 1)[1].strip()
    if current_key and in_facts:
        facts.append({"key": current_key, "value": (current_value or "").strip(), "evidence": (current_evidence or "").strip()})
    return facts


def load_all_wiki() -> Dict[str, WikiFile]:
    files: Dict[str, WikiFile] = {}
    for md in WIKI_ROOT.rglob("*.md"):
        rel = str(md.relative_to(WIKI_ROOT))
        if rel.startswith("99-待审核"):
            # 待审核目录是临时区，不纳入 lint 主体（但矛盾检查可以纳入）
            pass
        text = md.read_text(encoding="utf-8")
        fm, body = _split_fm(text)
        facts = _parse_facts(body)
        files[rel] = WikiFile(rel_path=rel, fm=fm, body=body, facts=facts)
    return files


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------


def check_fm_incomplete(files: Dict[str, WikiFile]) -> List[Alert]:
    alerts: List[Alert] = []
    for rel, wf in files.items():
        if rel.startswith("99-待审核"):
            continue
        missing = [f for f in REQUIRED_FM_FIELDS if f not in wf.fm]
        if missing:
            alerts.append(Alert(
                level="error",
                category="fm_incomplete",
                path=rel,
                message=f"frontmatter 缺字段: {', '.join(missing)}",
                details={"missing": missing},
            ))
    return alerts


def check_stale(files: Dict[str, WikiFile]) -> List[Alert]:
    alerts: List[Alert] = []
    cutoff = dt.datetime.now() - dt.timedelta(days=STALE_DAYS)
    for rel, wf in files.items():
        if rel.startswith("99-待审核"):
            continue
        lv = wf.fm.get("last_verified")
        if not lv:
            continue
        try:
            ts = dt.datetime.fromisoformat(str(lv).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            alerts.append(Alert(
                level="warning", category="stale", path=rel,
                message=f"last_verified 解析失败: {lv}",
            ))
            continue
        if ts.replace(tzinfo=None) < cutoff:
            days = (dt.datetime.now() - ts.replace(tzinfo=None)).days
            alerts.append(Alert(
                level="warning", category="stale", path=rel,
                message=f"已 {days} 天未校验（阈值 {STALE_DAYS} 天）",
                details={"last_verified": lv, "days": days},
            ))
    return alerts


def check_contradictions(files: Dict[str, WikiFile]) -> List[Alert]:
    """同 type+slug 视为同一实体，跨多源（不同 path）矛盾检查。"""
    alerts: List[Alert] = []
    # 按 (type, slug) 分组
    groups: Dict[Tuple[str, str], List[WikiFile]] = defaultdict(list)
    for rel, wf in files.items():
        t = str(wf.fm.get("type", ""))
        s = str(wf.fm.get("slug", ""))
        if not t or not s:
            continue
        groups[(t, s)].append(wf)
    for (t, s), group in groups.items():
        if len(group) < 2:
            continue
        # 收集所有 facts by key
        key_values: Dict[str, List[Tuple[str, WikiFile]]] = defaultdict(list)
        for wf in group:
            for f in wf.facts:
                key = f.get("key", "")
                val = str(f.get("value", "")).strip()
                if key and val:
                    key_values[key].append((val, wf))
        for key, items in key_values.items():
            distinct = set(v for v, _ in items)
            if len(distinct) > 1:
                # 数值字段特殊处理：取数字比较
                nums = set()
                for v, _ in items:
                    nums.update(re.findall(r"\d+(?:\.\d+)?", v))
                if len(nums) <= 1:
                    continue
                sources = [wf.rel_path for _, wf in items]
                alerts.append(Alert(
                    level="error", category="contradiction", path=sources[0],
                    message=f"字段 `{key}` 在 {len(sources)} 个源中冲突: {sorted(distinct)[:3]}",
                    details={"key": key, "values": sorted(distinct), "sources": sources},
                ))
    return alerts


def check_orphans(files: Dict[str, WikiFile]) -> List[Alert]:
    """孤立页：不被 related 引用、不被索引列出。"""
    alerts: List[Alert] = []
    # 1) 收集所有 related 引用
    referenced: Set[str] = set()
    for wf in files.values():
        rels = wf.fm.get("related") or []
        if isinstance(rels, list):
            for r in rels:
                referenced.add(str(r))
    # 2) 收集索引页提到的（00-索引/CLAUDE.md 的内容）
    index_path = "00-索引/CLAUDE.md"
    indexed: Set[str] = set()
    if index_path in files:
        idx_body = files[index_path].body
        # 提取形如 `subdir/slug.md` 的提及
        for m in re.finditer(r"`([\w\-]+/[\w\-]+\.md)`", idx_body):
            indexed.add(m.group(1))
    # 3) 检查
    for rel, wf in files.items():
        if rel.startswith("99-待审核"):
            continue
        if rel == index_path:
            continue
        if rel not in referenced and rel not in indexed:
            alerts.append(Alert(
                level="info", category="orphan", path=rel,
                message="未被 related 引用，也未被索引列出",
            ))
    return alerts


def check_missing_refs(files: Dict[str, WikiFile]) -> List[Alert]:
    """缺失交叉引用：body 中出现其他页面的 title/slug 但没在 related 中。"""
    alerts: List[Alert] = []
    # 1) 建 slug/title 索引
    title_to_path: Dict[str, str] = {}
    slug_to_path: Dict[str, str] = {}
    for rel, wf in files.items():
        if rel.startswith("99-待审核"):
            continue
        title = str(wf.fm.get("title", "")).strip()
        slug = str(wf.fm.get("slug", "")).strip()
        if title:
            title_to_path[title] = rel
        if slug:
            slug_to_path[slug] = rel
    # 2) 对每个文件 body，检查是否提到其他 title/slug
    for rel, wf in files.items():
        if rel.startswith("99-待审核"):
            continue
        body = wf.body
        related = set(wf.fm.get("related") or [])
        mentioned: Set[str] = set()
        for title, other_path in title_to_path.items():
            if not title or other_path == rel:
                continue
            if len(title) < 2:  # 太短易误判
                continue
            if title in body:
                mentioned.add(other_path)
        for slug, other_path in slug_to_path.items():
            if not slug or other_path == rel:
                continue
            if slug in body:
                mentioned.add(other_path)
        # 减去自身和已 related 的
        missing = mentioned - {rel} - related
        if missing:
            alerts.append(Alert(
                level="info", category="missing_ref", path=rel,
                message=f"body 提到但未建立 related: {sorted(missing)[:3]}",
                details={"missing": sorted(missing)},
            ))
    return alerts


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_text(alerts: List[Alert], files: Dict[str, WikiFile]) -> str:
    if not alerts:
        return f"✓ 所有 {len(files)} 个 wiki 文件通过 5 类检查，无告警。\n"

    by_cat: Dict[str, List[Alert]] = defaultdict(list)
    for a in alerts:
        by_cat[a.category].append(a)

    cat_names = {
        "contradiction": "矛盾检测",
        "stale": "过期检测",
        "orphan": "孤立页",
        "missing_ref": "缺失交叉引用",
        "fm_incomplete": "frontmatter 完整性",
    }

    lines: List[str] = []
    lines.append(f"=== Wiki 健康检查报告 ===")
    lines.append(f"扫描文件: {len(files)} 个")
    lines.append(f"告警总数: {len(alerts)}\n")

    by_level: Dict[str, int] = defaultdict(int)
    for a in alerts:
        by_level[a.level] += 1
    lines.append(f"  error:   {by_level.get('error', 0)}")
    lines.append(f"  warning: {by_level.get('warning', 0)}")
    lines.append(f"  info:    {by_level.get('info', 0)}\n")

    for cat in ["contradiction", "stale", "fm_incomplete", "orphan", "missing_ref"]:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"## {cat_names.get(cat, cat)} ({len(items)} 条)")
        for a in items[:20]:  # 每类最多列 20 条
            icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}[a.level]
            lines.append(f"  {icon} [{a.path}]")
            lines.append(f"      {a.message}")
        if len(items) > 20:
            lines.append(f"  ... 省略 {len(items) - 20} 条")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Wiki 健康检查")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    files = load_all_wiki()
    if not files:
        print("未发现任何 wiki 文件")
        return

    alerts: List[Alert] = []
    alerts.extend(check_fm_incomplete(files))
    alerts.extend(check_stale(files))
    alerts.extend(check_contradictions(files))
    alerts.extend(check_orphans(files))
    alerts.extend(check_missing_refs(files))

    if args.json:
        print(json.dumps(
            {
                "scanned_files": len(files),
                "alert_count": len(alerts),
                "alerts": [a.__dict__ for a in alerts],
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render_text(alerts, files))

    # exit code: error 数 > 0 则非零
    errors = sum(1 for a in alerts if a.level == "error")
    sys.exit(1 if errors > 0 else 0)


if __name__ == "__main__":
    main()
