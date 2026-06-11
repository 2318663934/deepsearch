"""
scripts/raw_scanner.py — raw/ 扫描调度(找未入仓的 raw 文件, 批量触发 ingest)

用法:
  python -m scripts.raw_scanner                    # 扫描全部,仅报告
  python -m scripts.raw_scanner --product luoke    # 按产品扫描
  python -m scripts.raw_scanner --ingest           # 扫描 + 自动入仓
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = _PROJECT_ROOT / "raw"


def scan_raw(product: Optional[str] = None) -> List[Dict[str, str]]:
    """
    扫描 raw/ 目录,返回未处理(无 .processed 标记)的文件清单。

    Returns:
        [{product, source, date, filename, bytes, ext}]
    """
    pending: List[Dict[str, str]] = []
    roots = [RAW_ROOT / product] if product else sorted(RAW_ROOT.iterdir())
    for prod_dir in roots:
        if not prod_dir.is_dir():
            continue
        if prod_dir.name.startswith("."):
            continue
        prod_name = prod_dir.name
        for src_dir in sorted(prod_dir.iterdir()):
            if not src_dir.is_dir():
                continue
            source = src_dir.name
            for date_dir in sorted(src_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                date = date_dir.name
                for raw_file in sorted(date_dir.glob("*")):
                    if raw_file.suffix not in (".wikitext", ".txt"):
                        continue
                    if raw_file.with_suffix(raw_file.suffix + ".processed").exists():
                        continue
                    pending.append({
                        "product": prod_name,
                        "source": source,
                        "date": date,
                        "filename": raw_file.name,
                        "bytes": raw_file.stat().st_size,
                        "ext": raw_file.suffix,
                    })
    return pending


def scan_stats(pending: List[Dict[str, str]]) -> str:
    """汇总 pending 列表为文本报告。"""
    lines = [f"未处理 raw 文件: {len(pending)} 个"]
    by_product: Dict[str, int] = {}
    for p in pending:
        by_product[p["product"]] = by_product.get(p["product"], 0) + 1
    for prod, n in sorted(by_product.items()):
        lines.append(f"  {prod}: {n}")
    return "\n".join(lines)


def run_ingest_batch(pending: List[Dict[str, str]], product_filter: Optional[str] = None) -> Dict[str, int]:
    """
    对 pending 批量调 ingest_one。

    Returns: {written, replaced, review, discarded, conflict, failed, skipped}
    """
    from scripts.ingest import ingest_one
    from scripts.lib_llm import LlamaCppClient

    client = LlamaCppClient()
    stats = {"written": 0, "replaced": 0, "review": 0, "discarded": 0, "conflict": 0, "failed": 0, "skipped": 0}

    for i, p in enumerate(pending, 1):
        if product_filter and p["product"] != product_filter:
            continue
        rp = RAW_ROOT / p["product"] / p["source"] / p["date"] / p["filename"]
        if not rp.exists():
            stats["skipped"] += 1
            continue
        print(f"  [{i}/{len(pending)}] {p['product']}/{p['source']}/{p['filename'][:40]}")
        try:
            res = ingest_one(rp, client, product=p["product"])
            status = res.get("status", "unknown")
            if status in stats:
                stats[status] += 1
            else:
                stats["failed"] += 1
        except Exception as e:
            print(f"    [error] {e}")
            stats["failed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="raw/ 扫描调度")
    parser.add_argument("--product", default=None, help="按产品过滤")
    parser.add_argument("--ingest", action="store_true", help="触发 ingest(否则仅报告)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    pending = scan_raw(product=args.product)
    print("=" * 60)
    print(scan_stats(pending))

    if not pending:
        print("所有 raw 文件均已入仓。")
        if args.json:
            print(json.dumps({"pending": 0}, ensure_ascii=False))
        return

    if args.json:
        print(json.dumps({
            "pending_count": len(pending),
            "by_product": {p: sum(1 for q in pending if q["product"] == p) for p in set(q["product"] for q in pending)},
        }, ensure_ascii=False, indent=2))

    if args.ingest:
        print(f"\n开始 ingest {len(pending)} 个文件...")
        stats = run_ingest_batch(pending, product_filter=args.product)
        print("\n入仓结果:")
        for k, v in sorted(stats.items()):
            if v:
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
