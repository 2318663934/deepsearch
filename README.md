# 王者荣耀产品环境 — MVP

> 基于 Karpathy LLM-Wiki 模式的产品专属知识库。
> 完整方案见 [CLAUDE.md](CLAUDE.md)；规划见 `~/.claude/plans/buzzing-watching-micali.md`。

## 目标

构建一个**为LLM的"产品专属长期记忆"**，从根因上解决幻觉问题。
当LLM被问及王者荣耀的任何信息时，它先浏览 `wiki/` 下的md文件再回答，
而不是凭"通用知识"自由发挥。

## 目录速查

- `wiki/` — 知识库本体（git管理）
- `raw/` — 原始资料（不可变）
- `scripts/` — 维护脚本
- `state/` — 增量状态
- `CLAUDE.md` — 宪法（命名/置信度/写入/对话协议）

## 快速开始

### 安装依赖

```bash
pip install openai anthropic requests beautifulsoup4 pyyaml python-dotenv
```

### 配置密钥

```bash
cp .env.example .env
# 编辑 .env 填入密钥
```

### 验证连通性

```bash
python -c "from scripts.lib_llm import LlamaCppClient, MiniMaxClient; print(LlamaCppClient().generate('ping').content[:50])"
```

## 阶段验证清单

- [ ] **阶段 0**：双LLM联通（Ollama + MiniMax-M3）
- [ ] **阶段 1**：手工放文章 → ingest → query 最小链路
- [ ] **阶段 2**：爬虫 + 增量 + 多源冲突门控
- [ ] **阶段 3**：对话式更新 + Lint
- [ ] **阶段 4**：产品概述子集覆盖

## 当前进度

**阶段 0：脚手架**（进行中）
