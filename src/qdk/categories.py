# -*- coding: utf-8 -*-
"""三类资产（可转债 / 个股 / ETF）的数据抓取定义。

每个类别声明：
  - universe(): 怎么拿到标的名单
  - sources():   单个标的的多数据源，按优先级排列（前列失败自动降级）

所有接口均为 AKShare 封装的公开数据（东方财富 / 新浪 / 腾讯 / 同花顺），
无需 token、无需付费账号。
"""
from __future__ import annotations

from typing import Callable, Dict, List

import akshare as ak
import pandas as pd

from .symbols import normalize, with_exchange_prefix, exchange_of
from .sources import STANDARD_COLUMNS

__all__ = ["CATEGORIES", "get_category_sources", "get_universe"]

# ---------------------------------------------------------------------------
# 统一 HTTP 会话
#
# 必须显式 trust_env=False：本机 Windows 系统代理（127.0.0.1:7897）会被
# requests 自动采用，导致境内接口被送到代理，表现为间歇性 ProxyError。
# 更麻烦的是同一接口"有时通有时不通"，极易误判为数据源不稳定。
# 统一会话 + 重试，把这层不确定性挡在数据源函数之外。
# ---------------------------------------------------------------------------
import requests as _requests
from requests.adapters import HTTPAdapter

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_HTTP = _requests.Session()
_HTTP.trust_env = False          # 不读系统代理 / 环境变量代理
_HTTP.headers.update({"User-Agent": _UA})
_adapter = HTTPAdapter(max_retries=2, pool_connections=8, pool_maxsize=8)
_HTTP.mount("https://", _adapter)
_HTTP.mount("http://", _adapter)


def _http_get(url: str, params: dict | None = None,
              headers: dict | None = None, timeout: int = 20):
    """统一的 HTTP GET：禁代理 + 自动重试 + 明确抛错。"""
    return _HTTP.get(url, params=params, headers=headers or {}, timeout=timeout)



# --------------------------------------------------------------------- 可转债

def _cb_prices_tencent(code: str) -> pd.DataFrame:
    """可转债日线（腾讯源，首选）。

    实测（2026-09）：
      - 新浪 bond_zh_hs_cov_daily 依赖的 klc_kl.js 已 404 下线，akshare 内部抛 KeyError
      - 东财 push2his 在部分网络环境（系统代理规则）完全不可达
      - 腾讯 web.ifzq.gtimg.cn 稳定可用，但单次 datalen 不能给太大（见 _parse_tencent_klines）
    """
    return _parse_tencent_klines(code)


def _cb_prices_sina_hq(code: str) -> pd.DataFrame:
    """可转债日线（新浪源，备用）——直接调用公开行情接口，不经过 akshare。"""
    import json
    sym = with_exchange_prefix(code)
    # 新浪 K 线公开接口
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/"
        "CN_MarketDataService.getKLineData"
    )
    params = {"symbol": sym, "scale": "240", "ma": "no", "datalen": "10000"}
    r = _http_get(url, params=params, headers={"Referer": "https://finance.sina.com.cn"})
    text = r.text.strip()
    if not text or text == "null":
        return pd.DataFrame()
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    return df.rename(columns={"day": "date"})


def _source_order_default():
    """按"实测可用性"排序的默认优先级，用户可在 config 中覆盖。"""
    return ("tencent", "sina")


def _cb_universe() -> pd.DataFrame:
    """可转债全名单（含已退市/到期的转债，用于规避幸存者偏差）。

    实测坑：名单接口返回的转债包含"已核准但尚未上市"的（如 123286 国科转债，
    到期日 2032 却在行情接口查不到任何日线，实时价为 100.0 且成交量为 0）。
    这类标的必须过滤，否则每次全量拉取都会产生一批"永远失败"的噪音失败。
    """
    df = None
    # 优选同花顺源（含退市/到期转债）
    try:
        ths = ak.bond_zh_cov_info_ths()
        code_col = next((c for c in ths.columns if "代码" in str(c)), None)
        name_col = next((c for c in ths.columns if "简称" in str(c)), None)
        if code_col and name_col:
            df = pd.DataFrame({
                "code": ths[code_col].astype(str).str.strip().str.zfill(6),
                "name": ths[name_col].astype(str).str.strip(),
                "maturity": pd.to_datetime(ths.get("到期时间"), errors="coerce"),
                "list_date": pd.to_datetime(ths.get("上市日期"), errors="coerce"),
                "delisted_flag": False,
            })
    except Exception:
        df = None

    if df is None or len(df) == 0:
        # 降级到东财源（仅现存转债，无上市日期）
        em = ak.bond_zh_cov()
        code_col = next((c for c in em.columns if "代码" in str(c)), None)
        name_col = next((c for c in em.columns if "简称" in str(c)), None)
        df = pd.DataFrame({
            "code": em[code_col].astype(str).str.strip().str.zfill(6),
            "name": em[name_col].astype(str).str.strip(),
            "maturity": pd.to_datetime(em.get("到期时间"), errors="coerce"),
            "list_date": pd.to_datetime(em.get("上市日期"), errors="coerce"),
            "delisted_flag": False,
        })

    # 过滤：未上市（有上市日期字段时）
    if "list_date" in df.columns and df["list_date"].notna().any():
        today = pd.Timestamp.today().normalize()
        unlisted = df["list_date"].isna() | (df["list_date"] > today)
        # 仅在"上市日期字段基本可用"时才过滤，避免字段整体缺失导致误杀
        if unlisted.sum() < len(df) * 0.5:
            df = df[~unlisted].copy()

    # 标记已到期/退市
    if "maturity" in df.columns:
        today = pd.Timestamp.today().normalize()
        df["delisted_flag"] = df["maturity"].notna() & (df["maturity"] < today)

    return df.reset_index(drop=True)


