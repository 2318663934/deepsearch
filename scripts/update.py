"""
update.py — 对话式更新

用户输入自然语言指令（如"把李白的1技能冷却改成12秒"），
让 LLM 定位目标文件、生成 patch、写回 md、git commit。

安全设计：
- 默认 dry-run：先打印"将要做的事"，用户确认后才执行
- 所有写入必须经过 git commit 留痕
- 不允许"删除整个文件"
- ReAct 字符串协议（与 query.py 风格一致）

用法：
  python -m scripts.update --instruction "把李白的1技能冷却改成12秒" --dry-run
  python -m scripts.update --instruction "把李白的1技能冷却改成12秒"  # 实际执行
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from scripts.lib_llm import LlamaCppClient, MiniMaxClient, parse_json_safe

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

MAX_STEPS = 6
TIMEOUT_SEC = 60


# ---------------------------------------------------------------------------
# wiki 文件操作
# ---------------------------------------------------------------------------


def _read_wiki_md(rel_path: str) -> Optional[Tuple[Dict[str, Any], str, str]]:
    """读 wiki md，返回 (frontmatter_dict, body_text, full_text) 或 None。"""
    p = WIKI_ROOT / rel_path
    if not p.exists() or not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text, text
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2).rstrip(), text


def _write_wiki_md(rel_path: str, fm: Dict[str, Any], body: str) -> Path:
    """写 wiki md 文件，frontmatter + body。"""
    p = WIKI_ROOT / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    full = f"---\n{fm_text}---\n\n{body.rstrip()}\n"
    p.write_text(full, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ReAct 工具：list_dir, read_file
# ---------------------------------------------------------------------------


def _safe_path(rel_path: str) -> Optional[Path]:
    rel = rel_path.strip().lstrip("/")
    candidate = (WIKI_ROOT / rel).resolve()
    try:
        candidate.relative_to(WIKI_ROOT.resolve())
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def tool_read_file(rel_path: str) -> str:
    p = _safe_path(rel_path)
    if not p:
        return f"[ERROR] 文件不存在或越界: {rel_path}"
    content = p.read_text(encoding="utf-8")
    if len(content) > 6000:
        content = content[:6000] + "\n... [文件截断] ..."
    return f"[FILE: {rel_path}]\n{content}"


def tool_list_dir(rel_path: str = ".") -> str:
    if rel_path.strip() in ("", "."):
        target = WIKI_ROOT
        prefix = ""
    else:
        rel = rel_path.strip().lstrip("/")
        target = (WIKI_ROOT / rel).resolve()
        try:
            target.relative_to(WIKI_ROOT.resolve())
        except ValueError:
            return f"[ERROR] 越界: {rel_path}"
        prefix = rel
    if not target.exists() or not target.is_dir():
        return f"[ERROR] 目录不存在: {rel_path}"
    items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name))
    lines = [f"[DIR: {prefix or '.'}]"]
    for it in items:
        kind = "/" if it.is_dir() else ""
        lines.append(f"  - {it.name}{kind}")
    return "\n".join(lines)


def tool_search(query: str) -> str:
    if not query.strip():
        return "[ERROR] search query 为空"
    pat = re.compile(re.escape(query), re.IGNORECASE)
    hits: List[Tuple[str, str]] = []
    for md in WIKI_ROOT.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        if pat.search(text):
            for i, line in enumerate(text.splitlines()):
                if pat.search(line):
                    ctx_start = max(0, i - 1)
                    ctx_end = min(len(text.splitlines()), i + 2)
                    ctx = "\n".join(text.splitlines()[ctx_start:ctx_end])
                    rel = md.relative_to(WIKI_ROOT)
                    hits.append((str(rel), ctx))
                    break
        if len(hits) >= 8:
            break
    if not hits:
        return f"[SEARCH: {query}] 无匹配"
    out = [f"[SEARCH: {query}] 找到 {len(hits)} 个文件"]
    for path, ctx in hits:
        out.append(f"\n--- {path} ---\n{ctx[:400]}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 协议解析
# ---------------------------------------------------------------------------


_ACTION_PATTERN = re.compile(
    r"ACTION:\s*(read_file|list_dir|search)\s+(?:PATH|QUERY)=([^\n]+)",
    re.IGNORECASE,
)
_FINAL_PATTERN = re.compile(r"FINAL_ANSWER\s*:?\s*(.*)", re.IGNORECASE | re.DOTALL)


def parse_response(text: str) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
    m = _FINAL_PATTERN.search(text)
    if m:
        return m.group(1).strip(), None
    m = _ACTION_PATTERN.search(text)
    if m:
        return None, (m.group(1).lower(), m.group(2).strip())
    return None, None


# ---------------------------------------------------------------------------
# 系统 prompt：定位目标文件
# ---------------------------------------------------------------------------


def _build_locate_prompt() -> str:
    return """你是"产品知识库"的更新助手。你的任务：
