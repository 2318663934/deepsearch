"""
ui/app.py — Flask web app: 产品知识库浏览 UI

阶段 A 路由:
  GET  /                                  -> 产品列表
  GET  /product/<name>                    -> 产品详情(子目录列表)
  GET  /product/<name>/<subdir>           -> 子目录列表
  GET  /product/<name>/<subdir>/<slug>    -> 单个 md 详情
  GET  /review                            -> 99-待审核 全局列表

启动:
  python -m scripts.ui_app
  (默认 http://127.0.0.1:5000)
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import markdown
import yaml
from flask import Flask, abort, render_template_string, request, url_for

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"

# 已知产品白名单(避免遍历未知目录)
KNOWN_PRODUCTS = ["wangzhe", "luoke"]

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_fm(text: str) -> Tuple[Dict[str, Any], str]:
    """分离 YAML frontmatter 和 body, 错误时返回空 fm。"""
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


def _list_subdirs(product: str) -> List[Path]:
    """列出产品下所有子目录(按中文章号前缀排序)。"""
    root = WIKI_ROOT / product
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda x: x.name)


def _list_files(product: str, subdir: str) -> List[Dict[str, Any]]:
    """列出子目录下所有 .md 文件, 提取 frontmatter。"""
    root = WIKI_ROOT / product / subdir
    if not root.exists():
        return []
    out = []
    for md in sorted(root.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="ignore")
        fm, _ = _split_fm(text)
        out.append({
            "rel_path": f"{product}/{subdir}/{md.name}",
            "name": md.stem,
            "title": fm.get("title", md.stem),
            "slug": fm.get("slug", ""),
            "type": fm.get("type", ""),
            "confidence": fm.get("confidence"),
            "score": fm.get("score"),
            "updated": fm.get("updated", ""),
        })
    return out


def _read_md(product: str, subdir: str, slug: str) -> Tuple[Dict[str, Any], str, str]:
    """读单个 md 文件, 返回 (fm, body, source_path)。"""
    md = WIKI_ROOT / product / subdir / f"{slug}.md"
    if not md.exists() or not md.is_file():
        abort(404)
    text = md.read_text(encoding="utf-8", errors="ignore")
    fm, body = _split_fm(text)
    return fm, body, str(md.relative_to(_PROJECT_ROOT))


def _render_md(body: str) -> str:
    """渲染 markdown body 为 HTML。"""
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc"],
        output_format="html",
    )
    return md.convert(body)


def _product_stats(product: str) -> Dict[str, int]:
    """统计产品下实体数。"""
    root = WIKI_ROOT / product
    if not root.exists():
        return {}
    stats = {}
    for sub in _list_subdirs(product):
        n = sum(1 for _ in sub.glob("*.md") if not _.name.startswith("CLAUDE"))
        stats[sub.name] = n
    return stats


def _get_product_display_name(product: str) -> str:
    """返回产品的中文显示名(简单映射)。"""
    return {"wangzhe": "王者荣耀", "luoke": "洛克王国世界"}.get(product, product)


def _product_last_crawl(product: str) -> Optional[str]:
    """从 state 文件读该产品最近的成功爬取时间, 用于 UI 展示。"""
    import json as _json
    best_ts: Optional[str] = None

    # 1) state/last_crawl.json — 通过 config/urls.json 找到该产品的 URLs
    state_path = _PROJECT_ROOT / "state" / "last_crawl.json"
    config_path = _PROJECT_ROOT / "config" / "urls.json"
    if state_path.exists() and config_path.exists():
        cfg = _json.loads(config_path.read_text(encoding="utf-8"))
        urls: List[str] = []
        if "products" in cfg:
            for block in cfg["products"]:
                if block.get("name") == product:
                    for u in block.get("urls", []):
                        urls.append(u.get("source", "") + ":" + u.get("url", ""))
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        for key, rec in state.items():
            if any(key.startswith(u) for u in urls):
                ts = rec.get("last_crawled", "")
                if ts and (not best_ts or ts > best_ts):
                    best_ts = ts

    # 2) crawl_luoke 状态文件(洛克专用)
    luoke_state = _PROJECT_ROOT / "state" / "luoke_crawled.json"
    if product == "luoke" and luoke_state.exists():
        try:
            import os
            mtime = os.path.getmtime(str(luoke_state))
            mt = dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            if not best_ts or mt > best_ts:
                best_ts = mt
        except Exception:
            pass
    return best_ts[:19] if best_ts else None


# ---------------------------------------------------------------------------
# 模板(用 {{ content_html|safe }} 占位)
# ---------------------------------------------------------------------------

BASE_TEMPLATE = """
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>知识库 — {{ product_display }}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
  <header class="topbar">
    <a href="{{ url_for('index') }}"><h1>📚 知识库</h1></a>
    <nav>
      <a href="{{ url_for('index') }}">产品</a>
      <a href="{{ url_for('onboard') }}">+上线</a>
      <a href="{{ url_for('raw_stats') }}">raw</a>
      <a href="{{ url_for('review') }}">99-待审 ({{ pending_count }})</a>
    </nav>
  </header>
  <main>
    {{ content_html|safe }}
  </main>
  <footer><p>deepsearch — 知识库系统 | {{ now }}</p></footer>
