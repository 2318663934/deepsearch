# 王者荣耀产品环境 — 项目宪法

> 这是整个项目的"宪法"。任何脚本、任何LLM的写入行为、任何用户的对话纠正，都必须遵守本文件。
> 本项目基于 Karpathy 的 LLM-Wiki 模式（[原始gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)）。

---

## 1. 目录结构（不可变）

```
e:\deepsearch\
├── raw/                  # 第1层：原始资料（不可变，LLM永不修改）
│   └── wangzhe/{source}/{date}/*.{html,txt}
├── wiki/                 # 第2层：知识库本体（LLM全权维护）
│   ├── 00-索引/          # 总入口，目录
│   ├── 10-产品概述/      # 产品定位/发展史
│   ├── 20-英雄/          # 英雄信息
│   ├── 30-技能机制/      # 通用机制说明
│   └── 99-待审核/        # 冲突/低置信度条目
├── scripts/              # 维护脚本
├── state/                # 增量状态
└── .env                  # 密钥（不入git）
```

**硬性约束**：
- 目录层级 ≤ 3 层
- 每个 md 文件 < 300 行
- 违反约束的写入必须先警告再写

---

## 2. 文件命名规范

**格式**：`{类型前缀}-{子类型}/{英文slug}.md`

**示例**：
- `20-英雄/li-bai.md`（李白）
- `20-英雄/hou-yi.md`（后羿）
- `30-技能机制/cooling-system.md`（冷却系统）
- `10-产品概述/game-positioning.md`（游戏定位）
- `10-产品概述/development-history.md`（发展历程）

**规则**：
- 路径用英文slug（小写、连字符分隔），跨平台稳定
- 中文显示名放 frontmatter `title`
- 类型前缀必须在 `00-99` 范围内，体现分类层级
- 新增类型时需要扩展本节，不能随意加

---

## 3. frontmatter 字段定义

每个 md 文件必须以 YAML frontmatter 开头，字段如下：

```yaml
---
title: 李白                                    # 必填，中文显示名
type: hero                                      # 必填，hero|skill|overview|stub
slug: li-bai                                    # 必填，英文slug，与文件名一致
aliases: [libai, 谪仙, 青莲剑仙]               # 可选，其他称呼
sources:                                        # 必填，来源URL或路径列表
  - https://pvp.qq.com/hero/li-bai.html
  - raw/wangzhe/manual/2026-06-08.txt
confidence: 0.85                                # 必填，0-1
confidence_reason: 跨3个独立来源一致，技能数值有原文支撑  # 必填
created: 2026-06-08T10:30:00+08:00             # 必填
updated: 2026-06-08T10:30:00+08:00             # 必填，每次修改更新
last_verified: 2026-06-08T10:30:00+08:00       # 必填，最近一次人工/外部校验时间
status: verified                                # 必填，verified|pending|deprecated
related:                                        # 可选，关联文件路径
  - 30-技能机制/cooling-system.md
tags: [战士, 刺客, 打野]                      # 可选
version: 1                                      # 必填，每次实质修改+1
---
```

**规则**：
- 修改正文时必须更新 `updated` 字段
- 实质内容变更（不是格式微调）必须 `version` +1
- 人工校验后必须更新 `last_verified`

---

## 4. 置信度评分规则（必须严格执行）

### 4.1 三档阈值

| 置信度区间 | 处理方式 |
|-----------|----------|
| `confidence >= 0.7` | 自动写入 wiki 既有页（若无冲突）/ 创建新页 |
| `0.4 <= confidence < 0.7` | 写入 `wiki/99-待审核/` 等用户裁决 |
| `confidence < 0.4` | 丢弃 + 写入日志 |

### 4.2 LLM 自评 + 硬规则校准

LLM 输出 `confidence` 字段时，Python 端必须叠加以下硬规则：

1. **必须有至少 1 个 source**（URL 或 raw 文件路径）—— 否则强制 `confidence < 0.4`
2. **数值类字段（冷却/伤害/价格等）必须能从原文找到对应字符串** —— 否则该字段所在条目的 `confidence` 降 0.2
3. **与 wiki 既有页冲突时自动降一档**（如 0.85 → 0.65，落入待审核）

### 4.3 防止 LLM 自评偏高

LLM 自我评分经验上偏高，必须用外部锚点校准。如果某条评分 ≥ 0.95，必须有 ≥ 2 个独立来源支持。

---

## 5. 写入流程（Ingest）

```
读取 raw 文件
    ↓
调用 LlamaCppClient 抽取（prompt: scripts/prompts/extract.md）
    ↓
调用 LlamaCppClient 评置信度（prompt: scripts/prompts/extract.md 的 confidence 部分）
    ↓
Python 端叠加硬规则
    ↓
判断阈值：
    ≥ 0.7 → 写入对应 wiki/ 子目录
    0.4-0.7 → 写入 99-待审核/
    < 0.4 → 丢弃 + 写 state/discarded.log
    ↓
git add + git commit
```

---

## 6. 冲突处理

- **同 type+slug** 视为同一实体
- 抽取时若发现 wiki 既有页中存在相同字段但内容不同 → 标记为冲突，新条目入 `99-待审核/`
- 冲突解决：人工裁决（编辑 md 或 `git mv` 合并）

---

## 7. 用户对话协议

当用户输入形如以下指令时，对应处理：

| 用户输入模式 | 处理动作 |
|-------------|----------|
| "把X改成Y" / "X是Y，不是Z" | 定位到对应 md → 改字段 → 更新 `updated` 和 `version` → git commit |
| "X有误" / "X是错的" | 标记对应字段 `status: pending` → 写 `99-待审核/修正_X.md` |
| "加一条X" | 在对应 md 的相关小节追加 → 更新 frontmatter |
| "X和Y冲突，以哪个为准？" | 调用 LLM 列出所有相关来源 → 人工决定 |
| 问"X是什么？" | 进入 query 模式，不修改任何文件 |

**所有修改动作必须 git commit，commit message 格式**：
```
update: <简述> [<文件路径>]
```
例如：`update: 李白1技能冷却从8s改为12s [20-英雄/li-bai.md]`

---

## 8. Lint 规则（健康检查）

`scripts/lint.py` 跑出后必须检查以下项：

1. **矛盾检测**：同 slug 文件的相同字段值冲突
2. **过期检测**：`last_verified` 超过 90 天的文件 → 警告
3. **孤立页**：不被任何 `related` 引用、且不被 `00-索引/CLAUDE.md` 列出的页面
4. **缺失交叉引用**：出现英雄/机制名但未在 `related` 中建立链接
5. **frontmatter 完整性**：必填字段缺失

---

## 9. 增量更新

- 每次 crawl 完成后，更新 `state/last_crawl.json` 记录每个 source 的最后爬取时间
- 下次 crawl 从该时间点开始

---

## 10. 不可妥协的约束

1. **raw/ 下任何文件 LLM 永远不修改**
2. **wiki/ 下的修改必须有 git commit 留痕**
3. **冲突条目必须入 `99-待审核/`，不得覆盖既有页**
4. **任何 LLM 自评置信度 < 0.4 的条目不得进入 wiki**
5. **目录层级 ≤ 3 层，单文件 < 300 行**
