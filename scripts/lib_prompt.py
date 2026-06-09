"""
lib_prompt.py — 产品无关的 prompt 加载器(单文件维护,运行期按 product 替换占位符)

为什么需要: extract.md / react_query.md 等 prompt 在硬编码"王者荣耀"时无法服务
第二个产品(洛克王国世界)。用 {{PRODUCT}} 占位符 + 运行期替换,可以单文件维护
两套产品的 prompt,避免 prompt 漂移。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "scripts" / "prompts"

# 产品 ID → 显示名(用于 prompt 文案)
PRODUCT_DISPLAY = {
    "wangzhe": "王者荣耀",
    "luoke": "洛克王国世界",
}

# 产品 ID → 标准 slug(用于 prompt 中"必须使用标准 slug"这种规则)
PRODUCT_SLUG = {
    "wangzhe": "wangzhe-rongyao",
    "luoke": "luoke-guowang-shijie",
}


def load_product_prompt(name: str, product: str) -> str:
    """加载 prompt 模板并替换 {{PRODUCT}} / {{PRODUCT_SLUG}} 占位符。

    Args:
        name: prompt 文件名,如 "extract.md"
        product: 产品 ID,如 "wangzhe" 或 "luoke"

    Returns:
        替换后的 prompt 文本

    Raises:
        KeyError: product 不在 PRODUCT_DISPLAY 中
        FileNotFoundError: prompt 文件不存在
    """
    if product not in PRODUCT_DISPLAY:
        raise KeyError(f"未知 product: {product},已知: {list(PRODUCT_DISPLAY)}")
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    text = text.replace("{{PRODUCT}}", PRODUCT_DISPLAY[product])
    text = text.replace("{{PRODUCT_SLUG}}", PRODUCT_SLUG[product])
    return text
