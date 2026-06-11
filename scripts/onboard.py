"""
scripts/onboard.py — 新产品上线流水线(WebSearch → 爬取 → 抽取 → 入仓)

流程:
  1. 产品名 → DuckDuckGo 搜索相关页面(Wiki/百科/萌娘)
  2. 下载找到的 URL, 存到 raw/{product_slug}/{source}/{date}/
  3. 调 ingest.py 抽取 + 门控(≥0.7 入 wiki, 0.4-0.7 入 99-待审, <0.4 丢弃)
  4. 返回入仓统计

用法:
  python -m scripts.onboard --product "原神" --max-urls 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urlparse

import requests
from ddgs import DDGS

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = _PROJECT_ROOT / "raw"
WIKI_ROOT = _PROJECT_ROOT / "wiki"
CONFIG_DIR = _PROJECT_ROOT / "config"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 搜索时优先的站点模式(匹配 URL 域名)
PREFERRED_DOMAINS = [
    "wiki.biligame.com",   # B站游戏 wiki
    "mzh.moegirl.org.cn",  # 萌娘百科
    "baike.baidu.com",     # 百度百科
    "baike.sogou.com",     # 搜狗百科
    "zh.wikipedia.org",    # 中文维基
]

# 拒绝的 URL 模式(非内容页)
SKIP_URL_PATTERNS = [
    r"/wiki/[^/]+:[^/]+$",  # MediaWiki 特殊页
    r"action=",
    r"oldid=",
    r"diff=",
    r"\.(png|jpg|gif|svg|css|js|json|xml|ico)",
]


def _product_slug(name: str) -> str:
    """产品名 → 文件系统安全的英文 slug。"""
    from pypinyin import lazy_pinyin
    s = "-".join(lazy_pinyin(name)).lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unknown-product"


def search_sources(product_name: str, max_results: int = 10) -> List[Dict[str, str]]:
    """
    用 DuckDuckGo 搜索产品相关页面。
    返回 [{title, url, snippet}]。
    """
    queries = [
        f"{product_name} wiki",
        f"{product_name} 萌娘百科",
        f"{product_name} 百度百科",
    ]
    seen_urls: Set[str] = set()
    results: List[Dict[str, str]] = []

    with DDGS() as ddgs:
        for q in queries:
            try:
                for r in ddgs.text(q, max_results=max_results):
                    url = r.get("href", "")
                    if not url:
                        continue
                    # 去重
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    # 跳过明显非内容页
                    if any(re.search(p, url) for p in SKIP_URL_PATTERNS):
                        continue
                    results.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "snippet": r.get("body", ""),
                    })
                    if len(results) >= max_results:
                        break
            except Exception as e:
                print(f"  [search] {q}: {e}")
                continue
            if len(results) >= max_results:
                break

    # 优先排序: 优先域名在前
    def _score(r: dict) -> int:
        domain = urlparse(r["url"]).netloc
        for i, pref in enumerate(PREFERRED_DOMAINS):
            if pref in domain:
                return i
        return len(PREFERRED_DOMAINS)

    results.sort(key=_score)
    return results[:max_results]


def download_page(url: str, product: str, source: str) -> Optional[Path]:
    """下载一个页面,保存原始 HTML + 提取纯文本 TXT。返回 txt 路径。"""
    today = dt.date.today().isoformat()
    out_dir = RAW_ROOT / product / source / today
    out_dir.mkdir(parents=True, exist_ok=True)

    # 生成文件名: URL 末尾 path 的 hash
    slug = re.sub(r"[^A-Za-z0-9_\-]", "", urlparse(url).path.strip("/").replace("/", "_"))[:60]
    if not slug:
        import hashlib
        slug = hashlib.md5(url.encode()).hexdigest()[:12]

    txt_path = out_dir / f"{slug}.txt"
    if txt_path.exists():
        print(f"    [exists] {txt_path.name}")
        return txt_path

    try:
        resp = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [fail] {url[:60]}: {e}")
        return None

    # 简单 HTML→text
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: List[str] = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style", "noscript"}:
                self.skip += 1

        def handle_endtag(self, tag):
            if tag in {"script", "style", "noscript"} and self.skip > 0:
                self.skip -= 1
            if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4"}:
                self.parts.append("\n")

        def handle_data(self, data):
            if self.skip == 0 and data.strip():
                self.parts.append(data.strip())

    html = resp.content.decode("utf-8", errors="ignore")
    ex = _TextExtractor()
    ex.feed(html)
    text = " ".join(ex.parts)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 200:
        print(f"    [short] {url[:60]}: {len(text)} chars")
        return None

    txt_path.write_text(text, encoding="utf-8")
    time.sleep(1.0)  # 礼貌爬取
    return txt_path


def run_onboard(
    product_name: str,
    product_slug: Optional[str] = None,
    max_urls: int = 10,
    auto_ingest: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    主入口: 搜索 → 下载 → ingest → 返回统计。

    Returns:
        {
            "product": product_name,
            "slug": product_slug,
            "raw_dir": str,
            "urls_found": int,
            "downloaded": int,
            "ingest": {written, review, discarded, failed} or None,
        }
    """
    if product_slug is None:
        product_slug = _product_slug(product_name)

    print(f"\n{'='*60}")
    print(f"  新产品上线: {product_name} (slug={product_slug})")
    print(f"{'='*60}\n")

    # 1. 搜索
    print("[1/3] 搜索中...")
    sources = search_sources(product_name, max_results=max_urls)
    print(f"  找到 {len(sources)} 个页面")
    if verbose:
        for s in sources[:5]:
            print(f"  - {s['title'][:50]} | {s['url'][:60]}")

    if not sources:
        return {
            "product": product_name,
            "slug": product_slug,
            "error": "未找到任何有效页面",
        }

    # 2. 下载
    print("\n[2/3] 下载中...")
    downloaded = 0
    for i, s in enumerate(sources, 1):
        domain = urlparse(s["url"]).netloc.replace(".", "-")
        source_name = f"web-{domain[:30]}"
        print(f"  [{i}/{len(sources)}] {s['title'][:40]}")
        path = download_page(s["url"], product_slug, source_name)
        if path:
            downloaded += 1

    if downloaded == 0:
        return {
            "product": product_name,
            "slug": product_slug,
            "urls_found": len(sources),
            "downloaded": 0,
            "error": "所有页面下载失败",
        }

    # 3. Ingest
    result: Dict[str, Any] = {
        "product": product_name,
        "slug": product_slug,
        "urls_found": len(sources),
        "downloaded": downloaded,
    }

    if auto_ingest:
        print("\n[3/3] Ingest 中...")
        from scripts.ingest import ingest_one
        from scripts.lib_llm import LlamaCppClient

        client = LlamaCppClient()
        # 找 raw 目录下刚下载的文件
        raws: List[Path] = []
        raw_root = RAW_ROOT / product_slug
        if raw_root.exists():
            raws = sorted(raw_root.rglob("*.txt"))

        stats = {"written": 0, "review": 0, "discarded": 0, "failed": 0}
        for rp in raws:
            try:
                res = ingest_one(rp, client, product=product_slug)
                status = res.get("status", "unknown")
                if status in ("written", "replaced"):
                    stats["written"] += 1
                elif status == "review":
                    stats["review"] += 1
                elif status == "discarded":
                    stats["discarded"] += 1
                else:
                    stats["failed"] += 1
            except Exception as e:
                print(f"  [ingest error] {rp.name}: {e}")
                stats["failed"] += 1
        result["ingest"] = stats

    return result


