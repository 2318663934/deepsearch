"""
LLM 调用模块 — 双客户端（llama.cpp 本地 + MiniMax-M3 云端）

借鉴自 E:\\行业稿件写作\\code\\rag_writer\\llm_client.py 的 BaseLLMClient 模式。
本项目要求：
- LlamaCppClient：OpenAI 兼容协议，调本地 llama.cpp server (默认 :8080)
  - 当前部署模型：Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf
  - 该模型带 thinking chain（reasoning_content），max_tokens 需要给足
  - 用于简单任务（抽取/分类/置信度）
- MiniMaxClient：Anthropic 兼容协议，调云端 MiniMax-M3，用于复杂任务（推理/对话/回答）
"""
from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from dotenv import load_dotenv

# 加载 .env（从项目根目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass
class LLMResponse:
    """LLM 响应统一结构"""

    content: str
    model: str
    usage: Dict[str, int] = field(default_factory=dict)
    raw_response: Optional[Dict[str, Any]] = None
    elapsed_ms: int = 0


class BaseLLMClient(ABC):
    """LLM 客户端基类"""

    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """生成文本"""

    @abstractmethod
    def generate_with_system(
        self, system_prompt: str, user_prompt: str, **kwargs
    ) -> LLMResponse:
        """带系统提示词的生成"""


# ---------------------------------------------------------------------------
# llama.cpp 本地客户端（OpenAI 兼容协议）
# ---------------------------------------------------------------------------