1. 接收用户的一条自然语言修改指令（如"把李白的1技能冷却改成12秒"）
2. 通过 ReAct 协议浏览 wiki 目录，找到**唯一**应该被修改的目标文件
3. 输出最终决策

# 工具

## 工具 1: 列出目录
ACTION: list_dir PATH=<相对 wiki 根的路径>
例如：ACTION: list_dir PATH=20-英雄

## 工具 2: 读取文件
ACTION: read_file PATH=<相对 wiki 根的路径>
例如：ACTION: read_file PATH=20-英雄/libai.md

## 工具 3: 关键词搜索
ACTION: search QUERY=<关键词>
例如：ACTION: search QUERY=李白

# 终止协议

找到目标文件后，输出：
```
FINAL_ANSWER:
{
  "target_path": "20-英雄/libai.md",
  "reason": "该文件 frontmatter title=李白，对应指令中的'李白的1技能'",
  "action_type": "modify_field",
  "target_field": "1st_skill_cooldown",
  "old_value_hint": "满级时大约是8秒",
  "new_value_hint": "12秒"
}
```

`action_type` 取值：
- `modify_field`：修改某个事实字段
- `modify_summary`：修改 summary
- `add_related`：增加 related 链接
- `add_fact`：增加新事实

# 行为约束
1. 硬限 6 步；超时 60s 强制终止
2. 找不到时 FINAL_ANSWER 中 target_path 留空并说明
3. 不确定时宁可不输出 FINAL_ANSWER，继续 search
4. FINAL_ANSWER 的 JSON 块**必须**是合法 JSON
"""


# ---------------------------------------------------------------------------
# 系统 prompt：生成 patch
# ---------------------------------------------------------------------------


def _build_patch_prompt() -> str:
    return """你是"产品知识库"的更新助手。你已经定位到目标文件，现在需要生成 patch。

# 输入

你将收到：
- `instruction`：用户的自然语言指令
- `target_path`：要修改的文件路径（相对 wiki 根）
- `current_file`：当前文件的完整内容（frontmatter + body）

# 输出（严格 JSON Schema）

```json
{
  "new_frontmatter": { ... 完整的 frontmatter 字典 ... },
  "new_body": "... 完整的 body 文本 ...",
  "summary": "本次修改的一句话说明",
  "version_bump": 1
}
```

# 规则

