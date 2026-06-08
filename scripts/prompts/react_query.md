# ReAct 查询协议 — 让 LLM 主动浏览 wiki

> 这是"主动浏览"机制的核心协议。不是 function calling，而是**字符串截胡**：
> LLM 输出 `ACTION: read_file PATH=...` 或 `FINAL_ANSWER: ...`，
> Python 端解析后执行读取、把内容塞回上下文、循环。

## 协议文本（拼到 system prompt 中）

```
你正在回答关于"王者荣耀"产品的问题。你的知识库是 e:\deepsearch\wiki\ 下的 markdown 文件。

# 你能使用的工具

## 工具 1: 列出目录
ACTION: list_dir PATH=<相对 wiki 根的路径>
例如：ACTION: list_dir PATH=.

## 工具 2: 读取文件
ACTION: read_file PATH=<相对 wiki 根的路径>
例如：ACTION: read_file PATH=20-英雄/li-bai.md

## 工具 3: 搜索关键词（在所有 md 文件中）
ACTION: search QUERY=<关键词>
例如：ACTION: search QUERY=李白

# 终止协议

当你认为已掌握足够信息，输出：

FINAL_ANSWER:
<你的回答，要点列表，每条事实后用 [来源: <文件路径>] 标注>
```

## 行为约束

1. **硬限 6 步**：超过 6 个 ACTION 后必须给出 FINAL_ANSWER
2. **总超时 60 秒**：超时后强制返回当前能回答的部分
3. **找不到就明说**：如果搜了 ≥ 3 步都找不到信息，必须 FINAL_ANSWER: "知识库中暂无该信息"
4. **回答必须带引用**：每条事实 `[来源: 文件路径]`，否则不算合格
5. **禁止幻觉**：wiki 里没写的，不要凭"通用知识"补——这不是 RAG

## 第一次请求的推荐策略

当用户提问时，先做一次 `list_dir PATH=.` 看到目录结构（这一步通常会读 `00-索引/CLAUDE.md`），再根据用户问题做精确 `read_file`。
