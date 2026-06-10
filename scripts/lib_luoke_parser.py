"""
lib_luoke_parser.py — 洛克王国世界 B 站游戏 wiki 模板 wikitext 解析器

数据源: wiki.biligame.com/rocom/(标准 MediaWiki + Semantic MediaWiki + Lua)
特点: 100% 结构化,wikitext 中嵌 {{精灵信息/兼容|key=value|...}} 模板,
直接 regex 抽参数即可,**不需要 LLM 抽取**(精度 100%,免 token)。

模板字段集(从站点抓样例推得):
- {{精灵信息/兼容|...}}: 精灵名/精灵形态/主属性/2属性/类型/阶段/生命/物攻/魔攻/物防/魔防/速度/特性/分布地区/技能/进化条件
- {{技能信息|...}}: 技能名称/属性/技能类别/耗能/威力/效果/描述/技能版本
- 未来可能扩展: 道具/任务/地图模板
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 模板字段顺序(仅文档用,实际解析按 key=value)
# ---------------------------------------------------------------------------

PET_TEMPLATE = "精灵信息/兼容"
SKILL_TEMPLATE = "技能信息"
ITEM_TEMPLATE = "物品信息"
QUEST_TEMPLATE = "任务信息"

PET_EXPECTED_FIELDS = [
    "精灵名称", "精灵形态", "地区形态名称", "精灵初阶名称", "是否有异色",
    "精灵阶段", "精灵类型", "精灵描述", "主属性", "2属性", "特性", "特性描述",
    "生命", "物攻", "魔攻", "物防", "魔防", "速度", "体型", "重量",
    "分布地区", "图鉴课题", "课题技能石", "技能", "技能解锁等级",
    "血脉技能", "可学技能石", "宠物立绘形态", "进化条件",
]

SKILL_EXPECTED_FIELDS = [
    "技能名称", "属性", "技能类别", "耗能", "威力", "效果", "描述", "技能版本",
]

ITEM_EXPECTED_FIELDS = [
    "物品名称", "稀有度", "主分类", "次分类", "用途", "描述", "来源", "icon", "道具版本",
]

QUEST_EXPECTED_FIELDS = [
    "任务序号", "任务分类", "任务名称", "任务地点", "任务描述", "任务奖励", "任务图片", "任务备注", "任务归属",
]


# ---------------------------------------------------------------------------
# 通用模板参数解析
# ---------------------------------------------------------------------------


def _strip_nested_templates(text: str) -> str:
    """把 text 中的 {{...}} 嵌套模板替换为空字符串,避免 key=value 解析受嵌套影响。"""
    out = []
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _split_top_level_pipes(body: str) -> List[str]:
    """按顶层 | 切分(不切嵌套模板里的 |)。"""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    bracket_depth = 0
    for ch in body:
        if ch == "{" and buf and buf[-1] == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}" and depth > 0:
            depth -= 1
            buf.append(ch)
        elif ch == "[":
            bracket_depth += 1
            buf.append(ch)
        elif ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
            buf.append(ch)
        elif ch == "|" and depth == 0 and bracket_depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_kv_params(params: List[str]) -> Dict[str, str]:
    """把 ['精灵名=科基', '主属性=火', '未命名值'] 解析为 {'精灵名': '科基', ...}。"""
    fields: Dict[str, str] = {}
    for p in params:
        p = p.strip()
        if not p:
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            fields[k.strip()] = v.strip()
        else:
            # 位置参数(无名),归到 _unnamed 下
            fields.setdefault("_unnamed", []).append(p.strip())  # type: ignore
    return fields


def parse_template_params(wikitext: str, template_name: str) -> Optional[Dict[str, str]]:
    """从 wikitext 抓出指定模板的参数。

    返回 None 表示该模板未在 wikitext 中出现。
    """
    # 模板起始: {{TemplateName| ... }}
    pattern = re.compile(
        r"\{\{\s*" + re.escape(template_name) + r"\s*\|(.+?)\}\}",
        re.DOTALL,
    )
    m = pattern.search(wikitext)
    if not m:
        return None
    body = m.group(1)
    body = _strip_nested_templates(body)
    params = _split_top_level_pipes(body)
    return _parse_kv_params(params)


# ---------------------------------------------------------------------------
# 精灵 / 技能专用解析
# ---------------------------------------------------------------------------


def parse_pet_wikitext(wikitext: str) -> Optional[Dict[str, Any]]:
    """解析 {{精灵信息/兼容|...}},返回标准化 dict。

    返回 None 表示 wikitext 不含该模板。
    """
    raw = parse_template_params(wikitext, PET_TEMPLATE)
    if not raw:
        return None

    # 必填字段:精灵名称
    name = raw.get("精灵名称")
    if not name:
        return None

    # 数值字段(种族值)尝试转 int
    stats = {}
    for k in ("生命", "物攻", "魔攻", "物防", "魔防", "速度"):
        v = raw.get(k)
        if v and v.lstrip("-").isdigit():
            stats[k] = int(v)
        else:
            stats[k] = v  # 可能为空或非数字字符串

    return {
        "entity_type": "pet",
        "name": name,
        "form": raw.get("精灵形态"),
        "form_name": raw.get("地区形态名称"),  # 仅异形态时有,主形态为空
        "type": raw.get("精灵类型"),
        "phase": raw.get("精灵阶段"),
        "main_attr": raw.get("主属性"),
        "sub_attr": raw.get("2属性"),
        "trait": raw.get("特性"),
        "trait_desc": raw.get("特性描述"),
        "description": raw.get("精灵描述"),
        "stats": stats,
        "region": raw.get("分布地区"),
        "skills": raw.get("技能"),
        "evolution": raw.get("进化条件"),
        "raw_fields": raw,  # 完整原始数据,方便后续扩展
    }


def parse_skill_wikitext(wikitext: str) -> Optional[Dict[str, Any]]:
    """解析 {{技能信息|...}},返回标准化 dict。"""
    raw = parse_template_params(wikitext, SKILL_TEMPLATE)
    if not raw:
        return None

    name = raw.get("技能名称")
    if not name:
        return None

    return {
        "entity_type": "skill",
        "name": name,
        "attr": raw.get("属性"),
        "category": raw.get("技能类别"),
        "cost": raw.get("耗能"),
        "power": raw.get("威力"),
        "effect": raw.get("效果"),
        "description": raw.get("描述"),
        "version": raw.get("技能版本"),
        "raw_fields": raw,
    }


def parse_item_wikitext(wikitext: str) -> Optional[Dict[str, Any]]:
    """解析 {{物品信息|...}},返回标准化 dict。"""
    raw = parse_template_params(wikitext, ITEM_TEMPLATE)
    if not raw:
        return None

    name = raw.get("物品名称")
    if not name:
        return None

    return {
        "entity_type": "item",
        "name": name,
        "rarity": raw.get("稀有度"),
        "main_category": raw.get("主分类"),
        "sub_category": raw.get("次分类"),
        "use": raw.get("用途"),
        "description": raw.get("描述"),
        "source": raw.get("来源"),
        "icon": raw.get("icon"),
        "version": raw.get("道具版本"),
        "raw_fields": raw,
    }


def parse_quest_wikitext(wikitext: str) -> Optional[Dict[str, Any]]:
    """解析 {{任务信息|...}},返回标准化 dict。"""
    raw = parse_template_params(wikitext, QUEST_TEMPLATE)
    if not raw:
        return None

    name = raw.get("任务名称")
    if not name:
        return None

    return {
        "entity_type": "quest",
        "name": name,
        "quest_id": raw.get("任务序号"),
        "category": raw.get("任务分类"),
        "location": raw.get("任务地点"),
        "description": raw.get("任务描述"),
        "reward": raw.get("任务奖励"),
        "image": raw.get("任务图片"),
        "note": raw.get("任务备注"),
        "owner": raw.get("任务归属"),
        "raw_fields": raw,
    }


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


ROMAN_NUMERALS = {
    # 罗马数字 全角 + 半角 大写 + 小写 共 24 个
    "Ⅰ": "one", "Ⅱ": "two", "Ⅲ": "three", "Ⅳ": "four", "Ⅴ": "five",
    "Ⅵ": "six", "Ⅶ": "seven", "Ⅷ": "eight", "Ⅸ": "nine", "Ⅹ": "ten",
    "Ⅺ": "eleven", "Ⅻ": "twelve",
    "ⅰ": "one", "ⅱ": "two", "ⅲ": "three", "ⅳ": "four", "ⅴ": "five",
    "ⅵ": "six", "ⅶ": "seven", "ⅷ": "eight", "ⅸ": "nine", "ⅹ": "ten",
    "ⅺ": "eleven", "ⅻ": "twelve",
    # ASCII 罗马数字(全大写或全小写)
    "I": "one", "II": "two", "III": "three", "IV": "four", "V": "five",
    "VI": "six", "VII": "seven", "VIII": "eight", "IX": "nine", "X": "ten",
    "XI": "eleven", "XII": "twelve",
    "i": "one", "ii": "two", "iii": "three", "iv": "four", "v": "five",
    "vi": "six", "vii": "seven", "viii": "eight", "ix": "nine", "x": "ten",
    "xi": "eleven", "xii": "twelve",
}


def _preprocess_for_slugify(name: str) -> str:
    """slugify 前的预处理:把罗马数字/特殊符号转成拼音可识别的字。

    用 regex word boundary,避免"III"被"III"→"three"和"I"分别替换的双重命中。
    """
    # 按长度倒序(先长后短,避免短 key 吃掉长 key 的前缀)
    for k in sorted(ROMAN_NUMERALS.keys(), key=len, reverse=True):
        v = ROMAN_NUMERALS[k]
        # 用 \b 对 ASCII 字符,用普通 replace 对 unicode
        if k.isascii():
            name = re.sub(rf"\b{re.escape(k)}\b", v, name)
        else:
            name = name.replace(k, v)
    return name


def slugify_zh(name: str) -> str:
    """中文名 → 拼音 slug(无外部依赖回退)。

    优先 pypinyin,失败回退 unicode 转义序列。

    多音字消歧:如果拼音后 slug 字符数 < 8(短 slug 易撞车),
    追加原 name 首个汉字的 unicode hex 后 4 位做 disambiguation。
    例: "冲击" → chong-ji(8 字符,不需要加); "虫击" → chong-ji(8 字符,加 866b)
    """
    name = _preprocess_for_slugify(name)
    try:
        from pypinyin import lazy_pinyin

        s = "-".join(lazy_pinyin(name)).lower()
        s = re.sub(r"[^a-z0-9-]", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        # 短 slug(<=8 字符)易撞车,加 unicode 末 4 位 disambiguation
        if s and len(s) <= 8 and name:
            # 取 name 中第一个非 ASCII 字符的 hex 后 4 位
            for ch in name:
                if not ch.isascii():
                    s = f"{s}-{ch.encode('utf-8').hex()[-4:]}"
                    break
        return s or "unknown"
    except ImportError:
        # 回退:取每个中文字符 unicode 码点的 hex 后 4 位
        out: List[str] = []
        for ch in name:
            if ch.isascii() and ch.isalnum():
                out.append(ch.lower())
            elif ch in (" ", "_", "-"):
                out.append("-")
            else:
                out.append(f"u{ord(ch):04x}")
        s = "-".join(out)
        s = re.sub(r"-+", "-", s).strip("-")
        return s or "unknown"
