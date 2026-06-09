"""
query.py — 用户查询入口（ReAct 字符串协议）

让 LLM 主动浏览 wiki/ 下的 md 文件，生成带引用的回答。

协议：
  - LLM 输出 `ACTION: read_file PATH=...` 或 `ACTION: list_dir PATH=...`
  - 或 `ACTION: search QUERY=...`
  - 或 `FINAL_ANSWER: <回答>`
  - Python 截胡、读文件、塞回上下文、循环
  - 硬限 6 步 + 60s 超时

用法：
  python -m scripts.query --q "李白的技能是什么"
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from scripts.lib_llm import MiniMaxClient
from scripts.lib_prompt import load_product_prompt

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = _PROJECT_ROOT / "wiki"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

MAX_STEPS = 6
TIMEOUT_SEC = 60


# ---------------------------------------------------------------------------
# 工具实现：实际执行 LLM 要求的操作
# ---------------------------------------------------------------------------


def _safe_path(rel_path: str, product: Optional[str] = None) -> Optional[Path]:
    """
    把 LLM 给的相对路径安全地映射到 WIKI_ROOT 下的真实路径。
    防止 LLM 输出 ../../../etc/passwd 之类的越权。

    Args:
        rel_path: LLM 给的相对路径,如 "20-英雄/li-bai.md" 或 "wangzhe/20-英雄/li-bai.md"
        product: 产品 ID,None=全搜;非 None 时简写路径找不到会自动试加 product 前缀
    """
    rel = rel_path.strip().lstrip("/")
    # 1) 直接尝试
    candidate = (WIKI_ROOT / rel).resolve()
    try:
        candidate.relative_to(WIKI_ROOT.resolve())
    except ValueError:
        return None
    if candidate.exists():
        return candidate
    # 2) product 限定时,fallback 试加 product 前缀
    if product:
        prefixed = f"{product}/{rel}"
        candidate2 = (WIKI_ROOT / prefixed).resolve()
        try:
            candidate2.relative_to(WIKI_ROOT.resolve())
        except ValueError:
            return None
        if candidate2.exists():
            return candidate2
    return None


def tool_read_file(rel_path: str, product: Optional[str] = None) -> str:
    p = _safe_path(rel_path, product)
    if not p:
        return f"[ERROR] 文件不存在或越界: {rel_path}"
    if p.is_dir():
        return tool_list_dir(str(p.relative_to(WIKI_ROOT)), product)
    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"[ERROR] 读取失败: {e}"
    # 截断过长文件
    if len(content) > 6000:
        content = content[:6000] + "\n\n... [文件截断，原文更长] ..."
    return f"[FILE: {rel_path}]\n{content}"


def tool_list_dir(rel_path: str = ".", product: Optional[str] = None) -> str:
    if rel_path.strip() in ("", "."):
        # 根目录:有 product 限定时进到产品子目录
        if product:
            target = WIKI_ROOT / product
            prefix = product
        else:
            target = WIKI_ROOT
            prefix = ""
        if not target.exists():
            return f"[ERROR] 目录不存在: {target}"
    else:
        p = _safe_path(rel_path, product)
        if not p:
            return f"[ERROR] 目录不存在或越界: {rel_path}"
        target = p
        prefix = rel_path
    if not target.is_dir():
        return f"[ERROR] 不是目录: {rel_path}"
    items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name))
    lines = [f"[DIR: {prefix or '.'}]"]
    for it in items:
        kind = "/" if it.is_dir() else ""
        lines.append(f"  - {it.name}{kind}")
    return "\n".join(lines)


def tool_search(query: str, product: Optional[str] = None) -> str:
    """在所有 md 文件中搜索关键词。product 限定时只搜产品子目录。"""
    if not query.strip():
        return "[ERROR] search query 为空"
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    hits: List[Tuple[str, str]] = []
    roots = [WIKI_ROOT / product] if product else [WIKI_ROOT]
    for root in roots:
        if not root.exists():
            continue
        for md in root.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if pattern.search(text):
                for i, line in enumerate(text.splitlines()):
                    if pattern.search(line):
                        ctx_start = max(0, i - 1)
                        ctx_end = min(len(text.splitlines()), i + 2)
                        ctx = "\n".join(text.splitlines()[ctx_start:ctx_end])
                        rel = md.relative_to(WIKI_ROOT)
                        hits.append((str(rel), ctx))
                        break
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
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


def parse_response(text: str) -> Tuple[Optional[str], Optional[Tuple[str, str]], str]:
    """
    解析 LLM 输出。
    返回 (final_answer, action, remaining_text)
    - final_answer 非空 → 已找到最终答案
    - action 非空 → (action_type, arg)
    """
    m = _FINAL_PATTERN.search(text)
    if m:
        answer = m.group(1).strip()
        return answer, None, text

    m = _ACTION_PATTERN.search(text)
    if m:
        return None, (m.group(1).lower(), m.group(2).strip()), text

    return None, None, text


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def _load_protocol_prompt(product: str = "wangzhe") -> str:
    react_md = load_product_prompt("react_query.md", product)
    answer_md = (PROMPTS_DIR / "answer.md").read_text(encoding="utf-8")
    # 合并 react 协议说明 + answer 协议作为 system prompt
    return f"{react_md}\n\n---\n\n{answer_md}"


def query(question: str, client: Optional[MiniMaxClient] = None, verbose: bool = True, product: str = "wangzhe") -> str:
    if client is None:
        client = MiniMaxClient()

    system_prompt = _load_protocol_prompt(product=product)
    messages: List[dict] = [
        {"role": "user", "content": f"用户问题: {question}\n\n请开始 ReAct 协议。"}
    ]

    start = time.time()
    final_answer: Optional[str] = None
    action_history: List[str] = []

    for step in range(1, MAX_STEPS + 1):
        if time.time() - start > TIMEOUT_SEC:
            print(f"  [!] 超时 {TIMEOUT_SEC}s 强制终止")
            break

        if verbose:
            print(f"\n--- Step {step}/{MAX_STEPS} ---")

        resp = client.generate_with_system(
            system_prompt=system_prompt,
            user_prompt="\n".join(
                [f"用户问题: {question}"]
                + action_history
                + ["请继续。"]
            ),
            temperature=0.2,
            max_tokens=1500,
        )
        text = resp.content
        if verbose:
            print(f"LLM 输出:\n{text[:500]}{'...' if len(text) > 500 else ''}")

        final_answer, action, _ = parse_response(text)

        if final_answer is not None:
            if verbose:
                print("\n=== 找到 FINAL_ANSWER ===")
            break

        if action is None:
            # LLM 没有按协议输出
            if verbose:
                print("  [!] LLM 未按协议输出 ACTION/FINAL_ANSWER，强制终止")
            final_answer = (
                "模型未按 ReAct 协议响应。基于当前 wiki 知识，无法可靠回答该问题。\n\n"
                f"LLM 原始输出: {text[:500]}"
            )
            break

        action_type, arg = action
        if verbose:
            print(f"  → 执行 ACTION: {action_type} {arg}")

        # 执行工具(注入 product 上下文)
        if action_type == "read_file":
            observation = tool_read_file(arg, product=product)
        elif action_type == "list_dir":
            observation = tool_list_dir(arg, product=product)
        elif action_type == "search":
            observation = tool_search(arg, product=product)
        else:
            observation = f"[ERROR] 未知 action: {action_type}"

        action_history.append(
            f"<observation step=\"{step}\">\n"
            f"你上一步执行了 ACTION: {action_type} {arg}\n"
            f"以下是工具返回的结果：\n\n{observation}\n"
            f"</observation>\n"
            f"重要：你的下一次输出**必须**严格遵守协议——只输出以 `ACTION:` 开头（继续读取）或 `FINAL_ANSWER:` 开头（给出最终答案）的内容，**不要**输出 `[Step N] ACTION:` 这种带前缀的格式。"
        )

    if final_answer is None:
        # 强制 final
        if verbose:
            print("  [!] 达到步数上限，强制 FINAL_ANSWER")
        final_answer = (
            f"达到 ReAct 步数上限（{MAX_STEPS}步）或超时（{TIMEOUT_SEC}s）。\n"
            "知识库中可能暂无该信息，或模型未能在限制内定位。\n\n"
            "可尝试: 1) 在 wiki/ 中增加相关内容; 2) 用更具体的关键词重试。"
        )

    return final_answer


def main():
    parser = argparse.ArgumentParser(description="Query the wiki via ReAct")
    parser.add_argument("--q", required=True, help="用户问题")
    parser.add_argument("--quiet", action="store_true", help="不打印 ReAct 步骤")
    parser.add_argument("--product", type=str, default="wangzhe", help="产品 ID(wangzhe/luoke)")
    args = parser.parse_args()

    answer = query(args.q, verbose=not args.quiet, product=args.product)
    print("\n" + "=" * 60)
    print("最终回答:")
    print("=" * 60)
    print(answer)


if __name__ == "__main__":
    main()
