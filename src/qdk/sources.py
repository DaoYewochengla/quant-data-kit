# -*- coding: utf-8 -*-
"""数据源适配层：统一列名、多源互为备份、失败明确抛出。

设计原则（这是本项目的核心价值，也是最容易踩坑的地方）：

1. **绝不静默成功**。akshare 在节假日/停牌/接口变更时会返回空 DataFrame 而不报错。
   本模块对空结果一律抛 EmptyResult，由调用方决定是"跳过"还是"报错"。
   一个写进磁盘的空文件，比一个报错危险得多。

2. **多源自动切换**。同一份数据配置多个 source，按顺序尝试，前一个失败自动降级。
   单一数据源抽风不该导致整条管道断更。

3. **列名统一**。各接口返回中文列名且时常变动，统一映射成
   date/open/high/low/close/volume/amount，异常列名要明确报错而非默默丢弃。

4. **限流保护**。公开接口有频率限制，连续请求之间强制 sleep，避免被临时封禁。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

__all__ = ["EmptyResult", "SourceError", "FetchResult", "fetch_with_fallback", "STANDARD_COLUMNS"]

# ---------------------------------------------------------------------------
# 代理处理：境内数据源（东财/新浪/腾讯/同花顺）绝不应该走代理。
#
# 实测坑：Windows 系统代理（HKCU\...\Internet Settings\ProxyEnable=1,
# ProxyServer=127.0.0.1:7897）会被 requests 的 trust_env=True 自动采用，
# 导致境内接口被送到代理，表现为间歇性 ProxyError —— 同一接口一会儿通一会儿不通，
# 极难排查。开源包必须对这种环境健壮，因此这里在导入时即清空代理相关环境变量。
# 需要走代理的用户可在调用前自行设置 REQUESTS_CA_BUNDLE / 显式 proxies=。
# ---------------------------------------------------------------------------
import os as _os

for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
           "http_proxy", "https_proxy", "all_proxy"):
    _os.environ.pop(_v, None)
_os.environ["NO_PROXY"] = "*"
_os.environ["no_proxy"] = "*"

STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

# 各数据源可能出现的列名 -> 标准列名
_COLUMN_ALIASES: Dict[str, str] = {
    # 中文
    "日期": "date", "时间": "date",
    "开盘": "open", "开盘价": "open",
    "最高": "high", "最高价": "high",
    "最低": "low", "最低价": "low",
    "收盘": "close", "收盘价": "close",
    "成交量": "volume",
    "成交额": "amount",
    # 英文变体
    "vol": "volume", "turnover": "amount", "trade_date": "date",
}


class EmptyResult(Exception):
    """接口返回成功但数据为空。

    常见原因：非交易日、标的停牌、接口静默变更。调用方必须显式处理。
    """


class SourceError(Exception):
    """数据源调用失败（网络、限流、接口签名变更等）。"""


class FetchResult:
    """一次成功抓取的结果，附带来源与行数信息，便于日志与体检。"""

    def __init__(self, df: pd.DataFrame, source: str, code: str):
        self.df = df
        self.source = source
        self.code = code

    @property
    def rows(self) -> int:
        return len(self.df)

    def __repr__(self) -> str:
        return f"<FetchResult {self.code} via {self.source} rows={self.rows}>"


def standardize_columns(df: pd.DataFrame, keep: Optional[List[str]] = None) -> pd.DataFrame:
    """把原始 DataFrame 的列名标准化。

    未识别的列保留原名（不丢弃，交给上层决定），但 date 列必须存在。
    """
    renamed = {c: _COLUMN_ALIASES.get(str(c).strip(), str(c).strip()) for c in df.columns}
    out = df.rename(columns=renamed)

    if "date" not in out.columns:
        raise SourceError(
            f"数据源未返回日期列，实际列名={list(df.columns)}。"
            "接口可能已变更，请检查 akshare 是否升级。"
        )
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"])

    if keep:
        cols = [c for c in keep if c in out.columns]
        out = out[cols]
    return out.reset_index(drop=True)


def fetch_with_fallback(
    sources: Dict[str, Callable[[str], pd.DataFrame]],
    code: str,
    sleep: float = 0.5,
    keep: Optional[List[str]] = None,
    min_rows: int = 1,
) -> FetchResult:
    """按顺序尝试多个数据源，返回第一个成功且非空的结果。

    Parameters
    ----------
    sources : dict
        {来源名: 调用函数}，函数签名 (code) -> DataFrame。
        顺序即优先级，前一个失败自动降级到下一个。
    code : str
        已规范化的标的代码。
    min_rows : int
        少于此行数视为无效（防止接口返回仅表头或单行脏数据）。

    Raises
    ------
    EmptyResult : 所有源都返回空
    SourceError : 所有源都调用失败
    """
    errors: List[str] = []
    empty_sources: List[str] = []

    for source_name, fn in sources.items():
        try:
            raw = fn(code)
        except Exception as e:  # 网络/限流/接口变更
            errors.append(f"{source_name}: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(sleep)
            continue

        if raw is None or len(raw) == 0:
            empty_sources.append(source_name)
            time.sleep(sleep)
            continue

        try:
            df = standardize_columns(raw, keep=keep)
        except SourceError as e:
            errors.append(f"{source_name}: {str(e)[:100]}")
            time.sleep(sleep)
            continue

        if len(df) < min_rows:
            empty_sources.append(f"{source_name}(仅{len(df)}行)")
            time.sleep(sleep)
            continue

        time.sleep(sleep)
        return FetchResult(df=df, source=source_name, code=code)

    # 全部失败：区分"都为空"和"都报错"，因为处理方式不同
    detail = f"调用失败[{'; '.join(errors)}]" if errors else ""
    if empty_sources:
        detail += f" 返回空[{', '.join(empty_sources)}]"
    raise EmptyResult(f"{code} 所有数据源均未取到数据: {detail}")
