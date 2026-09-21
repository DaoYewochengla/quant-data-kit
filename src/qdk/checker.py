# -*- coding: utf-8 -*-
"""数据体检：找出"看起来正常但实际有问题"的数据。

这是本项目和普通爬虫脚本最大的区别 —— 大多数人死在"数据悄悄坏了却不知道"。

检查项：
  A. 覆盖缺口   —— 某标的最后一个交易日远落后于全市场最新交易日（漏跑/断更）
  B. 空值       —— 关键列存在 NaN
  C. 价格异常   —— 单日涨跌幅超过阈值（未复权导致的跳变、数据源错位）
  D. 重复行     —— 同一日期出现多行
  E. 行数过少   —— 疑似只拉到部分历史
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pandas as pd

from .store import DataStore

__all__ = ["HealthReport", "check_category"]


class HealthReport:
    def __init__(self, category: str):
        self.category = category
        self.issues: List[dict] = []
        self.checked = 0
        self.latest_market_date: Optional[pd.Timestamp] = None

    def add(self, kind: str, code: str, detail: str, severity: str = "warn"):
        self.issues.append(
            {"kind": kind, "code": code, "detail": detail, "severity": severity}
        )

    @property
    def errors(self) -> List[dict]:
        return [i for i in self.issues if i["severity"] == "error"]

    def summary(self) -> str:
        if self.checked == 0:
            return f"[{self.category}] 没有找到任何数据文件，请先执行 fetch"
        lines = [
            f"[{self.category}] 检查 {self.checked} 个标的",
            f"  全市场最新交易日: "
            f"{self.latest_market_date.strftime('%Y-%m-%d') if self.latest_market_date is not None else 'N/A'}",
        ]
        if not self.issues:
            lines.append("  ✅ 未发现问题")
        else:
            by_kind: dict[str, int] = {}
            for i in self.issues:
                by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
            for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
                lines.append(f"  ⚠️  {k}: {v} 个")
            lines.append("  （详见 --verbose）")
        return "\n".join(lines)


def check_category(
    store: DataStore,
    category: str,
    max_pct_move: float = 0.30,
    stale_days: int = 10,
    min_rows: int = 20,
) -> HealthReport:
    """对某类别做全面体检。

    Parameters
    ----------
    max_pct_move : float
        单日涨跌幅绝对值的告警阈值。可转债/个股正常不会超过 30%
        （新股首日除外，会以 info 级别记录而非 error）。
    stale_days : int
        某标的最新数据落后全市场最新交易日超过该天数则告警。
    """
    rep = HealthReport(category)
    files = store.list_existing(category)
    if not files:
        return rep

    frames: dict[str, pd.DataFrame] = {}
    for f in files:
        code = f.stem.split("_")[0]
        df = store.load(category, code, f.stem.split("_", 1)[1] if "_" in f.stem else "")
        if df is None:
            # 尝试直接用文件名读
            try:
                df = pd.read_csv(f)
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"])
            except Exception:
                rep.add("无法读取", code, f"{f.name} 解析失败", "error")
                continue
        frames[code] = df

    if not frames:
        return rep

    rep.checked = len(frames)
    rep.latest_market_date = max(df["date"].max() for df in frames.values())

    for code, df in frames.items():
        # D 重复行
        dup = df["date"].duplicated().sum()
        if dup > 0:
            rep.add("重复日期", code, f"{dup} 行重复", "error")

        # E 行数过少
        if len(df) < min_rows:
            rep.add("行数过少", code, f"仅 {len(df)} 行，疑似未拉全历史", "warn")

        # B 空值
        for col in ("open", "high", "low", "close"):
            if col in df.columns:
                n_null = int(df[col].isna().sum())
                if n_null > 0:
                    rep.add("关键列空值", code, f"{col} 有 {n_null} 个 NaN", "error")

        # C 价格异常
        if "close" in df.columns and len(df) > 1:
            safe = df.dropna(subset=["close"]).sort_values("date")
            pct = safe["close"].pct_change().abs()
            big = pct[pct > max_pct_move]
            if len(big) > 0:
                first = big.index[0]
                rep.add(
                    "价格跳变",
                    code,
                    f"{len(big)} 处单日涨跌超 {max_pct_move:.0%}，"
                    f"首次 {safe.loc[first, 'date']:%Y-%m-%d} "
                    f"{pct.loc[first]:.1%}（疑似未复权）",
                    "warn",
                )

        # A 覆盖缺口
        gap = (rep.latest_market_date - df["date"].max()).days
        if gap > stale_days:
            rep.add(
                "数据滞后",
                code,
                f"最新数据 {df['date'].max():%Y-%m-%d}，落后 {gap} 天",
                "warn",
            )

    return rep
