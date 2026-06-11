"""
scripts/parse_hero_skins.py — 从萌娘百科英雄子页 raw txt 抽取皮肤数据

解析"皮肤一览"章节, 生成 40-皮肤/per-hero/{slug}.md

用法:
  python -m scripts.parse_hero_skins          # 仅报告
  python -m scripts.parse_hero_skins --write  # 写 wiki 文件
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"
RAW_SUB = (
    _PROJECT_ROOT / "raw/wangzhe/moegirl-baike_sub/2026-06-08"
)

# 英雄 slug 映射: raw 文件名前缀 -> wiki slug
# raw 文件名形如 E78E8BE88085E88DA3E88080E69D8EE799BD.txt (URL编码)
# 对应的 wiki 页在 wiki/wangzhe/20-英雄/li-bai.md (从 wiki 目录查)
def _load_hero_slug_map() -> Dict[str, str]:
    """读 wiki/wangzhe/20-英雄/ 下所有 md, 建 title→slug 映射。"""
    hero_dir = WIKI_ROOT / "wangzhe/20-英雄"
    m: Dict[str, str] = {}
    if not hero_dir.exists():
        return m
    for md in hero_dir.glob("*.md"):
        if md.name == "CLAUDE.md":
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        m2 = re.match(r"---\n(.*?)\n---", text, re.DOTALL)
        if not m2:
            continue
        fm = yaml.safe_load(m2.group(1))
        title = fm.get("title", "")
        slug = fm.get("slug", "")
        if title and slug:
            m[title] = slug
    return m


def extract_skins_from_text(raw_text: str) -> List[Dict[str, str]]:
    """
    从"皮肤一览"章节抽取皮肤列表。
    返回 [{name, name_en, rarity, description, acquisition, release_date, features}]
    """
    idx = raw_text.find("皮肤一览")
    if idx < 0:
        return []
    section = raw_text[idx : idx + 30000]  # 取后 30K 字符

    skins: List[Dict[str, str]] = []
    # 按"稀有度"字段把每段切出来
    # 每个皮肤块结构:
    #   皮肤名
    #   皮肤名/EnglishName
    #   稀有度：XX
    #   介绍文字：XX
    #   获取方式：XX (可选)
    #   上架时间：XX (可选)
    #   相关皮肤：XX (可选)
    #   特性\n外观：XX (可选)

    # 策略: 按"稀有度"分割, 每块的标题是前 1-2 行的文本
    blocks = re.split(r'\n稀有度\s*[：:]\s*', section)
    if len(blocks) <= 1:
        return []

    # 第一块是"皮肤一览"标题, 跳过
    blocks = blocks[1:]

    for block in blocks:
        # block 内容: 第一行是稀有度值
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) < 1:
            continue

        rarity_val = lines[0]  # 稀有度值本身
        # 皮肤名在前面的 lines (被 split 丢失了, 实际上在稀有度行之前)
        # 我们的 split 策略丢失了皮肤名... 需要改策略

    return []  # 先退出, 调整策略
