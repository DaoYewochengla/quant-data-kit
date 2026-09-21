# -*- coding: utf-8 -*-
"""作为库使用 quant-data-kit 的示例。

运行：python examples/basic_usage.py
"""
from pathlib import Path

from qdk.categories import CATEGORIES, get_category_sources, get_universe
from qdk.checker import check_category
from qdk.sources import EmptyResult, fetch_with_fallback
from qdk.store import DataStore
from qdk.symbols import normalize

DATA_DIR = Path("./data")


def demo_symbols():
    """1. 代码规范化 —— 各种写法都能统一。"""
    print("=== 代码规范化 ===")
    for raw in ("113050", "sh113050", "113050.SH", 128145):
        print(f"  {raw!r:>12} -> {normalize(raw)}")


def demo_fetch_one():
    """2. 抓取单个标的（自动多源降级）。"""
    print("\n=== 抓取单只可转债 ===")
    sources = get_category_sources("cb")
    try:
        res = fetch_with_fallback(sources, "110075", sleep=0.3)
    except EmptyResult as e:
        print(f"  未取到数据: {e}")
        return

    print(f"  来源: {res.source} | 行数: {res.rows}")
    print(f"  区间: {res.df['date'].min():%Y-%m-%d} -> {res.df['date'].max():%Y-%m-%d}")
    print(res.df.tail(3).to_string(index=False))


def demo_store_and_check():
    """3. 落盘 + 体检。"""
    print("\n=== 落盘与体检 ===")
    store = DataStore(DATA_DIR)

    # 先看目录里已有什么
    for cat in CATEGORIES:
        s = store.stats(cat)
        print(f"  {cat:>6}: {s['files']} 个文件, {s['total_rows']} 行, "
              f"最新 {s['latest_date']}")

    print()
    rep = check_category(store, "cb")
    print(rep.summary())


def demo_universe():
    """4. 获取标的名单（注意：个股名单需分页抓取，约 10~15 秒）。"""
    print("\n=== 标的名单 ===")
    for cat, meta in CATEGORIES.items():
        try:
            uni = get_universe(cat)
            n_delisted = int(uni["delisted_flag"].sum()) if "delisted_flag" in uni else 0
            print(f"  {cat:>6} ({meta['label']}): {len(uni)} 只"
                  f"（其中退市/到期 {n_delisted}）")
        except Exception as e:
            print(f"  {cat:>6}: 获取失败 {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    demo_symbols()
    demo_fetch_one()
    demo_store_and_check()
    demo_universe()
