"""
ingest.py — 从 raw/ 原始资料抽取信息，按置信度门控写入 wiki/

阶段1最小链路：
  读取 raw 文件 → 调 LlamaCppClient 抽取 → 调 LlamaCppClient 评置信度
  → 叠加硬规则校准 → 按阈值写入 wiki/ 或 99-待审核/ 或丢弃

用法：
  python -m scripts.ingest --raw <文件路径> [--entity-type hero|skill|overview|stub]
  python -m scripts.ingest --raw-dir <目录>  # 批量处理目录所有文件
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from scripts.lib_llm import LlamaCppClient, parse_json_safe
from scripts.lib_prompt import load_product_prompt

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"
RAW_ROOT = _PROJECT_ROOT / "raw"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
STATE_DIR = _PROJECT_ROOT / "state"

CONFIDENCE_AUTO_WRITE = 0.7
CONFIDENCE_REVIEW = 0.4

ENTITY_TYPE_TO_DIR = {
    "hero": "20-英雄",      # 王者:英雄
    "pet": "20-精灵",       # 洛克:精灵(对应王者荣耀英雄)
    "skill": "30-技能",     # 通用:技能(个体技能)
    "mechanism": "30-机制", # 通用机制(冷却/攻速/游戏模式等)
    "summoner_spell": "80-召唤师技能",  # 王者:召唤师技能
    "equipment": "90-局内装备",        # 王者:局内装备
    "item": "40-道具",      # 洛克:道具
    "quest": "50-任务",     # 洛克:任务
    "map": "60-地图",       # 洛克:地图/地区
    "clothing": "40-服装",  # 洛克:服装(皮卡服装店等)
    "skin": "40-皮肤",      # 王者:皮肤系统+系列
    "overview": "10-产品概述",
    "stub": "99-待审核",
}


# ---------------------------------------------------------------------------
# Prompt 加载
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 原始资料读取
# ---------------------------------------------------------------------------


def read_raw_file(path: Path) -> str:
    """读取原始资料。优先 .txt，其他按 utf-8 文本读取。"""
    if not path.exists():
        raise FileNotFoundError(f"raw 文件不存在: {path}")
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".wikitext"}:
        return path.read_text(encoding="utf-8")
    if suffix in {".html", ".htm"}:
        # 简单 HTML 文本提取（不引入额外依赖）
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

        ex = _TextExtractor()
        ex.feed(path.read_text(encoding="utf-8", errors="ignore"))
        text = " ".join(ex.parts)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    # 默认按文本读取
    return path.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# LLM 抽取 + 置信度评估
# ---------------------------------------------------------------------------


def _build_extract_prompt(source_path: str, raw_text: str, product: str) -> tuple[str, str]:
    system = load_product_prompt("extract.md", product)
    user = (
        f"## source_path\n{source_path}\n\n"
        f"## raw_text（前 4000 字）\n{raw_text[:4000]}\n\n"
        "请按 schema 输出严格 JSON。"
    )
    return system, user


def _call_extract(client: LlamaCppClient, source_path: str, raw_text: str, product: str) -> Optional[Dict[str, Any]]:
    system, user = _build_extract_prompt(source_path, raw_text, product)
    resp = client.generate_with_system(system, user, temperature=0.2, max_tokens=2000)
    parsed = parse_json_safe(resp.content)
    if not isinstance(parsed, dict):
        print(f"  [!] 抽取结果解析失败，返回内容前 300 字：\n{resp.content[:300]}")
        return None
    return parsed


# ---------------------------------------------------------------------------
# 硬规则校准
# ---------------------------------------------------------------------------


def _pet_to_extracted(parsed: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    """把 lib_luoke_parser.parse_pet_wikitext 的输出包装成 ingest 期望的 schema。"""
    stats = parsed.get("stats") or {}
    facts: List[Dict[str, str]] = []
    if parsed.get("phase"):
        facts.append({"key": "phase", "value": parsed["phase"], "evidence": f"精灵阶段={parsed['phase']}"})
    if parsed.get("main_attr"):
        facts.append({"key": "main_attr", "value": parsed["main_attr"], "evidence": f"主属性={parsed['main_attr']}"})
    if parsed.get("sub_attr"):
        facts.append({"key": "sub_attr", "value": parsed["sub_attr"], "evidence": f"2属性={parsed['sub_attr']}"})
    for k, v in stats.items():
        if v != "" and v is not None:
            facts.append({"key": f"stat_{k}", "value": str(v), "evidence": f"{k}={v}"})
    if parsed.get("description"):
        facts.append({"key": "description", "value": parsed["description"], "evidence": "精灵描述"})
    if parsed.get("region"):
        facts.append({"key": "region", "value": parsed["region"], "evidence": "分布地区"})
    if parsed.get("evolution"):
        facts.append({"key": "evolution", "value": parsed["evolution"], "evidence": "进化条件"})
    if parsed.get("form"):
        facts.append({"key": "form", "value": parsed["form"], "evidence": f"精灵形态={parsed['form']}"})
    if parsed.get("form_name"):
        facts.append({"key": "form_name", "value": parsed["form_name"], "evidence": f"地区形态名称={parsed['form_name']}"})

    from scripts.lib_luoke_parser import slugify_zh
    base_slug = slugify_zh(parsed["name"])
    # 异形态: 拼上 form_name pinyin 作区分(避免冲突)
    form_name = parsed.get("form_name")
    if form_name:
        slug = f"{base_slug}-{slugify_zh(form_name)}"
    else:
        slug = base_slug

    return {
        "entity_type": "pet",
        "slug": slug,
        "title": parsed["name"] + (f"({form_name})" if form_name else ""),
        "aliases": [],
        "summary": parsed.get("description", "")[:80],
        "facts": facts,
        "sources": [source_path],
        "confidence": 0.95,  # 模板解析 = 100% 准确
        "confidence_reason": "B 站 wiki SMW 模板结构化解析,字段精确,无需 LLM",
    }


def _skill_to_extracted(parsed: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    """把 parse_skill_wikitext 输出包装成 ingest schema。"""
    facts: List[Dict[str, str]] = []
    for k in ("attr", "category", "cost", "power", "effect", "description", "version"):
        v = parsed.get(k)
        if v:
            facts.append({"key": k, "value": v, "evidence": f"{k}={v}"})
    from scripts.lib_luoke_parser import slugify_zh
    slug = slugify_zh(parsed["name"])
    return {
        "entity_type": "skill",
        "slug": slug,
        "title": parsed["name"],
        "aliases": [],
        "summary": parsed.get("description", "")[:80],
        "facts": facts,
        "sources": [source_path],
        "confidence": 0.95,
        "confidence_reason": "B 站 wiki 技能模板结构化解析",
    }


def _item_to_extracted(parsed: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    """把 parse_item_wikitext 输出包装成 ingest schema。"""
    facts: List[Dict[str, str]] = []
    for k in ("rarity", "main_category", "sub_category", "use", "description", "source", "icon", "version"):
        v = parsed.get(k)
        if v:
            facts.append({"key": k, "value": v, "evidence": f"{k}={v}"})
    from scripts.lib_luoke_parser import slugify_zh
    slug = slugify_zh(parsed["name"])
    return {
        "entity_type": "item",
        "slug": slug,
        "title": parsed["name"],
        "aliases": [],
        "summary": parsed.get("description", "")[:80] or parsed.get("use", "")[:80],
        "facts": facts,
        "sources": [source_path],
        "confidence": 0.95,
        "confidence_reason": "B 站 wiki 物品信息模板结构化解析",
    }


def _quest_to_extracted(parsed: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    """把 parse_quest_wikitext 输出包装成 ingest schema。"""
    facts: List[Dict[str, str]] = []
    for k in ("quest_id", "category", "location", "description", "reward", "note", "owner"):
        v = parsed.get(k)
        if v:
            facts.append({"key": k, "value": v, "evidence": f"{k}={v}"})
    from scripts.lib_luoke_parser import slugify_zh
    slug = slugify_zh(parsed["name"])
    return {
        "entity_type": "quest",
        "slug": slug,
        "title": parsed["name"],
        "aliases": [],
        "summary": parsed.get("description", "")[:80],
        "facts": facts,
        "sources": [source_path],
        "confidence": 0.95,
        "confidence_reason": "B 站 wiki 任务信息模板结构化解析",
    }


def _calibrate_confidence(
    extracted: Dict[str, Any],
    raw_text: str,
) -> Dict[str, Any]:
    """
    叠加硬规则校准置信度（见 CLAUDE.md §4.2）。
    """
    raw_lower = raw_text.lower()
    notes: List[str] = []
    base_conf = float(extracted.get("confidence", 0.0))
    conf = base_conf

    # 规则 1：必须有 source
    sources = extracted.get("sources") or []
    sources = sources if isinstance(sources, list) else [sources]
    if not sources:
        conf = min(conf, 0.39)
        notes.append("无 source，强制 < 0.4")

    # 规则 2：数值类字段必须能在原文找到对应字符串
    facts = extracted.get("facts") or []
    if not isinstance(facts, list):
        facts = []
    numerical_drop = 0
    for f in facts:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key", ""))
        value = str(f.get("value", ""))
        # 简单识别：key 含 cooldown/damage/price/date/number 等
        is_numerical = any(
            kw in key.lower()
            for kw in ["cooldown", "damage", "price", "date", "数值", "冷却", "伤害", "价格"]
        )
        if is_numerical:
            # 取数字部分
            nums = re.findall(r"\d+(?:\.\d+)?", value)
            if nums:
                # 至少一个数字能在原文找到
                if not any(n in raw_text for n in nums):
                    numerical_drop += 1
                    notes.append(f"字段 {key} 数值 {nums} 在原文中未找到，降 0.2")
    if numerical_drop > 0:
        conf = max(0.0, conf - 0.2 * numerical_drop)

    # 规则 3：与 wiki 既有页冲突时自动降一档（阶段1简化版：跳过，等阶段2）
    # 阶段1不做冲突检测，TODO 阶段2接入

    conf = round(min(1.0, max(0.0, conf)), 2)
    extracted["confidence"] = conf
    extracted["confidence_reason"] = (
        extracted.get("confidence_reason", "")
        + (" | 硬规则校准: " + "; ".join(notes) if notes else " | 硬规则校准: 无调整")
    )
    return extracted


# ---------------------------------------------------------------------------
# 写入 md
# ---------------------------------------------------------------------------


def _build_frontmatter(extracted: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    sources = extracted.get("sources") or [source_path]
    if not isinstance(sources, list):
        sources = [str(sources)]
    sources = [str(s) for s in sources]
    if source_path not in sources:
        sources.insert(0, source_path)

    aliases = extracted.get("aliases") or []
    if not isinstance(aliases, list):
        aliases = [str(aliases)]
    aliases = [str(a) for a in aliases if a]

    return {
        "title": str(extracted.get("title", extracted.get("slug", "未命名"))),
        "type": str(extracted.get("entity_type", "stub")),
        "slug": str(extracted.get("slug", "unknown")),
        "aliases": aliases,
        "sources": sources,
        "confidence": float(extracted.get("confidence", 0.0)),
        "confidence_reason": str(extracted.get("confidence_reason", "")),
        "created": now,
        "updated": now,
        "last_verified": now,
        "status": "verified" if float(extracted.get("confidence", 0.0)) >= CONFIDENCE_AUTO_WRITE else "pending",
        "version": 1,
    }


def _build_body(extracted: Dict[str, Any]) -> str:
    lines: List[str] = []
    summary = extracted.get("summary")
    if summary:
        lines.append(str(summary).strip())
        lines.append("")

    facts = extracted.get("facts") or []
    if isinstance(facts, list) and facts:
        lines.append("## 事实")
        for f in facts:
            if not isinstance(f, dict):
                continue
            key = f.get("key", "")
            value = f.get("value", "")
            evidence = f.get("evidence", "")
            lines.append(f"### {key}")
            lines.append(f"- **值**: {value}")
            if evidence:
                lines.append(f"- **原文依据**: {evidence}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_markdown(fm: Dict[str, Any], body: str) -> str:
    fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{fm_text}---\n\n{body}"


def _target_path(extracted: Dict[str, Any], product: str, entity_type_override: Optional[str] = None) -> Path:
    entity_type = entity_type_override or extracted.get("entity_type", "stub")
    sub = ENTITY_TYPE_TO_DIR.get(entity_type, "99-待审核")
    slug = str(extracted.get("slug", "unknown")).strip().lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9\-_]", "", slug) or "unknown"
    return WIKI_ROOT / product / sub / f"{slug}.md"


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_commit(paths: List[Path], message: str) -> None:
    """git add + commit。失败不抛错（可能是首次提交）。"""
    try:
        subprocess.run(
            ["git", "add", *[str(p.relative_to(_PROJECT_ROOT)) for p in paths]],
            cwd=_PROJECT_ROOT,
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # 没有变更时也正常
            if "nothing to commit" in (result.stdout + result.stderr).lower():
                return
            print(f"  [git] commit 提示: {result.stderr.strip()[:200]}")
    except FileNotFoundError:
        print("  [git] git 未安装或未在 PATH 中，跳过 commit")
    except Exception as e:
        print(f"  [git] commit 失败: {e}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def ingest_one(
    raw_path: Path,
    client: LlamaCppClient,
    product: Optional[str] = None,
    entity_type_override: Optional[str] = None,
    slug_override: Optional[str] = None,
) -> Dict[str, Any]:
    # product 必填:由 raw 路径 raw/<product>/<source>/<date>/xxx.txt 推断
    if product is None:
        try:
            rel = raw_path.relative_to(RAW_ROOT)
            product = rel.parts[0]  # e.g. raw/wangzhe/... → "wangzhe"
        except ValueError:
            raise ValueError(f"无法从 raw 路径推断 product: {raw_path},请显式传 --product")

    # 已处理跳过：raw 路径旁有 .processed 标记文件则跳过（避免重复 ingest）
    if raw_path.with_suffix(raw_path.suffix + ".processed").exists():
        return {"status": "skipped_processed", "path": str(raw_path)}

    print(f"\n=== 处理: {raw_path} (product={product}) ===")
    raw_text = read_raw_file(raw_path)
    print(f"  原文长度: {len(raw_text)} 字")

    # 1. 抽取(洛克+bilibili 走结构化 parser,其他走 LLM)
    print("  [1/3] 抽取中...")
    extracted: Optional[Dict[str, Any]] = None
    if product == "luoke" and "bilibili" in str(raw_path):
        from scripts.lib_luoke_parser import (
            parse_pet_wikitext, parse_skill_wikitext,
            parse_item_wikitext, parse_quest_wikitext,
        )

        rel_path = str(raw_path.relative_to(_PROJECT_ROOT))
        # 先按 entity_type_override 决定走哪条解析,否则按顺序试
        parsed: Optional[Dict[str, Any]] = None
        if entity_type_override == "pet":
            parsed = parse_pet_wikitext(raw_text)
        elif entity_type_override == "skill":
            parsed = parse_skill_wikitext(raw_text)
        elif entity_type_override == "item":
            parsed = parse_item_wikitext(raw_text)
        elif entity_type_override == "quest":
            parsed = parse_quest_wikitext(raw_text)
        else:
            parsed = (parse_pet_wikitext(raw_text) or parse_skill_wikitext(raw_text)
                       or parse_item_wikitext(raw_text) or parse_quest_wikitext(raw_text))
        if parsed:
            et = parsed.get("entity_type", "stub")
            if et == "pet":
                extracted = _pet_to_extracted(parsed, rel_path)
            elif et == "skill":
                extracted = _skill_to_extracted(parsed, rel_path)
            elif et == "item":
                extracted = _item_to_extracted(parsed, rel_path)
            elif et == "quest":
                extracted = _quest_to_extracted(parsed, rel_path)
            else:
                extracted = None
            print(f"  [parser] bilibili SMW 解析成功: type={et}, name={parsed.get('name')}")
        else:
            print("  [parser] SMW 模板未命中,回退 LLM 抽取")
            extracted = _call_extract(client, rel_path, raw_text, product)
    else:
        extracted = _call_extract(client, str(raw_path.relative_to(_PROJECT_ROOT)), raw_text, product)
    if not extracted:
        print("  [X] 抽取失败，跳过")
        return {"status": "extract_failed", "path": str(raw_path)}

    # slug 覆盖（命令行传入时优先）
    if slug_override:
        print(f"  强制 slug 覆盖: {extracted.get('slug')} -> {slug_override}")
        extracted["slug"] = slug_override

    print(f"  抽取 entity_type={extracted.get('entity_type')}, slug={extracted.get('slug')}")
    print(f"  LLM 自评 confidence={extracted.get('confidence')}, facts数={len(extracted.get('facts') or [])}")

    # 2. 校准
    print("  [2/3] 硬规则校准中...")
    extracted = _calibrate_confidence(extracted, raw_text)
    final_conf = extracted["confidence"]
    print(f"  校准后 confidence={final_conf}")

    # 3. 决定落点
    print("  [3/3] 落盘...")
    # entity_type 和 slug 都强制覆盖(避免 LLM 误用产品 slug)
    if entity_type_override:
        extracted["entity_type"] = entity_type_override
    if slug_override:
        extracted["slug"] = slug_override
    fm = _build_frontmatter(extracted, str(raw_path.relative_to(_PROJECT_ROOT)))
    body = _build_body(extracted)
    md = _render_markdown(fm, body)

    if entity_type_override:
        target = _target_path(extracted, product, entity_type_override=entity_type_override)
    else:
        target = _target_path(extracted, product)

    # 冲突时（既有页存在）先入 99-待审核
    if target.exists():
        # 升级为完整冲突检测：规则 + LLM 兜底
        from scripts.lib_confidence import decide_action

        print("  [conflict] 既有页存在，调用 decide_action...")
        diff_result, existing_page = decide_action(extracted, client=client, product=product)
        print(f"  [conflict] action={diff_result.action}, reason={diff_result.reason[:100]}")

        # 根据 action 决定处理
        if diff_result.action == "no_overlap":
            # LLM 判定"不是同一实体"——以新为准（罕见情况，覆盖既有页）
            if final_conf >= CONFIDENCE_AUTO_WRITE:
                _write_md(target, md)
                print(f"  ✓ 覆盖写入 {target.relative_to(_PROJECT_ROOT)} (LLM 判定非同一实体)")
                result = {
                    "status": "written",
                    "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
                    "confidence": final_conf,
                }
            else:
                review_path = WIKI_ROOT / product / "99-待审核" / target.name
                _write_md(review_path, md)
                result = {
                    "status": "review",
                    "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
                    "review_path": str(review_path.relative_to(_PROJECT_ROOT)),
                    "confidence": final_conf,
                }
        elif diff_result.action == "replace":
            # 规则/LLM 判定：可以替换既有页
            if final_conf >= CONFIDENCE_AUTO_WRITE:
                _write_md(target, md)
                print(f"  ✓ 替换既有页: {target.relative_to(_PROJECT_ROOT)}")
                result = {
                    "status": "replaced",
                    "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
                    "confidence": final_conf,
                }
            else:
                review_path = WIKI_ROOT / product / "99-待审核" / target.name
                _write_md(review_path, md)
                result = {
                    "status": "review",
                    "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
                    "review_path": str(review_path.relative_to(_PROJECT_ROOT)),
                    "confidence": final_conf,
                }
        elif diff_result.action == "append":
            # 互补：保留既有页，新事实追加到既有页（暂用待审核）
            review_path = WIKI_ROOT / product / "99-待审核" / f"{fm['slug']}-append-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.md"
            _write_md(review_path, md)
            print(f"  ➕ 追加模式：写入 {review_path.relative_to(_PROJECT_ROOT)}（待人工合并）")
            result = {
                "status": "append",
                "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
                "review_path": str(review_path.relative_to(_PROJECT_ROOT)),
                "confidence": final_conf,
                "appended_keys": diff_result.appended_keys,
            }
        else:
            # conflict：写入 99-待审核
            review_path = WIKI_ROOT / product / "99-待审核" / f"{fm['slug']}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.md"
            _write_md(review_path, md)
            print(f"  ⚠️  冲突: {review_path.relative_to(_PROJECT_ROOT)}")
            print(f"      冲突字段: {diff_result.conflicting_keys[:5]}")
            result = {
                "status": "conflict",
                "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
                "review_path": str(review_path.relative_to(_PROJECT_ROOT)),
                "confidence": final_conf,
                "conflicting_keys": diff_result.conflicting_keys,
            }
    elif final_conf >= CONFIDENCE_AUTO_WRITE:
        _write_md(target, md)
        print(f"  ✓ 写入 {target.relative_to(_PROJECT_ROOT)}")
        result = {
            "status": "written",
            "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
            "confidence": final_conf,
        }
    elif final_conf >= CONFIDENCE_REVIEW:
        review_path = WIKI_ROOT / product / "99-待审核" / target.name
        _write_md(review_path, md)
        print(f"  ⚠️  置信度 {final_conf} 入待审核: {review_path.relative_to(_PROJECT_ROOT)}")
        result = {
            "status": "review",
            "wiki_path": str(target.relative_to(_PROJECT_ROOT)),
            "review_path": str(review_path.relative_to(_PROJECT_ROOT)),
            "confidence": final_conf,
        }
    else:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "discarded.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": dt.datetime.now().isoformat(),
                        "raw_path": str(raw_path.relative_to(_PROJECT_ROOT)),
                        "product": product,
                        "confidence": final_conf,
                        "reason": extracted.get("confidence_reason", ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        print(f"  ✗ 置信度 {final_conf} 丢弃（已记录到 state/discarded.log）")
        result = {
            "status": "discarded",
            "confidence": final_conf,
        }

    # git commit（仅当实际写入了文件）
    if result["status"] in ("written", "conflict", "review"):
        paths_to_commit = []
        if "wiki_path" in result:
            wp = _PROJECT_ROOT / result["wiki_path"]
            if wp.exists():
                paths_to_commit.append(wp)
        if "review_path" in result:
            rp = _PROJECT_ROOT / result["review_path"]
            if rp.exists():
                paths_to_commit.append(rp)
        if paths_to_commit:
            commit_msg = (
                f"ingest: {fm['title']} (conf={final_conf}, status={result['status']})"
            )
            _git_commit(paths_to_commit, commit_msg)

    # 无论结果如何，写 .processed 标记（避免重跑）
    raw_path.with_suffix(raw_path.suffix + ".processed").write_text(
        f"status={result['status']}\nconfidence={result.get('confidence', 'N/A')}\n",
        encoding="utf-8",
    )

    return result


def main():
    parser = argparse.ArgumentParser(description="Ingest raw → wiki")
    parser.add_argument("--raw", type=str, help="单个 raw 文件路径（相对项目根）")
    parser.add_argument("--raw-dir", type=str, help="批量处理目录（相对项目根）")
    parser.add_argument("--entity-type", type=str, choices=list(ENTITY_TYPE_TO_DIR.keys()),
                        help="强制指定 entity_type")
    parser.add_argument("--slug", type=str, help="强制指定 slug（覆盖 LLM 抽取结果）")
    parser.add_argument("--product", type=str, default=None, help="产品 ID(wangzhe/luoke),None 时根据 raw 路径推断")
    args = parser.parse_args()

    if not args.raw and not args.raw_dir:
        parser.print_help()
        sys.exit(1)

    client = LlamaCppClient()
    print(f"使用模型: {client.model}")

    targets: List[Path] = []
    if args.raw:
        targets.append(_PROJECT_ROOT / args.raw)
    if args.raw_dir:
        d = _PROJECT_ROOT / args.raw_dir
        if not d.is_dir():
            print(f"目录不存在: {d}")
            sys.exit(1)
        # 只处理 txt 和 md（不处理 html，避免和 txt 重复）
        targets.extend(sorted(d.rglob("*.txt")))
        targets.extend(sorted(d.rglob("*.md")))
        targets.extend(sorted(d.rglob("*.wikitext")))

    if not targets:
        print("未找到任何 raw 文件")
        sys.exit(1)

    print(f"待处理 {len(targets)} 个文件")
    results = []
    for t in targets:
        r = ingest_one(t, client, product=args.product, entity_type_override=args.entity_type, slug_override=args.slug)
        results.append(r)

    print("\n=== 处理汇总 ===")
    for t, r in zip(targets, results):
        print(f"  {t.name}: {r['status']} (conf={r.get('confidence', 'N/A')})")


if __name__ == "__main__":
    main()
