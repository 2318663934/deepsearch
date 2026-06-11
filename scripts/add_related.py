"""
add_related.py — 批量给 wiki 页加 related 字段,消除孤立页警告

策略:
1. 给 20-精灵/ 下每个精灵页加 related: [20-精灵/CLAUDE.md, 00-索引/CLAUDE.md, 10-产品概述/luoke-guowang-shijie.md]
2. 给 10-产品概述/luoke-guowang-shijie.md 加 related(已有,不重复)
3. 给 00-索引/CLAUDE.md 加 related(已有,不重复)
4. 重建 20-精灵/CLAUDE.md 含每个精灵的提及

用法:
  python -m scripts.add_related --product luoke
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import List

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"


def _split_fm(text: str):
    """分离 frontmatter 和 body。"""
    if not text.startswith("---\n"):
        return {}, text
    m = re.search(r"\n---\n", text[4:])
    if not m:
        return {}, text
    fm_text = text[4 : 4 + m.start()]
    body = text[4 + m.end() + 1 :]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        fm = {}
    return fm, body


def _render_fm(fm: dict) -> str:
    return yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)


def add_related_to_pets(product: str, entity_type: str, sub: str) -> int:
    """给 sub/ 下所有 .md(除 CLAUDE.md)加 related 字段。"""
    root = WIKI_ROOT / product / sub
    if not root.exists():
        return 0
    related = [
        f"{sub}/CLAUDE.md",
        "00-索引/CLAUDE.md",
        "10-产品概述/luoke-guowang-shijie.md",
    ]
    n = 0
    for md in root.glob("*.md"):
        if md.name == "CLAUDE.md":
            continue
        text = md.read_text(encoding="utf-8")
        fm, body = _split_fm(text)
        if not fm:
            continue
        # 跳过已存在 related
        if fm.get("related"):
            existing = fm["related"] if isinstance(fm["related"], list) else [fm["related"]]
            existing_set = set(str(x) for x in existing)
            new_set = set(related) - existing_set
            if not new_set:
                continue  # 已完整
            fm["related"] = sorted(existing_set | set(related))
        else:
            fm["related"] = related
        fm["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        new_text = f"---\n{_render_fm(fm)}---\n\n{body}"
        md.write_text(new_text, encoding="utf-8")
        n += 1
    return n


def rebuild_pet_index(product: str, sub: str) -> int:
    """重建 sub/CLAUDE.md,列出所有精灵(主形态 + 异形态)。"""
    root = WIKI_ROOT / product / sub
    if not root.exists():
        return 0
    pets = sorted(md.stem for md in root.glob("*.md") if md.name != "CLAUDE.md")
    main_forms = [p for p in pets if "-" not in p or not any(
        suf in p for suf in ("de-yang-zi", "qiu-xing-tai", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "one")
    )]
    forms = [p for p in pets if p not in main_forms]

    lines = [
        "---",
        "title: 20-精灵",
        "type: stub",
        "slug: pets",
        "sources:",
        "- manual",
        "confidence: 1.0",
        "confidence_reason: 系统自动维护",
        f"created: '2026-06-10T10:30:00+08:00'",
        f"updated: '2026-06-10T10:30:00+08:00'",
        "last_verified: '2026-06-10T10:30:00+08:00'",
        "status: verified",
        "version: 1",
        "related:",
        "  - 00-索引/CLAUDE.md",
        "  - 10-产品概述/luoke-guowang-shijie.md",
        "---",
        "",
        "# 20-精灵(洛克王国世界)",
        "",
        "本目录收录洛克王国世界所有具体精灵条目。每条精灵包含:主属性/2属性/种族值(生命/物攻/魔攻/物防/魔防/速度)/特性/分布地区/技能/进化条件。",
        "",
        f"## 已收录({len(pets)} 条)",
        "",
    ]
    for p in main_forms[:50]:
        lines.append(f"- `{p}.md`")
    if len(main_forms) > 50:
        lines.append(f"- ... 共 {len(main_forms)} 条主形态,更多见 `*.md`")
    if forms:
        lines.append("")
        lines.append(f"## 异形态({len(forms)} 条)")
        for f in forms[:20]:
            lines.append(f"- `{f}.md`")
        if len(forms) > 20:
            lines.append(f"- ... 共 {len(forms)} 条异形态")

    text = "\n".join(lines) + "\n"
    (root / "CLAUDE.md").write_text(text, encoding="utf-8")
    return len(pets)


def main():
    parser = argparse.ArgumentParser(description="批量加 related 字段")
    parser.add_argument("--product", required=True)
    parser.add_argument("--sub", default="20-精灵")
    args = parser.parse_args()

    n1 = add_related_to_pets(args.product, "pet", args.sub)
    print(f"加 related 字段: {n1} 个精灵页")
    n2 = rebuild_pet_index(args.product, args.sub)
    print(f"重建 {args.sub}/CLAUDE.md: 含 {n2} 条精灵")


# ---------------------------------------------------------------------------
# 通用版本: 给任一 sub 下所有 .md(非 CLAUDE)加 related 字段
# ---------------------------------------------------------------------------


def add_related_generic(product: str, sub: str, related_paths: List[str]) -> int:
    """给 sub/ 下所有 .md(除 CLAUDE.md)加 related 字段。"""
    root = WIKI_ROOT / product / sub
    if not root.exists():
        return 0
    n = 0
    for md in root.glob("*.md"):
        if md.name == "CLAUDE.md":
            continue
        text = md.read_text(encoding="utf-8")
        fm, body = _split_fm(text)
        if not fm:
            continue
        existing = fm.get("related") or []
        if not isinstance(existing, list):
            existing = [str(existing)]
        existing_set = set(str(x) for x in existing)
        new_set = set(related_paths) - existing_set
        if not new_set:
            continue
        fm["related"] = sorted(existing_set | set(related_paths))
        fm["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        new_text = f"---\n{_render_fm(fm)}---\n\n{body}"
        md.write_text(new_text, encoding="utf-8")
        n += 1
    return n


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        # 批量模式: 给 30-技能/40-道具/50-任务/40-服装 全加 related
        for sub in ["30-技能", "40-道具", "50-任务", "40-服装"]:
            related = [f"{sub}/CLAUDE.md", "00-索引/CLAUDE.md", "10-产品概述/luoke-guowang-shijie.md"]
            n = add_related_generic("luoke", sub, related)
            print(f"{sub}: {n} 个文件加 related")
    else:
        main()