class LlamaCppClient(BaseLLMClient):
    """本地 llama.cpp server (默认 :8080, OpenAI 兼容协议 /v1)

    当前部署模型：Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf
    该模型带 thinking chain（reasoning_content），max_tokens 必须给到 4000+ 才能看到实际回答。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,  # 默认值；thinking 关闭后不再需要 4000
        timeout: int = 180,
    ):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "请安装 openai 库: pip install openai"
            ) from exc

        self.client = OpenAI(
            api_key=api_key or os.getenv("LLAMACPP_API_KEY", "llamacpp"),
            base_url=base_url or os.getenv(
                "LLAMACPP_BASE_URL", "http://192.168.100.211:8080/v1"
            ),
            timeout=timeout,
        )
        # 模型名默认空，让 llama.cpp 用它加载的默认模型
        # 但 .env 里我们填了具体 gguf 名，优先用环境变量
        self.model = model or os.getenv("LLAMACPP_MODEL", "")
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _call(self, messages, temperature, max_tokens) -> tuple[Any, int]:
        t0 = time.time()
        # 指数退避重试
        last_err = None
        for attempt in range(3):
            try:
                kwargs = dict(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    # 关闭 thinking chain：避免 Qwen3.6 把 JSON 草稿写到 reasoning_content
                    # 而 content 为空导致抽取失败
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                if self.model:
                    kwargs["model"] = self.model
                resp = self.client.chat.completions.create(**kwargs)
                elapsed = int((time.time() - t0) * 1000)
                return resp, elapsed
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"llama.cpp 调用失败（已重试3次）: {last_err}")

    def _extract_content(self, resp) -> tuple[str, str]:
        """提取 (content, reasoning_content)。llama.cpp 的 thinking 模型两者都返回。"""
        if not resp.choices:
            return "", ""
        msg = resp.choices[0].message
        content = getattr(msg, "content", "") or ""
        reasoning = getattr(msg, "reasoning_content", "") or ""
        return content, reasoning

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        temperature = kwargs.get("temperature", self.temperature)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        resp, elapsed = self._call(
            [{"role": "user", "content": prompt}], temperature, max_tokens
        )
        content, reasoning = self._extract_content(resp)
        # 如果 content 为空但 reasoning 有内容，合并到 content（避免 thinking 模型"答非所问"）
        effective = content if content else reasoning
        return LLMResponse(
            content=effective,
            model=getattr(resp, "model", self.model),
            usage={
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            },
            raw_response={"content": content, "reasoning_content": reasoning},
            elapsed_ms=elapsed,
        )

    def generate_with_system(
        self, system_prompt: str, user_prompt: str, **kwargs
    ) -> LLMResponse:
        temperature = kwargs.get("temperature", self.temperature)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        resp, elapsed = self._call(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature,
            max_tokens,
        )
        content, reasoning = self._extract_content(resp)
        effective = content if content else reasoning
        return LLMResponse(
            content=effective,
            model=getattr(resp, "model", self.model),
            usage={
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            },
            raw_response={"content": content, "reasoning_content": reasoning},
            elapsed_ms=elapsed,
        )


# 向后兼容：保留 OllamaClient 别名（指向 LlamaCppClient）
OllamaClient = LlamaCppClient


# ---------------------------------------------------------------------------
# MiniMax-M3 云端客户端（Anthropic 兼容协议）
# ---------------------------------------------------------------------------


class MiniMaxClient(BaseLLMClient):
    """云端 MiniMax-M3（Anthropic 兼容协议，base_url=https://api.minimaxi.com/anthropic）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
        timeout: int = 60,
    ):
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ImportError(
                "请安装 anthropic 库: pip install anthropic"
            ) from exc

        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("未设置 ANTHROPIC_API_KEY")

        self.client = Anthropic(
            api_key=api_key,
            base_url=base_url
            or os.getenv("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic"),
            timeout=timeout,
        )
        self.model = model or os.getenv("ANTHROPIC_MODEL", "MiniMax-M3")
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _call(self, system, messages, temperature, max_tokens) -> tuple[Any, int]:
        t0 = time.time()
        last_err = None
        for attempt in range(3):
            try:
                kwargs = dict(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=messages,
                )
                if system:
                    kwargs["system"] = system
                resp = self.client.messages.create(**kwargs)
                elapsed = int((time.time() - t0) * 1000)
                return resp, elapsed
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"MiniMax-M3 调用失败（已重试3次）: {last_err}")

    def _extract_text(self, resp) -> str:
        if not resp.content:
            return ""
        parts = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        temperature = kwargs.get("temperature", self.temperature)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        resp, elapsed = self._call(
            None,
            [{"role": "user", "content": prompt}],
            temperature,
            max_tokens,
        )
        return LLMResponse(
            content=self._extract_text(resp),
            model=self.model,
            usage={
                "input_tokens": getattr(resp.usage, "input_tokens", 0),
                "output_tokens": getattr(resp.usage, "output_tokens", 0),
            },
            elapsed_ms=elapsed,
        )

    def generate_with_system(
        self, system_prompt: str, user_prompt: str, **kwargs
    ) -> LLMResponse:
        temperature = kwargs.get("temperature", self.temperature)
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        resp, elapsed = self._call(
            system_prompt,
            [{"role": "user", "content": user_prompt}],
            temperature,
            max_tokens,
        )
        return LLMResponse(
            content=self._extract_text(resp),
            model=self.model,
            usage={
                "input_tokens": getattr(resp.usage, "input_tokens", 0),
                "output_tokens": getattr(resp.usage, "output_tokens", 0),
            },
            elapsed_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# 工具：JSON 容错解析
# ---------------------------------------------------------------------------


def parse_json_safe(text: str, max_retry_with_llm: Optional[BaseLLMClient] = None) -> Any:
    """
    从 LLM 输出中安全提取 JSON。
    - 优先尝试 json.loads
    - 失败时尝试截取第一个 { ... } 块
    - 仍失败时返回 None（调用方需处理）
    """
    if not text:
        return None
    text = text.strip()

    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 提取 markdown 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. 提取第一个 { ... } 块（贪婪匹配到最后一个 }）
    m = re.search(r"(\{.*\})", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 4. 提取第一个 [ ... ] 块
    m = re.search(r"(\[.*\])", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# 测试入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys

    if "--llamacpp" in sys.argv or "--ollama" in sys.argv or len(sys.argv) == 1:
        print("=== llama.cpp 本地客户端测试 ===")
        try:
            c = LlamaCppClient()
            r = c.generate("你好，请用一句话介绍你自己。", max_tokens=4000)
            print(f"模型: {r.model}")
            print(f"耗时: {r.elapsed_ms}ms")
            print(f"用量: {r.usage}")
            print(f"响应: {r.content[:200]}")
            if r.raw_response and r.raw_response.get("reasoning_content"):
                print(f"thinking 摘要: {r.raw_response['reasoning_content'][:150]}...")
        except Exception as e:
            print(f"[llama.cpp] 错误: {e}")
        print()

    if "--minimax" in sys.argv or len(sys.argv) == 1:
        print("=== MiniMax-M3 云端客户端测试 ===")
        try:
            c = MiniMaxClient()
            r = c.generate("你好，请用一句话介绍你自己。", max_tokens=100)
            print(f"模型: {r.model}")
            print(f"耗时: {r.elapsed_ms}ms")
            print(f"用量: {r.usage}")
            print(f"响应: {r.content[:200]}")
        except Exception as e:
            print(f"[MiniMax] 错误: {e}")
