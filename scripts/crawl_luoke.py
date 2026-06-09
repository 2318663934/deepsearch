"""
crawl_luoke.py — 洛克王国世界 B 站游戏 wiki 专用批量爬虫

不走 lib_crawl 的 _llm_filter_subpages(那个是王者荣耀的启发式,不适配洛克),
直接用 MediaWiki API 的 categorymembers 端点拉子页清单,然后逐个 ?action=raw 抓。

用法:
  # 1) 拉取分类:精灵 的所有精灵页 + ingest
  python -m scripts.crawl_luoke --category 精灵 --limit 1000 --action-raw

  # 2) 也支持其他分类
  python -m scripts.crawl_luoke --category 技能 --limit 500
  python -m scripts.crawl_luoke --category 道具 --limit 200
  python -m scripts.crawl_luoke --category 任务 --limit 200
  python -m scripts.crawl_luoke --category 机制 --limit 100
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.parse
from pathlib import Path
from typing import List, Optional, Set

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = _PROJECT_ROOT / "raw"
STATE_DIR = _PROJECT_ROOT / "state"

API_BASE = "https://wiki.biligame.com/rocom/api.php"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
)


def _api_get(params: dict) -> dict:
    """调一次 MediaWiki API。"""
    headers = {"User-Agent": DEFAULT_UA}
    resp = requests.get(API_BASE, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_category_members(category: str, limit: int = 1000) -> List[dict]:
    """拉取分类下所有成员(分页用 cmcontinue)。"""
    members: List[dict] = []
    cmcontinue: Optional[str] = None
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"分类:{category}",
            "cmlimit": min(500, limit - len(members)),
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        data = _api_get(params)
        chunk = data.get("query", {}).get("categorymembers", [])
        members.extend(chunk)
        print(f"  [api] 已拉 {len(members)}/{limit} 个")
        if len(members) >= limit:
            break
        cmcontinue = data.get("continue", {}).get("cmcontinue")
        if not cmcontinue:
            break
    return members[:limit]


def _url_to_slug(url: str) -> str:
    """复用 lib_crawl._url_to_slug 风格:用末尾路径,去除非字母数字。"""
    import re, hashlib
    m = re.search(r"://[^/]+(/[^?#]*)?", url)
    path = (m.group(1) if m else url).strip("/")
    if not path:
        return hashlib.md5(url.encode()).hexdigest()[:12]
    slug = re.sub(r"\.[a-zA-Z0-9]+$", "", path)
    slug = slug.replace("/", "_").replace("%", "")
    safe = re.sub(r"[^A-Za-z0-9_\-]", "", slug)
    if not safe:
        safe = hashlib.md5(url.encode()).hexdigest()[:12]
    return safe[:60]


def fetch_page_raw(title: str, product: str = "luoke", source: str = "bilibili-wiki",
                   delay_sec: float = 2.0) -> Optional[Path]:
    """抓一个页面的 ?action=raw 内容,落到 raw/{product}/{source}/{date}/{slug}.wikitext。"""
    url_title = urllib.parse.quote(title)
    url = f"https://wiki.biligame.com/rocom/{url_title}?action=raw"
    headers = {"User-Agent": DEFAULT_UA}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [fail] {title}: {e}")
        return None

    # 不存 action=raw 端点返回的 wikitext(通常 < 几 KB),只存 .wikitext 文件
    today = dt.date.today().isoformat()
    out_dir = RAW_ROOT / product / source / today
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _url_to_slug(url)
    path = out_dir / f"{slug}.wikitext"
    try:
        wt = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        wt = resp.content.decode("utf-8", errors="ignore")
    path.write_text(wt, encoding="utf-8")
    time.sleep(delay_sec)  # 礼貌爬取
    return path


def main():
    parser = argparse.ArgumentParser(description="洛克王国世界 B 站 wiki 批量爬虫")
    parser.add_argument("--category", required=True, help="分类名,如 '精灵'/'技能'/'道具'/'任务'")
    parser.add_argument("--limit", type=int, default=1000, help="最多抓 N 个")
    parser.add_argument("--source", default="bilibili-wiki", help="raw 下的 source 子目录名")
    parser.add_argument("--delay", type=float, default=2.0, help="每个 URL 抓取后 sleep 时长(秒)")
    parser.add_argument("--action-raw", action="store_true", default=True, help="抓 ?action=raw(默认开启)")
    parser.add_argument("--skip-titles", nargs="*", default=[], help="跳过的标题(精确匹配)")
    args = parser.parse_args()

    print(f"=== 洛克爬虫: 分类:{args.category}, 最多 {args.limit} 个 ===")
    members = fetch_category_members(args.category, limit=args.limit)
    print(f"分类 {args.category} 共有 {len(members)} 个成员")

    # 已抓过的(title → path)记忆,跨次重跑用
    seen: Set[str] = set()
    state_path = STATE_DIR / "luoke_crawled.json"
    if state_path.exists():
        seen = set(json.loads(state_path.read_text(encoding="utf-8")))
    print(f"已抓过 {len(seen)} 个(跨次记忆)")

    written = 0
    skipped = 0
    failed = 0
    skipped_existing = 0
    today = dt.date.today().isoformat()
    out_dir = RAW_ROOT / "luoke" / args.source / today
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, m in enumerate(members, 1):
        title = m["title"]
        if title in args.skip_titles:
            print(f"  [{i}/{len(members)}] skip-titles: {title}")
            skipped += 1
            continue
        if title in seen:
            print(f"  [{i}/{len(members)}] already: {title}")
            skipped_existing += 1
            continue
        url_title = urllib.parse.quote(title)
        slug = _url_to_slug(f"https://wiki.biligame.com/rocom/{url_title}?action=raw")
        # 检查今天的 raw 目录是否已存在
        if (out_dir / f"{slug}.wikitext").exists():
            print(f"  [{i}/{len(members)}] on-disk: {title}")
            seen.add(title)
            skipped_existing += 1
            continue
        print(f"  [{i}/{len(members)}] {title[:40]}")
        path = fetch_page_raw(title, product="luoke", source=args.source, delay_sec=args.delay)
        if path:
            seen.add(title)
            written += 1
        else:
            failed += 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
    print(f"\n=== 完成: 写入 {written}, 跳过 {skipped}(skip-titles), {skipped_existing}(已存在), 失败 {failed} ===")
    print(f"状态已保存: {state_path}")


if __name__ == "__main__":
    main()
