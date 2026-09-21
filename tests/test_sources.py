# -*- coding: utf-8 -*-
"""数据源适配层测试：多源降级、列名标准化、空结果处理、错误归因。

全部用真实 DataFrame fixture（还原各源真实返回的列名），不 mock 网络。
"""
import pandas as pd
import pytest

from qdk.sources import (
    EmptyResult, SourceError, fetch_with_fallback, standardize_columns,
)


class TestStandardizeColumns:
    def test_chinese_columns(self):
        """东财风格：中文列名应被映射为标准名。"""
        raw = pd.DataFrame({
            "日期": ["2024-01-02"], "开盘": [10.0], "最高": [10.5],
            "最低": [9.9], "收盘": [10.3], "成交量": [1000],
        })
        out = standardize_columns(raw, keep=["date", "open", "high", "low", "close", "volume"])
        assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-02")

    def test_english_columns(self):
        raw = pd.DataFrame({"date": ["2024-01-02"], "open": [1], "close": [2]})
        out = standardize_columns(raw)
        assert "date" in out.columns

    def test_missing_date_raises(self):
        """无日期列必须明确报错——静默丢列会导致下游拿到错位数据。"""
        raw = pd.DataFrame({"day": ["2024-01-02"], "close": [1]})
        with pytest.raises(SourceError, match="未返回日期列"):
            standardize_columns(raw)

    def test_drops_invalid_dates(self):
        raw = pd.DataFrame({"date": ["2024-01-02", "坏日期"], "close": [1, 2]})
        out = standardize_columns(raw)
        assert len(out) == 1

    def test_keep_filters_columns(self):
        raw = pd.DataFrame({"date": ["2024-01-02"], "open": [1], "无关列": [9]})
        out = standardize_columns(raw, keep=["date", "open"])
        assert list(out.columns) == ["date", "open"]


class TestFetchWithFallback:
    @staticmethod
    def _ok(payload):
        return lambda code: payload

    def test_first_source_wins(self):
        good = pd.DataFrame({"date": ["2024-01-02"], "close": [1.0]})
        calls = []

        def second(code):
            calls.append(code)
            return pd.DataFrame({"date": ["2024-01-03"], "close": [2.0]})

        res = fetch_with_fallback({"primary": self._ok(good), "backup": second}, "113050")
        assert res.source == "primary"
        assert calls == [], "首选成功时不应调用备用源"

    def test_fallback_on_exception(self):
        """首选抛异常 → 自动降级到备用源。"""
        def broken(code):
            raise ConnectionError("proxy error")

        good = pd.DataFrame({"date": ["2024-01-02"], "close": [1.0]})
        res = fetch_with_fallback({"primary": broken, "backup": self._ok(good)},
                                  "113050", sleep=0)
        assert res.source == "backup"
        assert res.rows == 1

    def test_fallback_on_empty(self):
        """首选返回空 DataFrame → 也要降级（节假日/停牌是常见情况）。"""
        good = pd.DataFrame({"date": ["2024-01-02"], "close": [1.0]})
        res = fetch_with_fallback(
            {"primary": self._ok(pd.DataFrame()), "backup": self._ok(good)},
            "113050", sleep=0)
        assert res.source == "backup"

    def test_all_empty_raises_empty_result(self):
        """全部为空 → EmptyResult，调用方须显式处理，绝不能当成成功。"""
        with pytest.raises(EmptyResult, match="所有数据源均未取到数据"):
            fetch_with_fallback(
                {"a": self._ok(pd.DataFrame()), "b": self._ok(pd.DataFrame())},
                "113050", sleep=0)

    def test_all_failed_reports_causes(self):
        """全部失败 → 错误信息里要能看到各源的具体原因，便于定位。"""
        def boom(name):
            def f(code):
                raise ValueError(f"{name} 崩了")
            return f

        with pytest.raises(EmptyResult) as ei:
            fetch_with_fallback({"s1": boom("源1"), "s2": boom("源2")}, "113050", sleep=0)
        msg = str(ei.value)
        assert "源1" in msg and "源2" in msg
        assert "ValueError" in msg

    def test_min_rows_filters_stub_data(self):
        """只返回表头/单行脏数据的源应被视为无效。"""
        stub = pd.DataFrame({"date": ["2024-01-02"]})
        good = pd.DataFrame({"date": [f"2024-01-{d:02d}" for d in range(1, 6)],
                             "close": [1.0] * 5})
        res = fetch_with_fallback({"stub": self._ok(stub), "good": self._ok(good)},
                                  "113050", sleep=0, min_rows=3)
        assert res.source == "good"

    def test_mixed_empty_and_error_message(self):
        """空与报错并存时，信息里两类都要出现（处理方式不同）。"""
        def broken(code):
            raise TimeoutError("超时")

        with pytest.raises(EmptyResult) as ei:
            fetch_with_fallback({"broken": broken, "empty": self._ok(pd.DataFrame())},
                                "113050", sleep=0)
        msg = str(ei.value)
        assert "调用失败" in msg and "返回空" in msg