# --------------------------------------------------------------------- 个股

def _parse_tencent_klines(code: str, max_bars: int = 800, page: int = 640) -> pd.DataFrame:
    """腾讯 K 线通用解析（个股/ETF/可转债共用）。

    实测坑（2026-09）：
      - param 里的 datalen 给 10000 会被腾讯拒绝，返回空 data（code=0 但无数据），
        不报错、不提示，表现为"该标的有数据但拉不到"。
      - 单次可用上限约 320~800 根。因此用 page 大小重复请求，
        end 参数向前回溯，直到取满或返回空。
    """
    import json

    sym = with_exchange_prefix(code)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    rows: list = []
    seen: set = set()
    end = ""  # 空表示"截止到最新"

    for _ in range(60):  # 安全上限：60 页 * 640 ≈ 38400 根，覆盖任何标的的历史
        param = f"{sym},day,,{end},{page},qfq"
        r = _http_get(url, params={"param": param})
        payload = json.loads(r.text)
        node = (payload.get("data") or {}).get(sym) or {}
        klines = node.get("qfqday") or node.get("day") or []
        if not klines:
            break

        new_in_batch = 0
        for k in klines:
            if len(k) < 6:
                continue
            d = k[0]
            if d in seen:
                continue
            seen.add(d)
            new_in_batch += 1
            rows.append({
                "date": d, "open": k[1], "close": k[2],
                "high": k[3], "low": k[4], "volume": k[5],
            })

        if new_in_batch == 0 or len(klines) < page or len(rows) >= max_bars:
            break
        # 继续向前拉：以本批最早日期为新的 end
        earliest = min(k[0] for k in klines if len(k) >= 6)
        end = earliest

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _stock_prices_tencent(code: str) -> pd.DataFrame:
    """个股日线（腾讯源，首选）。"""
    return _parse_tencent_klines(code)


def _etf_prices_tencent(code: str) -> pd.DataFrame:
    """ETF 日线（腾讯源，首选）。"""
    return _parse_tencent_klines(code)


def _stock_prices_em(code: str) -> pd.DataFrame:
    """个股日线（东方财富源，末位兜底）。"""
    return ak.stock_zh_a_hist(symbol=normalize(code), period="daily", adjust="qfq")


def _stock_prices_sina_direct(code: str) -> pd.DataFrame:
    """个股日线（新浪源，直接调用公开接口，不经过 akshare）。"""
    import json
    sym = with_exchange_prefix(code)
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/"
           "CN_MarketDataService.getKLineData")
    params = {"symbol": sym, "scale": "240", "ma": "no", "datalen": "10000"}
    r = _http_get(url, params=params, headers={"Referer": "https://finance.sina.com.cn"})
    text = r.text.strip()
    if not text or text == "null":
        return pd.DataFrame()
    data = json.loads(text)
    if not isinstance(data, list) or not data:
        return pd.DataFrame()
    return pd.DataFrame(data).rename(columns={"day": "date"})