</body>
</html>
"""


def _count_pending() -> int:
    """统计全局 99-待审 文件数。"""
    n = 0
    for p in WIKI_ROOT.rglob("99-待审核/*.md"):
        n += 1
    return n


def _base_ctx(product: Optional[str] = None) -> Dict[str, Any]:
    """公共模板变量。"""
    return {
        "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "pending_count": _count_pending(),
        "product_display": _get_product_display_name(product) if product else "全部",
    }


# ---------------------------------------------------------------------------
# 路由(用 Python f-string 构建 content_html, 然后传给 BASE 模板)
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """产品列表。"""
    products = []
    for p in KNOWN_PRODUCTS:
        root = WIKI_ROOT / p
        if not root.exists():
            continue
        stats = _product_stats(p)
        total = sum(stats.values())
        last = _product_last_crawl(p)
        products.append({
            "name": p,
            "display": _get_product_display_name(p),
            "total": total,
            "subdirs": stats,
            "last_crawl": last or "暂无记录",
        })

    sub_items = ""
    for p in products:
        sub_list = "".join(
            f"<li>{sub}: {n}</li>"
            for sub, n in p["subdirs"].items()
        )
        sub_items += f"""
        <a class="product-card" href="/product/{p['name']}">
          <h3>{p['display']}</h3>
          <p class="slug">{p['name']}</p>
          <p class="count">累计实体: <strong>{p['total']}</strong></p>
          <p class="muted">最后爬取: {p['last_crawl']}</p>
          <ul class="subdir-list">{sub_list}</ul>
        </a>
        """

    content_html = f"""
    <div class="onboard-hero">
      <h2>搜索并更新知识库</h2>
      <p>输入产品名,系统自动判断是新产品(全量上线)还是已有产品(增量更新),然后搜索 → 爬取 → 抽取 → 入仓。</p>
      <form method="post" action="/onboard" class="onboard-inline">
        <input type="text" name="product_name" placeholder="输入产品名,例如: 王者荣耀、洛克王国世界、原神..." required>
        <input type="text" name="product_slug" placeholder="slug(可选,留空自动识别)">
        <input type="number" name="max_urls" value="10" min="3" max="30" style="width:60px;" title="最多搜索页面数">
        <button type="submit" class="btn-save">🚀 更新知识库</button>
      </form>
    </div>
    <h2>已有产品</h2>
    <div class="product-grid">{sub_items}</div>
    """

    return render_template_string(
        BASE_TEMPLATE,
        content_html=content_html,
        **_base_ctx(None),
    )


@app.route("/onboard", methods=["GET", "POST"])
def onboard():
    """新产品上线: GET 渲染表单, POST 执行搜索+爬取+入仓流水线。"""
    if request.method == "GET":
        content_html = """
        <h2>上线新产品</h2>
        <p>输入产品名,系统将自动搜索、爬取、抽取、入仓。</p>
        <form method="post" action="/onboard" class="edit-form">
          <div class="form-row">
            <label>产品名(中文):</label>
            <input type="text" name="product_name" placeholder="例如: 原神" required>
          </div>
          <div class="form-row">
            <label>产品 slug (可选, 留空自动拼音):</label>
            <input type="text" name="product_slug" placeholder="例如: yuan-shen">
          </div>
          <div class="form-row">
            <label>最多搜索 N 个页面:</label>
            <input type="number" name="max_urls" value="10" min="3" max="30">
          </div>
          <div class="form-actions">
            <button type="submit" class="btn-save">🚀 开始上线</button>
            <a href="/" class="btn-cancel">取消</a>
          </div>
        </form>
        """
        return render_template_string(
            BASE_TEMPLATE, content_html=content_html, **_base_ctx(None),
        )

    # POST: 执行 onboard 流水线(自动判断新产品/已有产品)
    product_name = request.form.get("product_name", "").strip()
    product_slug = request.form.get("product_slug", "").strip() or None
    max_urls = int(request.form.get("max_urls", "10"))

    if not product_name:
        abort(400, "产品名不能为空")

    # 智能判断: 已有产品还是新产品
    is_existing = False
    if product_slug is None:
        # 从 product_name 反向查 slug(支持中文名输入)
        for known_slug, known_display in {
            "wangzhe": "王者荣耀", "luoke": "洛克王国世界",
        }.items():
            if product_name in (known_slug, known_display):
                product_slug = known_slug
                is_existing = True
                break
    else:
        is_existing = product_slug in KNOWN_PRODUCTS

    if product_slug is None:
        # 纯新产品,自动生成 slug
        from scripts.onboard import _product_slug
        product_slug = _product_slug(product_name)

    from scripts.onboard import run_onboard, search_sources, download_page
    from scripts.ingest import ingest_one
    from scripts.lib_llm import LlamaCppClient

    if is_existing:
        # 增量更新: 搜索 → 下载 → 只 ingest 未处理的新文件 → 已有 wiki 走 decide_action
        import json as _json
        mode_label = f"增量更新({product_name})"
        sources = search_sources(product_name, max_results=max_urls)
        downloaded = 0
        for s in sources:
            from urllib.parse import urlparse
            domain = urlparse(s["url"]).netloc.replace(".", "-")
            source_name = f"web-{domain[:30]}"
            path = download_page(s["url"], product_slug, source_name)
            if path:
                downloaded += 1

        # 扫描新 raw 并 ingest
        raw_root = _PROJECT_ROOT / "raw" / product_slug
        new_raws = []
        if raw_root.exists():
            for rp in sorted(raw_root.rglob("*.txt")):
                if not rp.with_suffix(rp.suffix + ".processed").exists():
                    new_raws.append(rp)

        client = LlamaCppClient()
        stats = {"written": 0, "replaced": 0, "review": 0, "discarded": 0, "failed": 0}
        for rp in new_raws:
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
            except Exception:
                stats["failed"] += 1

        content_html = f"""
        <h2>✅ 增量更新完成</h2>
        <p>产品: <strong>{product_name}</strong> (已有, slug: {product_slug})</p>
        <table class="entity-table" style="max-width: 600px;">
          <tr><th>项</th><th>值</th></tr>
          <tr><td>搜索到</td><td>{len(sources)} 个页面</td></tr>
          <tr><td>下载成功</td><td>{downloaded} 个</td></tr>
          <tr><td>新入仓</td><td style="color:#1a7f37"><strong>{stats['written']}</strong></td></tr>
          <tr><td>替换已有</td><td>{stats['replaced']}</td></tr>
          <tr><td>待审核</td><td style="color:#d4a72c">{stats['review']}</td></tr>
          <tr><td>丢弃</td><td style="color:#cf222e">{stats['discarded']}</td></tr>
        </table>
        <p style="margin-top: 16px;">
          <a href="/product/{product_slug}" class="btn-primary">→ 查看 {product_name} 知识库</a>
          <a href="/" class="btn-cancel">返回首页</a>
        </p>
        """
    else:
        # 全新产品: 全量上线
        mode_label = f"全量上线({product_name})"
        result = run_onboard(
            product_name=product_name,
            product_slug=product_slug,
            max_urls=max_urls,
            auto_ingest=True,
            verbose=False,
        )
        slug = result.get("slug", "")
        ing = result.get("ingest") or {}
        error = result.get("error", "")

        if error and error != "所有页面下载失败":
            content_html = f"""
            <h2>⚠️ 上线未完全成功</h2>
            <p>产品: <strong>{product_name}</strong> (slug: {slug})</p>
            <p>错误: {error}</p>
            <a href="/onboard" class="btn-primary">重试</a>
            <a href="/" class="btn-cancel">返回首页</a>
            """
        elif slug and slug not in KNOWN_PRODUCTS:
            KNOWN_PRODUCTS.append(slug)
            content_html = f"""
            <h2>✅ 上线完成</h2>
            <table class="entity-table" style="max-width: 600px;">
              <tr><th>项</th><th>值</th></tr>
              <tr><td>产品名</td><td><strong>{product_name}</strong></td></tr>
              <tr><td>slug</td><td><code>{slug}</code></td></tr>
              <tr><td>搜索到</td><td>{result.get('urls_found', 0)} 个页面</td></tr>
              <tr><td>下载成功</td><td>{result.get('downloaded', 0)} 个</td></tr>
              <tr><td>入仓</td><td style="color:#1a7f37"><strong>{ing.get('written', 0)}</strong></td></tr>
              <tr><td>待审核(0.4-0.7)</td><td style="color:#d4a72c">{ing.get('review', 0)}</td></tr>
              <tr><td>丢弃(&lt;0.4)</td><td style="color:#cf222e">{ing.get('discarded', 0)}</td></tr>
            </table>
            <p style="margin-top: 16px;">
              <a href="/product/{slug}" class="btn-primary">→ 查看 {product_name} 知识库</a>
              <a href="/" class="btn-cancel">返回首页</a>
            </p>
            """
        else:
            content_html = f"""
            <h2>⚠️ 上线未完全成功</h2>
            <p>产品: <strong>{product_name}</strong> (slug: {slug})</p>
            <p>错误: {error or '下载的页面内容不足,无法抽取有效信息'}</p>
            <a href="/onboard" class="btn-primary">重试</a>
            <a href="/" class="btn-cancel">返回首页</a>
            """

    return render_template_string(
        BASE_TEMPLATE, content_html=content_html, **_base_ctx(None),
    )


@app.route("/product/<name>")
def product(name: str):
    """产品详情: 子目录 + 统计。"""
    if name not in KNOWN_PRODUCTS:
        abort(404)
    subdirs = _list_subdirs(name)
    stats = _product_stats(name)
    total = sum(stats.values())

    tiles = "".join(
        f"""
        <li>
          <a href="/product/{name}/{sub.name}">
            <strong>{sub.name}</strong>
            <span class="badge">{stats.get(sub.name, 0)}</span>
          </a>
        </li>
        """
        for sub in subdirs
    )

    content_html = f"""
    <h2>{_get_product_display_name(name)}</h2>
    <p class="slug">slug: {name}</p>
    <p>累计实体: <strong>{total}</strong></p>
    <h3>子目录</h3>
    <ul class="subdir-tiles">{tiles}</ul>
    """

    return render_template_string(
        BASE_TEMPLATE,
        content_html=content_html,
        **_base_ctx(name),
    )


@app.route("/product/<name>/<subdir>")
def subdir(name: str, subdir: str):
    """子目录详情: 实体列表。"""
    if name not in KNOWN_PRODUCTS:
        abort(404)
    files = _list_files(name, subdir)
    if not files:
        abort(404)

    rows = "".join(
        f"""
        <tr>
          <td><a href="/product/{name}/{subdir}/{f['name']}">{f['title']}</a></td>
          <td>{f['type']}</td>
          <td>{("%.2f" % f['confidence']) if f['confidence'] is not None else ''}</td>
          <td class="muted">{(f['updated'] or '')[:10]}</td>
        </tr>
        """
        for f in files
    )

    content_html = f"""
    <h2>{_get_product_display_name(name)} / {subdir}</h2>
    <p>{len(files)} 个实体</p>
    <table class="entity-table">
      <thead>
        <tr><th>title</th><th>type</th><th>confidence</th><th>updated</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """

    return render_template_string(
        BASE_TEMPLATE,
        content_html=content_html,
        **_base_ctx(name),
    )


@app.route("/product/<name>/<subdir>/<slug>")
def entity(name: str, subdir: str, slug: str):
    """单实体详情: frontmatter + markdown body 渲染。"""
    if name not in KNOWN_PRODUCTS:
        abort(404)
    fm, body, source_path = _read_md(name, subdir, slug)
    rendered = _render_md(body)

    fm_rows = "".join(
        f"<tr><th>{k}</th><td>{v}</td></tr>"
        for k, v in fm.items()
    )

    score = fm.get("score")
    if score is not None:
        score_html = f'<span class="score-value">{"★" * int(score)}{"☆" * (5 - int(score))} ({score}/5)</span>'
    else:
        score_html = '<span class="score-value">暂无评分</span>'

    # 5 星打分 UI(form 提交到 /score 端点)
    score_form = f"""
    <form method="post" action="/product/{name}/{subdir}/{slug}/score" class="score-form">
      <span class="score-label">给该实体打分:</span>
      <div class="star-rating">
        {"".join(f'<button type="submit" name="score" value="{i}" class="star-btn">{("★" if (score and i <= int(score)) else "☆")}</button>' for i in range(1, 6))}
      </div>
    </form>
    """

    content_html = f"""
    <h2>{fm.get('title', slug)}</h2>
    <p class="slug">{name}/{subdir}/{slug}.md</p>
    <div class="frontmatter-box">
      <table>{fm_rows}</table>
    </div>
    <div class="score-actions">
      <span class="score-label">当前打分:</span>
      {score_html}
    </div>
    <div class="score-form-box">
      {score_form}
    </div>
    <div class="page-actions">
      <a href="/product/{name}/{subdir}/{slug}/edit" class="btn-edit">✏️ 编辑本文</a>
    </div>
    <div class="markdown-body">
      {rendered}
    </div>
    """

    return render_template_string(
        BASE_TEMPLATE,
        content_html=content_html,
        **_base_ctx(name),
    )


@app.route("/product/<name>/<subdir>/<slug>/edit", methods=["GET", "POST"])
def edit_entity(name: str, subdir: str, slug: str):
    """GET: 渲染编辑表单 (textarea 装 body)。
    POST: 写回 md + git commit。
    """
    if name not in KNOWN_PRODUCTS:
        abort(404)
    md_path = WIKI_ROOT / name / subdir / f"{slug}.md"
    if not md_path.exists():
        abort(404)
    text = md_path.read_text(encoding="utf-8")
    fm, body = _split_fm(text)
    if not fm:
        abort(500, "frontmatter 解析失败")

    if request.method == "GET":
        # 渲染编辑表单
        title = fm.get("title", slug)
        content_html = f"""
        <h2>编辑: {title}</h2>
        <p class="slug">{name}/{subdir}/{slug}.md</p>
        <form method="post" action="/product/{name}/{subdir}/{slug}/edit" class="edit-form">
          <div class="form-row">
            <label>body (Markdown):</label>
            <textarea name="body" rows="30" style="font-family: monospace;">{body}</textarea>
          </div>
          <div class="form-row">
            <label>commit message (可选, 默认 "update: <slug> via UI"):</label>
            <input type="text" name="commit_msg" placeholder="update: 修改说明">
          </div>
          <div class="form-actions">
            <button type="submit" class="btn-save">💾 保存并 git commit</button>
            <a href="/product/{name}/{subdir}/{slug}" class="btn-cancel">取消</a>
          </div>
        </form>
        """
        return render_template_string(
            BASE_TEMPLATE, content_html=content_html, **_base_ctx(name)
        )

    # POST: 写回 + git commit
    new_body = request.form.get("body", "").strip()
    commit_msg = request.form.get("commit_msg", "").strip()
    if not commit_msg:
        commit_msg = f"update: {slug} via UI"

    fm["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    new_text = f"---\n{yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)}---\n\n{new_body}\n"
    md_path.write_text(new_text, encoding="utf-8")

    # git commit
    import subprocess
    rel = str(md_path.relative_to(_PROJECT_ROOT))
    try:
        subprocess.run(
            ["git", "add", rel],
            cwd=str(_PROJECT_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr).lower():
            git_msg = f"git commit 失败: {result.stderr.strip()[:200]}"
        else:
            git_msg = f"已 git commit: {commit_msg}"
    except Exception as e:
        git_msg = f"git 错误: {e}"

    content_html = f"""
    <h2>✓ 已保存</h2>
    <p>{git_msg}</p>
    <p>文件: <code>{rel}</code></p>
    <a href="/product/{name}/{subdir}/{slug}" class="btn-primary">→ 返回详情</a>
    """
    return render_template_string(
        BASE_TEMPLATE, content_html=content_html, **_base_ctx(name)
    )
def score_entity(name: str, subdir: str, slug: str):
    """接收打分表单提交, 写回 frontmatter。"""
    if name not in KNOWN_PRODUCTS:
        abort(404)
    md_path = WIKI_ROOT / name / subdir / f"{slug}.md"
    if not md_path.exists():
        abort(404)
    try:
        score = int(request.form.get("score", "0"))
    except ValueError:
        abort(400)
    if not (1 <= score <= 5):
        abort(400)
    text = md_path.read_text(encoding="utf-8")
    fm, body = _split_fm(text)
    if not fm:
        abort(500, "frontmatter 解析失败")
    fm["score"] = score
    fm["score_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    fm["updated"] = fm["score_at"]
    # 重写文件
    new_text = f"---\n{yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)}---\n\n{body}"
    md_path.write_text(new_text, encoding="utf-8")
    return f"""<!doctype html>
    <meta http-equiv="refresh" content="1; url=/product/{name}/{subdir}/{slug}">
    <p>打分 {score}/5 已保存。正在跳转回详情页...</p>
    <a href="/product/{name}/{subdir}/{slug}">手动返回</a>
    """


@app.route("/review")
def review():
    """99-待审 全局列表。"""
    items = []
    for md in sorted(WIKI_ROOT.rglob("99-待审核/*.md")):
        rel = str(md.relative_to(WIKI_ROOT)).replace("\\", "/")
        text = md.read_text(encoding="utf-8", errors="ignore")
        fm, _ = _split_fm(text)
        items.append({
            "rel_path": rel,
            "title": fm.get("title", md.stem),
            "type": fm.get("type", ""),
            "slug": fm.get("slug", ""),
            "confidence": fm.get("confidence"),
        })

    rows = "".join(
        f"""
        <tr>
          <td><a href="/raw/{i['rel_path']}">{i['title']}</a></td>
          <td>{i['rel_path'].split('/')[0]}</td>
          <td>{i['rel_path'].split('/')[1] if '/' in i['rel_path'] else ''}</td>
          <td>{i['type']}</td>
          <td>{("%.2f" % i['confidence']) if i['confidence'] is not None else ''}</td>
        </tr>
        """
        for i in items
    )

    content_html = f"""
    <h2>99-待审 队列</h2>
    <p>{len(items)} 个待审条目(全局,跨产品)</p>
    <table class="entity-table">
      <thead>
        <tr><th>title</th><th>product</th><th>subdir</th><th>type</th><th>confidence</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """

    return render_template_string(
        BASE_TEMPLATE,
        content_html=content_html,
        **_base_ctx(None),
    )


@app.route("/raw-stats")
def raw_stats():
    """raw/ 扫描状态 + 一键入仓按钮。"""
    from scripts.raw_scanner import scan_raw, scan_stats
    pending = scan_raw()
    summary = scan_stats(pending)

    rows = "".join(
        f"""
        <tr>
          <td>{p['product']}</td>
          <td>{p['source']}</td>
          <td>{p['date']}</td>
          <td><code>{p['filename'][:50]}</code></td>
          <td>{p['bytes']:,} B</td>
        </tr>
        """
        for p in pending[:50]
    )

    action_form = ""
    if pending:
        action_form = f"""
        <form method="post" action="/raw-stats/ingest"
              onsubmit="return confirm('确认对未处理的 {len(pending)} 个 raw 文件执行 ingest?')">
          <button type="submit" class="btn-save">🚀 一键入仓({len(pending)} 个)</button>
        </form>
        """

    content_html = f"""
    <h2>raw/ 扫描状态</h2>
    <pre class="raw-summary">{summary}</pre>
    {action_form}
    <h3>待处理列表(前 50)</h3>
    <table class="entity-table">
      <thead><tr><th>产品</th><th>源</th><th>日期</th><th>文件名</th><th>大小</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="5">所有 raw 文件均已入仓 ✅</td></tr>'}</tbody>
    </table>
    """
    return render_template_string(
        BASE_TEMPLATE, content_html=content_html, **_base_ctx(None),
    )


@app.route("/raw-stats/ingest", methods=["POST"])
def raw_ingest_trigger():
    """执行一键入仓。"""
    from scripts.raw_scanner import scan_raw, run_ingest_batch
    pending = scan_raw()
    if not pending:
        content_html = "<h2>✅ 没有待入仓的 raw 文件</h2><a href='/raw-stats'>返回</a>"
    else:
        stats = run_ingest_batch(pending)
        rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>"
            for k, v in sorted(stats.items()) if v
        )
        content_html = f"""
        <h2>入仓完成</h2>
        <table class="entity-table" style="max-width:400px;">
          <thead><tr><th>状态</th><th>数量</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="margin-top:16px;"><a href="/raw-stats" class="btn-primary">返回</a></p>
        """
    return render_template_string(
        BASE_TEMPLATE, content_html=content_html, **_base_ctx(None),
    )


@app.route("/raw/<path:rel_path>")
def serve_md(rel_path: str):
    """99-待审 等 raw md 的简易渲染(同 entity 路由但路径任意)。"""
    md = WIKI_ROOT / rel_path
    if not md.exists() or not md.is_file():
        abort(404)
    text = md.read_text(encoding="utf-8", errors="ignore")
    fm, body = _split_fm(text)
    rendered = _render_md(body)

    fm_rows = "".join(
        f"<tr><th>{k}</th><td>{v}</td></tr>"
        for k, v in fm.items()
    )

    content_html = f"""
    <h2>{fm.get('title', md.stem)}</h2>
    <p class="slug">{rel_path}</p>
    <div class="frontmatter-box">
      <table>{fm_rows}</table>
    </div>
    <div class="markdown-body">{rendered}</div>
    """

    return render_template_string(
        BASE_TEMPLATE,
        content_html=content_html,
        **_base_ctx(None),
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="知识库 web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9003)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(f"启动 UI: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
