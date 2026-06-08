"""
lib_crawl.py — 通用爬虫框架（不带专属 selector）

设计目标：
- 接收 URL 列表（来自 config/urls.json 或命令行）
- 自动 UA / 限速 / 错误重试 / 增量（state/last_crawl.json 记录每源最后时间）
- 落地到 raw/wangzhe/{source}/{date}/{slug}.{html|txt}
- 不做内容解析 —— 解析由 ingest.py 调用 LLM 完成（LLM 比 selector 鲁棒得多）
- **两层爬取**：先抓聚合页，再用 LLM 兜底过滤子页 URL（取前 N 个）

数据源例子：
- 搜狗百科：https://baike.sogou.com/v152719307.htm （只抓聚合页）
- 萌娘百科：https://mzh.moegirl.org.cn/王者荣耀 （抓聚合页+30个英雄子页）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = _PROJECT_ROOT / "raw"
STATE_DIR = _PROJECT_ROOT / "state"
CONFIG_DIR = _PROJECT_ROOT / "config"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    # 注意：不要带具体 Chrome 版本号，萌娘百科对详细 UA 有反爬
)


@dataclass
class CrawlTarget:
    """单个爬取目标"""

    source: str  # 来源名，如 "sogou-baike"
    url: str
    product: str = "wangzhe"  # 产品目录
    extra_headers: Dict[str, str] = field(default_factory=dict)
    # 两层爬取配置：是否要从此页发现子页并继续抓
    discover_subpages: bool = False
    subpage_max: int = 30  # 子页最多抓几个
    subpage_url_pattern: Optional[str] = None  # 简单白名单 regex，如 r"王者荣耀:[^/]+$"


@dataclass
class CrawlResult:
    """单次爬取结果"""

    target: CrawlTarget
    saved_path: Optional[Path]
    status: str  # "ok" | "skipped" | "failed"
    message: str = ""
    bytes_written: int = 0


# ---------------------------------------------------------------------------
# 增量状态管理
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    p = STATE_DIR / "last_crawl.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / "last_crawl.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _state_key(target: CrawlTarget) -> str:
    return f"{target.source}:{target.url}"


def should_skip(target: CrawlTarget, min_interval_min: int = 60) -> bool:
    """增量：若该 URL 在 min_interval_min 分钟内已成功爬过，则跳过。"""
    state = _load_state()
    key = _state_key(target)
    record = state.get(key)
    if not record or record.get("status") != "ok":
        return False
    last_ts = dt.datetime.fromisoformat(record["last_crawled"])
    return (dt.datetime.now() - last_ts).total_seconds() < min_interval_min * 60


def mark_crawled(target: CrawlTarget, path: Path, status: str, msg: str = "") -> None:
    state = _load_state()
    state[_state_key(target)] = {
        "last_crawled": dt.datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "saved_to": str(path.relative_to(_PROJECT_ROOT)) if path else None,
        "bytes": path.stat().st_size if path and path.exists() else 0,
        "message": msg,
    }
    _save_state(state)


# ---------------------------------------------------------------------------
# URL → 文件名
# ---------------------------------------------------------------------------


def _url_to_slug(url: str) -> str:
    """从 URL 生成稳定 slug。优先用末尾路径，去除非字母数字字符。"""
    # 取 path 部分
    m = re.search(r"://[^/]+(/[^?#]*)?", url)
    path = (m.group(1) if m else url).strip("/")
    if not path:
        return hashlib.md5(url.encode()).hexdigest()[:12]
    # 去掉扩展名
    slug = re.sub(r"\.[a-zA-Z0-9]+$", "", path)
    # 替换分隔符
    slug = slug.replace("/", "_").replace("%", "")
    # 非 ASCII 字符编码后取哈希后缀
    safe = re.sub(r"[^A-Za-z0-9_\-]", "", slug)
    if not safe:
        safe = hashlib.md5(url.encode()).hexdigest()[:12]
    return safe[:60]


# ---------------------------------------------------------------------------
# 抓取主流程
# ---------------------------------------------------------------------------


def fetch_url(
    target: CrawlTarget,
    timeout: int = 30,
    retries: int = 3,
    backoff: float = 2.0,
) -> Optional[bytes]:
    """带 UA / 重试 / 指数退避的 HTTP GET。返回原始 bytes 或 None。"""
    headers = {"User-Agent": DEFAULT_UA, **target.extra_headers}
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(target.url, headers=headers, timeout=timeout)
            # 显式 4xx/5xx 抛错
            resp.raise_for_status()
            # 简单判断内容是否有效
            if len(resp.content) < 500:
                raise ValueError(f"响应过短（{len(resp.content)} 字节），可能不是真实页面")
            return resp.content
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
    print(f"  [crawl] {target.url} 抓取失败：{last_err}")
    return None


def html_to_text(html_bytes: bytes) -> str:
    """HTML → 纯文本（不依赖额外 NLP 库，只用 BeautifulSoup）。

    使用 html5lib 解析器：萌娘百科（MediaWiki）的页面用默认 html.parser 解析时，
    中文字符会被错误识别为 TemplateString 导致 get_text() 返回空字符串。
    """
    html = html_bytes.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html5lib")
    # 移除 script / style / noscript / iframe
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    # 拿到 body 文本（如果存在）
    text = soup.get_text(separator="\n")
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def save_crawl(
    target: CrawlTarget,
    raw_bytes: bytes,
    text: Optional[str] = None,
) -> Path:
    """把抓到的内容存到 raw/{product}/{source}/{date}/{slug}.{html|txt}

    同时保存原始 HTML 和提取后的纯文本。
    """
    today = dt.date.today().isoformat()
    out_dir = RAW_ROOT / target.product / target.source / today
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _url_to_slug(target.url)

    html_path = out_dir / f"{slug}.html"
    html_path.write_bytes(raw_bytes)

    if text is None:
        text = html_to_text(raw_bytes)
    txt_path = out_dir / f"{slug}.txt"
    txt_path.write_text(text, encoding="utf-8")

    return txt_path


def crawl_one(
    target: CrawlTarget,
    min_interval_min: int = 60,
    force: bool = False,
) -> CrawlResult:
    """爬取一个目标（带增量检查）。"""
    if not force and should_skip(target, min_interval_min=min_interval_min):
        return CrawlResult(
            target=target,
            saved_path=None,
            status="skipped",
            message=f"距上次成功爬取不足 {min_interval_min} 分钟，跳过",
        )

    raw = fetch_url(target)
    if raw is None:
        mark_crawled(target, None, "failed", "HTTP 抓取失败")
        return CrawlResult(target=target, saved_path=None, status="failed", message="抓取失败")

    try:
        text = html_to_text(raw)
        path = save_crawl(target, raw, text)
    except Exception as e:
        mark_crawled(target, None, "failed", f"保存失败: {e}")
        return CrawlResult(target=target, saved_path=None, status="failed", message=f"保存失败: {e}")

    mark_crawled(target, path, "ok")
    return CrawlResult(
        target=target,
        saved_path=path,
        status="ok",
        bytes_written=path.stat().st_size,
    )


# ---------------------------------------------------------------------------
# 两层爬取：聚合页 → 子页发现 → 抓子页
# ---------------------------------------------------------------------------


def _extract_subpage_hrefs(html_bytes: bytes, base_url: str) -> List[str]:
    """
    从聚合页 HTML 中提取候选子页 URL。
    规则：
      - href 在 title="王者荣耀*"/title="本产品*" 等带产品前缀的 <a> 标签内
      - 排除 self、edit、redlink、css/js/icon/锚点
      - 绝对化（拼上 base_url 的 host）
    """
    from urllib.parse import urljoin, urlparse, unquote

    html = html_bytes.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    candidates: List[str] = []
    seen: set = set()

    # 收集所有 a 标签
    for a in soup.find_all("a", href=True):
        title = a.get("title", "")
        href = a.get("href", "")
        if not title or not href:
            continue
        # 只看含"王者荣耀"标题的（可按需调整）
        if "王者荣耀" not in title:
            continue
        # 排除 edit / redlink / 锚点
        if "action=edit" in href or "redlink" in href or href.startswith("#"):
            continue
        # 绝对化
        if href.startswith("//"):
            href = parsed_base.scheme + ":" + href
        elif href.startswith("/"):
            href = base_origin + href
        elif not href.startswith("http"):
            continue
        # 排除 self
        full = href.split("#")[0]
        if full == base_url.split("#")[0]:
            continue
        # 排除跨域
        if not full.startswith(base_origin):
            continue
        # 排除 css/js/png/jpg 等静态资源
        if re.search(r"\.(css|js|json|xml|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf)(\?|$)", full, re.IGNORECASE):
            continue
        if full in seen:
            continue
        seen.add(full)
        candidates.append(full)
    return candidates


def _llm_filter_subpages(
    candidates: List[str],
    base_url: str,
    max_keep: int,
    llm_client=None,
) -> List[str]:
    """
    LLM 兜底：让 LLM 在候选 URL 列表中标记"是英雄详情页"的项。
    为减少 LLM 调用成本，先做简单规则过滤（如要求 URL 形如 王者荣耀:xxx）。
    """
    from urllib.parse import unquote, urlparse

    if not candidates:
        return []

    # 规则预过滤：URL decode 后含"王者荣耀:"（冒号=英雄）/"王者荣耀/"（斜杠=分类）的优先
    pref = []
    rest = []
    for url in candidates:
        decoded = unquote(url)
        if "王者荣耀:" in decoded or "王者荣耀/" in decoded:
            pref.append(url)
        else:
            rest.append(url)

    # 截断到 LLM 能处理的规模
    pool = pref[:200]
    print(f"  [subpage] 规则预过滤后 {len(pool)} 个候选（rest {len(rest)} 个）")

    if not pool:
        return []

    # 简化：暂时跳过 LLM 调用，先用启发式——只要 URL 含"王者荣耀:"且不含地区/版本关键字
    region_kw = ["城", "地区", "学院", "赛季", "版本", "背景", "故事", "设定", "地图", "模式", "玩法", "铭文", "装备", "技能", "皮肤", "更新", "公告", "新闻", "活动", "手册", "设定", "词条"]
    heroes = []
    for url in pool:
        decoded = unquote(url)
        if "王者荣耀:" not in decoded:
            continue
        # 提取冒号后的名字
        m = re.search(r"王者荣耀:(.+)$", decoded)
        if not m:
            continue
        name = m.group(1)
        # 过滤明显是地区/分类的
        if any(kw in name for kw in region_kw):
            continue
        # 过滤空名或过长名字（注意：单字名是合法英雄名，如"瑶"/"澜"/"铠"等，不要过滤！）
        if not name or len(name) > 8:
            continue
        # 过滤明显非人名（纯数字、含括号等）
        if re.search(r"[\(\)\[\]0-9]", name):
            continue
        heroes.append(url)
        if len(heroes) >= max_keep:
            break
    return heroes


def crawl_with_subpages(
    target: CrawlTarget,
    min_interval_min: int = 60,
    force: bool = False,
    llm_client=None,
) -> List[CrawlResult]:
    """
    两层爬取：先抓聚合页，再用启发式过滤子页 URL，最后逐个抓子页。
    """
    results: List[CrawlResult] = []
    # 1) 抓聚合页
    main_result = crawl_one(target, min_interval_min=min_interval_min, force=force)
    results.append(main_result)
    if main_result.status != "ok" or main_result.saved_path is None:
        return results
    if not target.discover_subpages:
        return results

    # 2) 从保存的 HTML 提取子页 URL
    # 注：raw 文件夹里我们存了 .html 原始文件吗？save_crawl 同时存了 html 和 txt。
    # 读回 html
    html_path = main_result.saved_path.with_suffix(".html")
    if not html_path.exists():
        print(f"  [subpage] 找不到原始 HTML: {html_path}")
        return results
    html_bytes = html_path.read_bytes()
    candidates = _extract_subpage_hrefs(html_bytes, target.url)
    print(f"  [subpage] 从 {target.url} 提取到 {len(candidates)} 个候选子页 URL")

    # 3) LLM/启发式过滤
    filtered = _llm_filter_subpages(candidates, target.url, target.subpage_max, llm_client)
    print(f"  [subpage] 过滤后保留 {len(filtered)} 个子页 URL")

    # 4) 逐个抓
    for i, sub_url in enumerate(filtered, 1):
        # 子页用 source + "_sub" 后缀
        sub_source = f"{target.source}_sub"
        sub_target = CrawlTarget(
            source=sub_source,
            url=sub_url,
            product=target.product,
        )
        print(f"  [subpage {i}/{len(filtered)}] 抓取: {sub_url[:80]}")
        sub_result = crawl_one(sub_target, min_interval_min=min_interval_min, force=force)
        results.append(sub_result)
        # 友好限速
        if i < len(filtered):
            time.sleep(1.0)

    return results


# ---------------------------------------------------------------------------
# URL 列表加载
# ---------------------------------------------------------------------------


def load_targets_from_config(config_path: Optional[Path] = None) -> List[CrawlTarget]:
    """从 config/urls.json 加载 URL 列表。

    json 格式示例：
    [
      {
        "source": "sogou-baike",
        "url": "https://baike.sogou.com/v152719307.htm",
        "product": "wangzhe"
      },
      {
        "source": "moegirl-baike",
        "url": "https://mzh.moegirl.org.cn/王者荣耀",
        "product": "wangzhe"
      }
    ]
    """
    if config_path is None:
        config_path = CONFIG_DIR / "urls.json"
    if not config_path.exists():
        return []
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return [CrawlTarget(**item) for item in data]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="通用爬虫 — URL → raw/")
    parser.add_argument("--config", type=str, help="URL 配置文件路径（相对项目根）")
    parser.add_argument("--url", type=str, action="append", help="单个 URL（可多次指定）")
    parser.add_argument("--source", type=str, default="manual-url", help="URL 的 source 名")
    parser.add_argument("--product", type=str, default="wangzhe", help="产品目录名")
    parser.add_argument("--force", action="store_true", help="忽略增量，强制重新抓取")
    parser.add_argument("--min-interval-min", type=int, default=60, help="最小重抓间隔（分钟）")
    parser.add_argument("--with-subpages", action="store_true", help="聚合页抓取后自动发现子页")
    parser.add_argument("--subpage-max", type=int, default=30, help="子页最多抓 N 个")
    args = parser.parse_args()

    targets: List[CrawlTarget] = []
    if args.url:
        for u in args.url:
            targets.append(CrawlTarget(
                source=args.source, url=u, product=args.product,
                discover_subpages=args.with_subpages, subpage_max=args.subpage_max,
            ))
    if args.config:
        cfg = _PROJECT_ROOT / args.config
        targets.extend(load_targets_from_config(cfg))
    if not targets:
        parser.print_help()
        print("\n提示: 把 URL 列表写到 config/urls.json 也可。")
        return

    print(f"待爬取 {len(targets)} 个聚合目标（含子页递归）")
    for t in targets:
        results = crawl_with_subpages(
            t, min_interval_min=args.min_interval_min, force=args.force
        )
        for r in results:
            rel = r.saved_path.relative_to(_PROJECT_ROOT) if r.saved_path else "(无)"
            print(f"  [{r.status}] {r.target.source:20s} {r.target.url[:60]:60s}")
            print(f"           → {rel}  ({r.bytes_written}B)  {r.message}")


if __name__ == "__main__":
    main()