def _ensure_product_in_config(product_slug: str, product_name: str) -> None:
    """在 config/urls.json 中加新产品的空块(如果不存在)。"""
    config_path = CONFIG_DIR / "urls.json"
    if not config_path.exists():
        return
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if "products" in cfg:
        for block in cfg["products"]:
            if block.get("name") == product_slug:
                return  # 已存在
        cfg["products"].append({
            "name": product_slug,
            "urls": [],
        })
    else:
        cfg = {"products": [{"name": product_slug, "urls": []}]}
    config_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  已注册到 config/urls.json")


def main():
    parser = argparse.ArgumentParser(description="新产品上线流水线")
    parser.add_argument("--product", required=True, help="产品名")
    parser.add_argument("--slug", default=None, help="产品 slug(默认 pinyin)")
    parser.add_argument("--max-urls", type=int, default=10, help="最多搜索 N 个页面")
    parser.add_argument("--no-ingest", action="store_true", help="只下载不抽取")
    parser.add_argument("--quiet", action="store_true", help="安静模式")
    args = parser.parse_args()

    slug = args.slug or _product_slug(args.product)
    _ensure_product_in_config(slug, args.product)

    result = run_onboard(
        product_name=args.product,
        product_slug=slug,
        max_urls=args.max_urls,
        auto_ingest=not args.no_ingest,
        verbose=not args.quiet,
    )

    print("\n" + "=" * 60)
    print("  上线结果:")
    for k, v in result.items():
        print(f"  {k}: {v}")

    if result.get("ingest"):
        ing = result["ingest"]
        print(f"    入仓: {ing['written']} | 待审: {ing['review']} | 丢弃: {ing['discarded']} | 失败: {ing['failed']}")


if __name__ == "__main__":
    main()
