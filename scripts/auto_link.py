"""
auto_link.py — 自动从 wiki body 提取其他页 slug/title,加入 related 字段

策略:对每个 .md(除 99-待审核),扫描 body 找所有其他 wiki 页的 slug/title,
加入 related 字段(不破坏已有)。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"


def _split_fm(text: str):
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


def build_index(product: str) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """扫描 product 下所有 wiki 页,返回 title→path, slug→path, path→product_prefix。"""
    title_to_path: Dict[str, str] = {}
    slug_to_path: Dict[str, str] = {}
    path_to_subdir: Dict[str, str] = {}
    root = WIKI_ROOT / product
    if not root.exists():
        return title_to_path, slug_to_path, path_to_subdir
    for md in root.rglob("*.md"):
        if "99-待审核" in str(md):
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        fm, _ = _split_fm(text)
        rel = str(md.relative_to(WIKI_ROOT)).replace("\\", "/")
        title = str(fm.get("title", "")).strip()
        slug = str(fm.get("slug", "")).strip()
        subdir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if title:
            title_to_path[title] = rel
        if slug:
            slug_to_path[slug] = rel
        if subdir:
            path_to_subdir[rel] = subdir
    return title_to_path, slug_to_path, path_to_subdir


def auto_link_product(product: str) -> int:
    title_to_path, slug_to_path, path_to_subdir = build_index(product)
    if not title_to_path:
        return 0
    root = WIKI_ROOT / product
    n_updated = 0
    for md in root.rglob("*.md"):
        if "99-待审核" in str(md):
            continue
        rel = str(md.relative_to(WIKI_ROOT)).replace("\\", "/")
        text = md.read_text(encoding="utf-8")
        fm, body = _split_fm(text)
        if not fm:
            continue
        my_subdir = path_to_subdir.get(rel, "")
        existing = fm.get("related") or []
        if not isinstance(existing, list):
            existing = [str(existing)]
        existing_set = set(str(x) for x in existing)
        # body 中找 title/slug
        mentioned: Set[str] = set()
        for title, other_path in title_to_path.items():
            if not title or other_path == rel:
                continue
            if len(title) < 2:
                continue
            if title in body:
                mentioned.add(other_path)
        for slug, other_path in slug_to_path.items():
            if not slug or other_path == rel:
                continue
            if slug in body:
                mentioned.add(other_path)
        # 同子目录的不需要互引(子目录 CLAUDE.md 覆盖)
        mentioned = {m for m in mentioned if path_to_subdir.get(m, "") != my_subdir}
        new_links = mentioned - existing_set
        if not new_links:
            continue
        fm["related"] = sorted(existing_set | mentioned)
        fm["updated"] = _now_iso()
        new_text = f"---\n{_render_fm(fm)}---\n\n{body}"
        md.write_text(new_text, encoding="utf-8")
        n_updated += 1
    return n_updated


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def main():
    parser = argparse.ArgumentParser(description="自动从 body 提取引用加到 related")
    parser.add_argument("--product", required=True)
    args = parser.parse_args()
    n = auto_link_product(args.product)
    print(f"{args.product}: {n} 个文件加了新 related")


if __name__ == "__main__":
    main()
