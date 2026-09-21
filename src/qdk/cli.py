# -*- coding: utf-8 -*-
"""quant-data-kit 命令行入口。

    qdk init    --data-dir ./data
    qdk fetch   --type cb,stock,etf [--start 20180101] [--limit 50]
    qdk update  --type cb,stock,etf
    qdk check   --type cb,stock,etf [--verbose]
    qdk info
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from . import __version__
from .categories import CATEGORIES, get_category_sources, get_universe
from .checker import check_category
from .sources import EmptyResult, fetch_with_fallback
from .store import DataStore
from .symbols import normalize, sanitize_filename

DEFAULT_DATA_DIR = "./data"


def _recent_trading_day(days_back: int = 4) -> pd.Timestamp:
    """返回"最近可能已有数据的交易日"（保守估计）。

    为什么不用"今天减 N 天"：周末与节假日不产生数据。若拿今天当基准，
    周五落盘的数据在周六运行时会永远被判为"未最新"，导致每天重复拉全量。
    这里回溯到最近一个工作日，并额外给 days_back 的缓冲以容忍节假日。
    """
    from datetime import timedelta

    today = pd.Timestamp.today().normalize()
    d = today - timedelta(days=days_back)
    while d.weekday() >= 5:  # 5=周六 6=周日
        d -= timedelta(days=1)
    return d


def _parse_types(s: str) -> list[str]:
    out = [t.strip() for t in s.split(",") if t.strip()]
    for t in out:
        if t not in CATEGORIES:
            raise SystemExit(f"未知类别 {t!r}，可选: {', '.join(CATEGORIES)}")
    return out


def cmd_init(args) -> int:
    store = DataStore(args.data_dir)
    print(f"数据目录已就绪: {store.root.resolve()}")
    for cat, meta in CATEGORIES.items():
        (store.root / cat).mkdir(parents=True, exist_ok=True)
        print(f"  {cat:>6} ({meta['label']}) -> {store.root / cat}")
    return 0


def cmd_info(args) -> int:
    print(f"quant-data-kit v{__version__}")
    print("\n可用类别:")
    for cat, meta in CATEGORIES.items():
        srcs = " -> ".join(meta["sources"].keys())
        print(f"  {cat:>6}  {meta['label']:<8}  数据源(优先级): {srcs}")
    print("\n重要: 本工具只采集公开数据，不含任何策略/因子/选股规则/投资建议。")
    return 0


def _run_fetch(store: DataStore, types: list[str], args, incremental: bool) -> int:
    total_ok = total_skip = total_fail = 0

    for cat in types:
        meta = CATEGORIES[cat]
        print(f"\n===== {meta['label']} ({cat}) — "
              f"{'增量更新' if incremental else '全量拉取'} =====")

        try:
            uni = get_universe(cat)
        except Exception as e:
            print(f"  [FATAL] 获取标的名单失败: {e}")
            total_fail += 1
            continue

        uni = uni.dropna(subset=["code"])
        if args.limit:
            # 抽样而非取头部：名单按代码升序，头部往往是新股/退市股，
            # 直接 head() 会得到一批只有 1 行数据的标的，无法反映真实情况。
            if len(uni) > args.limit:
                uni = uni.sample(n=args.limit, random_state=args.seed)
            else:
                uni = uni.head(args.limit)
        print(f"  标的数: {len(uni)}")

        sources = get_category_sources(cat)
        ok = skip = fail = 0

        for n, (_, row) in enumerate(uni.iterrows(), 1):
            code = normalize(row["code"])
            name = sanitize_filename(row.get("name", ""))

            # 已退市/已到期的标的：数据已封存，不再重拉。
            # 不加这一步的话，它们永远达不到"最近交易日"，
            # 会导致每次增量运行都白白重拉一大批死标的。
            if bool(row.get("delisted_flag", False)):
                skip += 1
                continue

            # 增量：已有数据且已覆盖到"最近一个交易日"则跳过
            if incremental:
                try:
                    last = store.last_date(cat, code, name)
                except Exception:
                    last = None
                if last is not None and last >= _recent_trading_day():
                    skip += 1
                    continue
                # 静默退市保护：名单仍标注为"现存"，但数据已长期停滞。
                # 这类标的（提前强赎、暂停上市）永远拿不到新数据，
                # 若不识别会造成同一批标的被无限重拉。
                if last is not None and last < pd.Timestamp.today().normalize() - pd.Timedelta(days=180):
                    skip += 1
                    continue

            try:
                res = fetch_with_fallback(sources, code, sleep=args.sleep,
                                         keep=meta.get("keep"))
            except EmptyResult as e:
                fail += 1
                if args.verbose:
                    print(f"  [EMPTY] {code} {name}: {str(e)[:150]}")
                continue
            except Exception as e:
                fail += 1
                print(f"  [ERROR] {code} {name}: {type(e).__name__}: {str(e)[:120]}")
                continue

            # 起始日期过滤
            if args.start:
                start = str(args.start)
                cut = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
                res.df = res.df[res.df["date"] >= cut]

            try:
                r = store.save(cat, code, name, res.df, incremental=incremental)
            except ValueError as e:
                fail += 1
                print(f"  [SKIP-EMPTY] {code}: {e}")
                continue

            ok += 1
            if args.verbose:
                print(f"  [OK] {code} {name} via {res.source} "
                      f"+{r['added_rows']} 行 (共{r['total_rows']})")

            if n % 25 == 0:
                print(f"  进度 {n}/{len(uni)} | 成功{ok} 跳过{skip} 失败{fail}", flush=True)

        print(f"  ✅ {cat} 完成: 成功 {ok} | 已最新跳过 {skip} | 失败 {fail}")
        total_ok += ok
        total_skip += skip
        total_fail += fail

    print(f"\n===== 总计: 成功 {total_ok} | 跳过 {total_skip} | 失败 {total_fail} =====")
    return 0 if total_fail == 0 else 1


def cmd_fetch(args) -> int:
    store = DataStore(args.data_dir)
    return _run_fetch(store, _parse_types(args.type), args, incremental=False)


def cmd_update(args) -> int:
    store = DataStore(args.data_dir)
    return _run_fetch(store, _parse_types(args.type), args, incremental=True)


def cmd_check(args) -> int:
    store = DataStore(args.data_dir)
    types = _parse_types(args.type)
    worst = 0
    for cat in types:
        rep = check_category(store, cat)
        print(rep.summary())
        if args.verbose and rep.issues:
            for i in rep.issues[: args.max_show]:
                print(f"    [{i['severity']}] {i['kind']} {i['code']}: {i['detail']}")
            if len(rep.issues) > args.max_show:
                print(f"    ... 另有 {len(rep.issues) - args.max_show} 条")
        if rep.errors:
            worst = 1
    return worst


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qdk",
        description="quant-data-kit — 中国市场公开行情数据采集与整理（纯数据工具，无策略/无建议）",
    )
    p.add_argument("--version", action="version", version=f"qdk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="数据目录")
        sp.add_argument("--type", default="cb", help="资产类别，逗号分隔: cb,stock,etf")
        sp.add_argument("--sleep", type=float, default=0.4, help="每次请求间隔秒数（限流保护）")
        sp.add_argument("--limit", type=int, default=0, help="最多处理多少个标的（调试用，0=全部）")
        sp.add_argument("--start", default="", help="起始日期 YYYYMMDD，如 20180101")
        sp.add_argument("--seed", type=int, default=42, help="抽样随机种子（--limit 时生效）")
        sp.add_argument("-v", "--verbose", action="store_true", help="输出每个标的的明细")

    sp = sub.add_parser("init", help="初始化数据目录")
    sp.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("fetch", help="全量拉取历史数据")
    add_common(sp)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("update", help="增量更新（推荐挂定时任务）")
    add_common(sp)
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("check", help="数据体检：发现缺口/空值/异常/重复")
    add_common(sp)
    sp.add_argument("--max-show", type=int, default=30, help="最多展示多少条问题")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("info", help="显示版本与可用类别")
    sp.set_defaults(func=cmd_info)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
