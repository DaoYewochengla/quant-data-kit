# -*- coding: utf-8 -*-
"""标的基础工具：代码规范化、交易所前缀判断、文件名安全化。

设计原则：所有标的代码在进入任何接口前，统一走 normalize，
避免各接口对代码格式要求不一致导致的静默失败。
"""
from __future__ import annotations

import re

__all__ = ["normalize", "with_exchange_prefix", "exchange_of", "sanitize_filename"]


def normalize(code) -> str:
    """把各种形态的代码统一成 6 位纯数字字符串。

    支持输入：'113050' / 113050 / 'sh113050' / 'SH113050' / '113050.SH'
    """
    if code is None:
        raise ValueError("code 不能为 None")
    s = str(code).strip().upper()
    # 去掉交易所前后缀
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    s = re.sub(r"\.(SH|SZ|BJ)$", "", s)
    s = re.sub(r"\s+", "", s)
    if not s.isdigit():
        raise ValueError(f"无法识别的标的代码: {code!r}")
    return s.zfill(6)


def exchange_of(code) -> str:
    """判断标的所属交易所，返回 'sh' / 'sz' / 'bj'。

    规则（公开代码编制规则，非策略逻辑）：
      沪市 sh: 6xxxxx(个股/指数) 11xxxx(可转债) 5xxxxx(基金/ETF) 000xxx(指数)
      深市 sz: 0xxxxx 3xxxxx(个股) 12xxxx 127xxx 128xxx(可转债) 15/16xxxx(基金)
      北交所 bj: 4xxxxx 8xxxxx(个股) 其中 83/87/88 开头
    """
    s = normalize(code)
    if s.startswith(("6", "11", "5")):
        return "sh"
    if s.startswith(("0", "3", "12", "15", "16")):
        return "sz"
    if s.startswith(("4", "8", "9")):
        return "bj"
    # 000xxx 指数归沪市
    if s.startswith("000"):
        return "sh"
    return "sz"


def with_exchange_prefix(code) -> str:
    """返回带交易所小写前缀的代码，如 'sz128145'。多数 akshare 接口需要这个形态。"""
    return exchange_of(code) + normalize(code)


def sanitize_filename(name: str) -> str:
    """清理标的名中的文件系统非法字符（*ST 之类的星号是常见来源）。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "", str(name or "")).strip() or "unnamed"