1. **必须**输出完整的新 frontmatter 字典（不能只输出 diff），保留所有未改动的字段
2. **`updated` 字段必须更新为当前时间**
3. **实质内容变更时 `version` 必须 +1**（除非只改 updated）
4. **`new_body` 必须是完整 body**，不能省略未改动的部分
5. 修改事实字段时：在 `## 事实` 章节下找到对应 `### <key>` 子节，更新 `- **值**: ...` 那行
6. 如果用户加了一条新事实，在 `## 事实` 末尾追加 `### <new_key>` 子节
7. 如果用户加 related 链接，更新 frontmatter 的 `related` 列表
8. **`sources` 字段不要丢**——如果用户没说改 sources，保留原值
9. **不要凭空增加内容**——只改用户明确说改的
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _git_commit(paths: List[Path], message: str) -> bool:
    try:
        rels = [str(p.relative_to(_PROJECT_ROOT)) for p in paths]
        subprocess.run(
            ["git", "add", *rels],
            cwd=_PROJECT_ROOT, check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=_PROJECT_ROOT, capture_output=True, text=True,
        )
        if result.returncode != 0:
            if "nothing to commit" in (result.stdout + result.stderr).lower():
                return True
            print(f"  [git] commit 提示: {result.stderr.strip()[:200]}")
            return False
        return True
    except FileNotFoundError:
        print("  [git] git 未安装或未在 PATH 中")
        return False
    except Exception as e:
        print(f"  [git] commit 失败: {e}")
        return False


def locate_target(
    instruction: str,
    client: LlamaCppClient,
    verbose: bool = True,
) -> Optional[Dict[str, Any]]:
    """阶段 A：通过 ReAct 定位目标文件 + 决策"""
    if verbose:
        print(f"\n=== 阶段 A: 定位目标文件 ===\n指令: {instruction}")

    action_history: List[str] = []
    start = dt.datetime.now()

    for step in range(1, MAX_STEPS + 1):
        if (dt.datetime.now() - start).total_seconds() > TIMEOUT_SEC:
            if verbose:
                print(f"  [!] 超时 {TIMEOUT_SEC}s")
            return None

        if verbose:
            print(f"\n--- Step {step}/{MAX_STEPS} ---")

        user_prompt = "\n".join(
            [f"用户指令: {instruction}"]
            + action_history
            + ["请继续。"]
        )
        resp = client.generate_with_system(
            system_prompt=_build_locate_prompt(),
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=1500,
        )
        text = resp.content
        if verbose:
            print(f"LLM 输出:\n{text[:500]}{'...' if len(text) > 500 else ''}")

        final, action = parse_response(text)
        if final:
            parsed = parse_json_safe(final)
            if isinstance(parsed, dict) and parsed.get("target_path"):
                if verbose:
                    print(f"\n=== 定位完成: {parsed.get('target_path')} ===")
                return parsed
            if verbose:
                print(f"  [!] FINAL_ANSWER 解析失败或 target_path 为空")
            return None

        if not action:
            if verbose:
                print("  [!] LLM 未按协议输出，终止")
            return None

        action_type, arg = action
        if verbose:
            print(f"  → 执行 ACTION: {action_type} {arg}")

        if action_type == "read_file":
            obs = tool_read_file(arg)
        elif action_type == "list_dir":
            obs = tool_list_dir(arg)
        elif action_type == "search":
            obs = tool_search(arg)
        else:
            obs = f"[ERROR] 未知 action: {action_type}"

        action_history.append(
            f"<observation step=\"{step}\">\n"
            f"你上一步执行了 ACTION: {action_type} {arg}\n"
            f"工具返回：\n\n{obs}\n"
            f"</observation>\n"
            f"重要：下一次输出**必须**严格遵守协议——只输出以 `ACTION:` 开头（继续读取）或 `FINAL_ANSWER:` 开头（给出最终答案）的内容。"
        )

    return None