def _stock_universe() -> pd.DataFrame:
    """全市场个股名单（现存 + 已退市，用于规避幸存者偏差）。

    重要：现存名单与退市名单来源不同，任一失败都会导致结果严重失真
    （只有退市股 = 幸存者偏差反着犯）。因此这里不允许静默吞异常，
    如果现存名单拿不到，必须显式报错，而不是返回一份只有退市股的名单。
    """
    frames = []
    alive_ok = False

    # --- 现存个股（必须成功）
    # 注意：新浪源返回的代码带交易所前缀（如 'bj920000'），需去掉；
    # 且新浪源需分页抓取全市场，耗时约 10~15 秒，属正常。
    for label, fn in (
        ("eastmoney", lambda: ak.stock_zh_a_spot_em()),
        ("sina", lambda: ak.stock_zh_a_spot()),
    ):
        try:
            a = fn()
            code_col = next((c for c in a.columns if c in ("代码", "symbol", "code")), None)
            name_col = next((c for c in a.columns if c in ("名称", "name")), None)
            if code_col is None:
                continue
            a = a.rename(columns={code_col: "code", name_col: "name"} if name_col
                         else {code_col: "code"})
            if "name" not in a.columns:
                a["name"] = ""
            # 统一成 6 位纯数字：去除 sh/sz/bj 前缀与 .SH 后缀
            a["code"] = (a["code"].astype(str).str.strip().str.upper()
                         .str.replace(r"^(SH|SZ|BJ)", "", regex=True)
                         .str.replace(r"\.(SH|SZ|BJ)$", "", regex=True)
                         .str.strip())
            a = a[a["code"].str.fullmatch(r"\d{6}", na=False)]
            if len(a) == 0:
                continue
            a["delisted_flag"] = False
            frames.append(a[["code", "name", "delisted_flag"]])
            alive_ok = True
            break
        except Exception:
            continue

    if not alive_ok:
        raise RuntimeError(
            "无法获取现存个股名单（东财与新浪源均失败）。"
            "已中止以免生成一份只含退市股的名单——那会造成严重的样本偏差。"
        )

    # --- 已退市（可选，失败只降级不中止）
    for fn in (ak.stock_info_sh_delist, ak.stock_info_sz_delist):
        try:
            d = fn()
            code_col = next((c for c in d.columns if "代码" in str(c)), None)
            name_col = next((c for c in d.columns if "简称" in str(c) or "名称" in str(c)), None)
            if code_col is None:
                continue
            d = d.rename(columns={code_col: "code", name_col: "name"}) if name_col \
                else d.rename(columns={code_col: "code"})
            if "name" not in d.columns:
                d["name"] = ""
            d["code"] = d["code"].astype(str).str.strip().str.zfill(6)
            d["delisted_flag"] = True
            frames.append(d[["code", "name", "delisted_flag"]])
        except Exception:
            continue

    out = pd.concat(frames, ignore_index=True)
    # 现存优先（keep="first"），退市补充
    return out.drop_duplicates(subset=["code"], keep="first")


# --------------------------------------------------------------------- ETF

def _etf_prices_sina(code: str) -> pd.DataFrame:
    """ETF 日线（新浪源）。"""
    return ak.fund_etf_hist_sina(symbol=with_exchange_prefix(code))


def _etf_prices_em(code: str) -> pd.DataFrame:
    """ETF 日线（东方财富源，备用）。"""
    return ak.fund_etf_hist_em(symbol=normalize(code), period="daily", adjust="")


def _etf_universe() -> pd.DataFrame:
    """场内 ETF 名单（多源，东财不可达时降级到新浪）。"""
    errors = []
    for label, fn in (("eastmoney", ak.fund_etf_spot_em), ("sina", ak.fund_etf_category_sina)):
        try:
            df = fn()
            code_col = next((c for c in df.columns if c in ("代码", "symbol", "code")), None)
            name_col = next((c for c in df.columns if c in ("名称", "name")), None)
            if code_col is None:
                errors.append(f"{label}: 无代码列 {list(df.columns)[:6]}")
                continue
            df = df.rename(columns={code_col: "code", name_col: "name"} if name_col
                           else {code_col: "code"})
            if "name" not in df.columns:
                df["name"] = ""
            df["delisted_flag"] = False
            return df[["code", "name", "delisted_flag"]]
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {str(e)[:60]}")
    raise RuntimeError("无法获取 ETF 名单: " + "; ".join(errors))


# --------------------------------------------------------------------- 注册表

CATEGORIES: Dict[str, dict] = {
    "cb": {
        "label": "可转债",
        "universe": _cb_universe,
        "sources": {
            "tencent": _cb_prices_tencent,
            "sina": _cb_prices_sina_hq,
        },
        "keep": ["date", "open", "high", "low", "close", "volume", "amount"],
    },
    "stock": {
        "label": "个股",
        "universe": _stock_universe,
        "sources": {
            "tencent": _stock_prices_tencent,
            "sina": _stock_prices_sina_direct,
            "eastmoney": _stock_prices_em,
        },
        "keep": ["date", "open", "high", "low", "close", "volume", "amount"],
    },
    "etf": {
        "label": "ETF",
        "universe": _etf_universe,
        "sources": {
            "tencent": _etf_prices_tencent,
            "sina": _etf_prices_sina,
        },
        "keep": ["date", "open", "high", "low", "close", "volume", "amount"],
    },
}


def get_category_sources(category: str) -> Dict[str, Callable[[str], pd.DataFrame]]:
    if category not in CATEGORIES:
        raise KeyError(f"未知类别 {category!r}，可选: {list(CATEGORIES)}")
    return CATEGORIES[category]["sources"]


def get_universe(category: str) -> pd.DataFrame:
    if category not in CATEGORIES:
        raise KeyError(f"未知类别 {category!r}，可选: {list(CATEGORIES)}")
    return CATEGORIES[category]["universe"]()
