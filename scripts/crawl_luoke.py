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
                   delay_sec: float = 2.0, max_retries: int = 4) -> Optional[Path]:
    """抓一个页面的 ?action=raw 内容,落到 raw/{product}/{source}/{date}/{slug}.wikitext。

    遇到 5xx/567 反爬时指数退避重试。
    """
    url_title = urllib.parse.quote(title)
    url = f"https://wiki.biligame.com/rocom/{url_title}?action=raw"
    headers = {"User-Agent": DEFAULT_UA}

    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code in (502, 503, 504, 567, 429):
                # 限速/服务端错误,退避重试
                backoff = 2.0 ** attempt
                print(f"  [retry {attempt+1}/{max_retries}] {title[:30]}: HTTP {resp.status_code}, 退避 {backoff:.1f}s")
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            # 成功后落盘
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
        except requests.exceptions.HTTPError as e:
            last_err = e
            if attempt < max_retries - 1:
                backoff = 2.0 ** attempt
                print(f"  [retry {attempt+1}/{max_retries}] {title[:30]}: {e}, 退避 {backoff:.1f}s")
                time.sleep(backoff)
                continue
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                backoff = 2.0 ** attempt
                print(f"  [retry {attempt+1}/{max_retries}] {title[:30]}: {e}, 退避 {backoff:.1f}s")
                time.sleep(backoff)
                continue
    print(f"  [fail] {title}: {last_err}")
    return None


def main():
    parser = argparse.ArgumentParser(description="洛克王国世界 B 站 wiki 批量爬虫")
    parser.add_argument("--category", default=None, help="分类名,如 '精灵'/'技能'/'道具'/'任务'(与 --titles-file 二选一)")
    parser.add_argument("--limit", type=int, default=1000, help="最多抓 N 个")
    parser.add_argument("--source", default="bilibili-wiki", help="raw 下的 source 子目录名")
    parser.add_argument("--delay", type=float, default=2.0, help="每个 URL 抓取后 sleep 时长(秒)")
    parser.add_argument("--action-raw", action="store_true", default=True, help="抓 ?action=raw(默认开启)")
    parser.add_argument("--skip-titles", nargs="*", default=[], help="跳过的标题(精确匹配)")
    parser.add_argument("--titles-file", default=None, help="本地 JSON 列表(从 state/luoke_*.json 读),与 --category 互斥")
    parser.add_argument("--start-from", type=int, default=0, help="从列表第 N 个开始(用于分批)")
    args = parser.parse_args()

    # 从本地 JSON 或 API 拿列表
    if args.titles_file:
        import json
        members = json.loads(Path(args.titles_file).read_text(encoding="utf-8"))
        # 兼容多种 list 格式
        if isinstance(members, dict) and "query" in members:
            members = members["query"]["categorymembers"]
        members = [{"title": m["title"] if isinstance(m, dict) else m} for m in members]
        members = members[args.start_from:]
        print(f"=== 洛克爬虫: 从 {args.titles_file} 读 {len(members)} 个 ===")
    else:
        if not args.category:
            print("错误: --category 或 --titles-file 至少给一个")
            return
        print(f"=== 洛克爬虫: 分类:{args.category}, 最多 {args.limit} 个 ===")
        members = fetch_category_members(args.category, limit=args.limit)
    print(f"待处理 {len(members)} 个成员")

    # 已抓过的(title → path)记忆,跨次重跑用
    seen: Set[str] = set()
    state_path = STATE_DIR / "luoke_crawled.json"
    if state_path.exists():
        seen = set(json.loads(state_path.read_text(encoding="utf-8")))
    print(f"已抓过 {len(seen)} 个(跨次记忆)")

    # 过滤 MediaWiki 特殊命名空间(Widget/模板/模块/帮助/分类 等)
    SKIP_NS_PREFIXES = ("Widget:", "模板:", "Template:", "模块:", "Module:",
                        "MediaWiki:", "帮助:", "Help:", "分类:", "Category:",
                        "特殊:", "Special:", "文件:", "File:", "User:", "用户:")
    filtered_members = []
    for m in members:
        t = m["title"]
        if any(t.startswith(p) for p in SKIP_NS_PREFIXES):
            continue
        filtered_members.append(m)
    if len(filtered_members) < len(members):
        print(f"  过滤 MediaWiki 特殊命名空间: {len(members)} → {len(filtered_members)}")
    members = filtered_members

    written = 0
    skipped = 0
    failed = 0
    skipped_existing = 0
    today = dt.date.today().isoformat()
    out_dir = RAW_ROOT / "luoke" / args.source / today
    out_dir.mkdir(parents=True, exist_ok=True)

    # 扫 raw 目录里已存在的 .wikitext(URL 编码 slug 形式),反推已抓过哪些 title
    # 通过 wikitext 内容里的"精灵名称=XXX"反推,这样能精确识别
    def _scan_existing_titles_from_raw() -> Set[str]:
        existing = set()
        for p in out_dir.glob("*.wikitext"):
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            import re as _re
            m = _re.search(r"\|\s*精灵名称\s*=\s*([^\n|]+)", content)
            if m:
                existing.add(m.group(1).strip())
        return existing

    raw_titles = _scan_existing_titles_from_raw()
    print(f"从 raw 目录反推已抓 {len(raw_titles)} 个 title")
    seen.update(raw_titles)

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
            # 每 10 个 save 一次,避免中途被杀丢失 seen
            if written % 10 == 0:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
        else:
            failed += 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
    print(f"\n=== 完成: 写入 {written}, 跳过 {skipped}(skip-titles), {skipped_existing}(已存在), 失败 {failed} ===")
    print(f"状态已保存: {state_path}")


if __name__ == "__main__":
    main()