def generate_patch(
    instruction: str,
    target_path: str,
    client: LlamaCppClient,
) -> Optional[Dict[str, Any]]:
    """阶段 B：让 LLM 生成完整的新 frontmatter + body"""
    cur = _read_wiki_md(target_path)
    if cur is None:
        print(f"  [X] 目标文件无法读取: {target_path}")
        return None
    fm, body, full = cur

    user = (
        f"## instruction\n{instruction}\n\n"
        f"## target_path\n{target_path}\n\n"
        f"## current_file\n{full}\n\n"
        "请按 schema 输出严格 JSON（new_frontmatter 必须包含所有现有字段并按需更新）。"
    )
    resp = client.generate_with_system(
        system_prompt=_build_patch_prompt(),
        user_prompt=user,
        temperature=0.1,
        max_tokens=4000,
    )
    parsed = parse_json_safe(resp.content)
    if not isinstance(parsed, dict):
        print(f"  [X] patch 解析失败\n  LLM 输出: {resp.content[:500]}")
        return None
    if "new_frontmatter" not in parsed or "new_body" not in parsed:
        print("  [X] patch 缺少 new_frontmatter 或 new_body")
        return None
    return parsed


def apply_patch(target_path: str, patch: Dict[str, Any], dry_run: bool = True) -> bool:
    """阶段 C：应用 patch（dry-run 时只打印不写）"""
    fm = patch["new_frontmatter"]
    body = patch["new_body"]
    summary = patch.get("summary", "update")
    version_bump = int(patch.get("version_bump", 1))

    # 校验
    if not target_path or "/" not in target_path:
        print(f"  [X] 目标路径非法: {target_path}")
        return False
    target_p = WIKI_ROOT / target_path
    if not target_p.exists():
        print(f"  [X] 目标文件不存在: {target_path}")
        return False

    # 读老 frontmatter 看 version
    cur = _read_wiki_md(target_path)
    if cur:
        old_fm = cur[0]
        # 如果 patch 里 version 没变 + LLM 说要 bump，强制应用
        if version_bump > 0 and int(fm.get("version", 1)) == int(old_fm.get("version", 1)):
            fm["version"] = int(old_fm.get("version", 1)) + version_bump
    # 确保 updated 是当前时间
    fm["updated"] = _now_iso()

    if dry_run:
        print("\n=== DRY RUN: 即将执行的修改 ===")
        print(f"目标文件: {target_path}")
        print(f"说明: {summary}")
        print(f"new frontmatter:")
        print(yaml.safe_dump(fm, allow_unicode=True, sort_keys=False))
        print("new body (前 500 字):")
        print(body[:500] + ("..." if len(body) > 500 else ""))
        print("\n要实际执行，请去掉 --dry-run 参数")
        return True

    # 实际写
    _write_wiki_md(target_path, fm, body)
    print(f"  ✓ 已写入 {target_path}")
    # commit
    msg = f"update: {summary} [{target_path}]"
    if _git_commit([target_p], msg):
        print(f"  ✓ git commit: {msg}")
    return True


def main():
    parser = argparse.ArgumentParser(description="对话式更新 wiki")
    parser.add_argument("--instruction", "-i", required=True, help="用户自然语言指令")
    parser.add_argument("--dry-run", action="store_true", default=True, help="只预览不写（默认）")
    parser.add_argument("--apply", action="store_true", help="实际写文件+commit")
    parser.add_argument("--quiet", action="store_true", help="不打印中间过程")
    parser.add_argument("--product", type=str, default=None, help="产品 ID(wangzhe/luoke)")
    args = parser.parse_args()

    dry_run = not args.apply

    client = LlamaCppClient()
    print(f"使用模型: {client.model}")

    # 阶段 A: 定位
    target = locate_target(args.instruction, client, verbose=not args.quiet)
    if not target or not target.get("target_path"):
        print("\n[X] 阶段 A 失败：未定位到目标文件")
        return

    # 阶段 B: 生成 patch
    if not args.quiet:
        print(f"\n=== 阶段 B: 生成 patch ===")
    patch = generate_patch(args.instruction, target["target_path"], client)
    if not patch:
        print("\n[X] 阶段 B 失败：未生成有效 patch")
        return

    # 阶段 C: 应用
    if not args.quiet:
        print(f"\n=== 阶段 C: {'DRY RUN' if dry_run else 'APPLY'} ===")
    apply_patch(target["target_path"], patch, dry_run=dry_run)


if __name__ == "__main__":
    main()
