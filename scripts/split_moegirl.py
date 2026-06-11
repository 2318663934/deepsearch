"""
split_moegirl.py — 把萌娘百科聚合页 txt 按章节拆分为片段,喂养 ingest

用法:
  python -m scripts.split_moegirl            # 仅拆分
  python -m scripts.split_moegirl --ingest   # 拆分 + 批量 ingest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

SOURCE_TXT = (
    _PROJECT_ROOT
    / "raw/wangzhe/moegirl-baike/2026-06-08/E78E8BE88085E88DA3E88080.txt"
)

OUT_DIR = (
    _PROJECT_ROOT / "raw/wangzhe/moegirl-baike-sections" / dt.date.today().isoformat()
)

# (slug, entity_type, title_hint, line_start, line_end)
# line_end 可以超过实际行数,会被 clamp
SECTIONS: List[Tuple[str, str, str, int, int]] = [
    # === 游戏模式 ===
    ("5v5-wangzhe-xiagu", "mechanism", "5v5王者峡谷", 727, 785),
    ("10v10-zhongxing-xiagu", "mechanism", "10v10众星峡谷", 727, 785),
    ("3v3-changping", "mechanism", "3v3长平攻防战", 727, 785),
    ("1v1-mojia", "mechanism", "1v1墨家机关道", 727, 785),
    ("zhengzhao-moshi", "mechanism", "征召模式", 727, 785),
    ("wanxiang-tiangong", "mechanism", "万象天工", 727, 830),
    ("wuxian-luan-dou", "mechanism", "无限乱斗", 830, 900),
    ("mengjing-daluan-dou", "mechanism", "梦境大乱斗", 830, 900),
    ("kelong-dazuozhan", "mechanism", "克隆大作战", 830, 900),
    ("huoyan-shan", "mechanism", "火焰山大战", 760, 785),
    ("bianjing-tuwei", "mechanism", "边境突围", 775, 800),
    ("juexing-zhi-zhan", "mechanism", "觉醒之战", 820, 850),

    # === 对局系统 ===
    ("mingwen-system", "mechanism", "铭文系统", 463, 500),
    ("zhaohuanshi-skill", "mechanism", "召唤师技能", 483, 500),
    ("junei-equipment", "mechanism", "局内装备", 490, 540),
    ("yingxiong-position", "mechanism", "英雄定位分类", 239, 330),
    ("skin-system", "mechanism", "皮肤系统", 390, 460),

    # === 通用机制(技能/战斗相关) ===
    ("cooldown-system", "mechanism", "冷却缩减机制", 463, 760),  # 从对局系统到游戏模式
    ("attack-speed", "mechanism", "攻速机制", 463, 760),
    ("damage-boost", "mechanism", "增伤机制", 463, 760),
    ("damage-reduction", "mechanism", "减伤机制", 463, 760),
    ("shield-mechanism", "mechanism", "护盾机制", 463, 760),
    ("control-mechanism", "mechanism", "控制机制", 463, 760),
    ("lifesteal-mechanism", "mechanism", "吸血机制", 463, 760),
    ("critical-mechanism", "mechanism", "暴击机制", 463, 760),
    ("penetration-mechanism", "mechanism", "穿透机制", 463, 760),
    ("movement-speed", "mechanism", "移速机制", 463, 760),

    # === 衍生作品 ===
    ("wangzhe-world", "overview", "王者荣耀世界(衍生游戏)", 2184, 2250),
    ("star-breaker", "overview", "星之破晓(衍生游戏)", 2184, 2250),
    ("derivative-anime", "overview", "衍生动画(王者别闹/碎月篇/命运篇)", 2184, 2250),
    ("derivative-music", "overview", "衍生音乐/歌曲", 2184, 2250),
    ("kpl-esports", "overview", "KPL职业联赛", 2249, 2400),
    ("world-championship", "overview", "世界冠军杯", 2249, 2400),
]


def split_sections() -> List[Tuple[str, str, str, Path]]:
    """拆分原文,返回 [(entity_type, slug, title, output_path)]。"""
    if not SOURCE_TXT.exists():
        print(f"源文件不存在: {SOURCE_TXT}")
        return []

    lines = SOURCE_TXT.read_text(encoding="utf-8").splitlines()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for slug, entity_type, title, start, end in SECTIONS:
        start = max(0, start - 1)  # 0-indexed
        end = min(len(lines), end)
        chunk = "\n".join(lines[start:end])
        if len(chunk.strip()) < 100:
            print(f"  [skip] {slug}: 片段太短({len(chunk.strip())} 字)")
            continue

        # 每个片段写入独立 raw 文件
        out_path = OUT_DIR / f"{slug}.txt"
        out_path.write_text(chunk, encoding="utf-8")
        results.append((entity_type, slug, title, out_path))
        print(f"  [ok] {slug}: {len(chunk.strip())} chars -> {out_path.name}")

    print(f"\n拆分完成: {len(results)} 个片段 -> {OUT_DIR}")
    return results


def ingest_all(sections: List[Tuple[str, str, str, Path]], dry_run: bool = False):
    """对拆分后的片段批量 ingest。"""
    from scripts.ingest import ingest_one
    from scripts.lib_llm import LlamaCppClient

    client = LlamaCppClient()
    stats = {"written": 0, "replaced": 0, "review": 0, "conflict": 0, "failed": 0, "skipped": 0}

    for i, (entity_type, slug, title, path) in enumerate(sections, 1):
        print(f"\n[{i}/{len(sections)}] {slug} ({entity_type})")
        if dry_run:
            print(f"  [dry-run] 将 ingest: {path} entity={entity_type} slug={slug}")
            continue

        try:
            res = ingest_one(
                path, client,
                product="wangzhe",
                entity_type_override=entity_type,
                slug_override=slug,
            )
            status = res.get("status", "unknown")
            if status in stats:
                stats[status] += 1
            else:
                stats["failed"] += 1
            print(f"  -> {status} (conf={res.get('confidence', 'N/A')})")
        except Exception as e:
            print(f"  [error] {e}")
            stats["failed"] += 1

    print("\n=== ingest 结果 ===")
    for k, v in sorted(stats.items()):
        if v:
            print(f"  {k}: {v}")


def main():
    parser = argparse.ArgumentParser(description="萌娘百科章节拆分 + 批量 ingest")
    parser.add_argument("--ingest", action="store_true", help="拆分后自动 ingest")
    parser.add_argument("--dry-run", action="store_true", help="只预览不执行")
    args = parser.parse_args()

    sections = split_sections()

    if args.ingest and sections:
        if args.dry_run:
            ingest_all(sections, dry_run=True)
        else:
            ingest_all(sections)


if __name__ == "__main__":
    main()
