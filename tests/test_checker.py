# -*- coding: utf-8 -*-
"""体检层测试：用真实 fixture 还原各类"看起来正常但实际有问题"的数据。

每个测试用一个已知有缺陷的数据集，断言体检能准确识别出它，
而不是笼统报"有问题"。分档断言：验证问题的类型与归属标的。
"""
import pandas as pd
import pytest

from qdk.checker import check_category
from qdk.store import DataStore


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path)


def _frame(dates, close=10.0):
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1000,
    })


class TestCleanData:
    def test_no_issues(self, store):
        """一批健康数据不应产生任何告警。"""
        dates = pd.bdate_range("2024-01-01", periods=30)
        for c in ("113050", "128145", "123286"):
            store.save("cb", c, "x", _frame(dates))
        rep = check_category(store, "cb")
        assert rep.checked == 3
        assert rep.issues == [], f"健康数据不应有告警，实际: {rep.issues}"


class TestIssueDetection:
    def test_detects_duplicate_dates(self, store):
        df = pd.concat([_frame(pd.bdate_range("2024-01-01", periods=25)),
                        _frame(["2024-01-10"])], ignore_index=True)
        store.save("cb", "113050", "x", df, incremental=False)
        rep = check_category(store, "cb")
        kinds = [i["kind"] for i in rep.issues]
        assert "重复日期" in kinds
        # 分档断言：问题必须归到正确标的
        assert all(i["code"] == "113050" for i in rep.issues)

    def test_detects_too_few_rows(self, store):
        store.save("cb", "113050", "x", _frame(["2024-01-02"]))
        rep = check_category(store, "cb", min_rows=20)
        assert any(i["kind"] == "行数过少" for i in rep.issues)

    def test_detects_null_in_key_column(self, store):
        df = _frame(pd.bdate_range("2024-01-01", periods=25))
        df.loc[5, "close"] = None
        store.save("cb", "113050", "x", df)
        rep = check_category(store, "cb")
        issues = [i for i in rep.issues if i["kind"] == "关键列空值"]
        assert len(issues) == 1
        assert "close" in issues[0]["detail"]
        assert issues[0]["code"] == "113050"

    def test_detects_price_jump(self, store):
        """未复权导致的跳变：10 → 40 应被标记。"""
        df = _frame(pd.bdate_range("2024-01-01", periods=30))
        df.loc[20, "close"] = 40.0
        store.save("cb", "113050", "x", df)
        rep = check_category(store, "cb", max_pct_move=0.30)
        assert any(i["kind"] == "价格跳变" for i in rep.issues)

    def test_no_false_positive_on_normal_move(self, store):
        """正常 5% 波动不应触发价格跳变告警（控制误报）。"""
        df = _frame(pd.bdate_range("2024-01-01", periods=30))
        for i in range(1, 30):
            df.loc[i, "close"] = 10.0 * (1.05 ** i)
        store.save("cb", "113050", "x", df)
        rep = check_category(store, "cb", max_pct_move=0.30)
        assert not any(i["kind"] == "价格跳变" for i in rep.issues)

    def test_detects_stale_data(self, store):
        """一个标的停在很久以前，其余是最新 → 只有它该被标记滞后。"""
        fresh = pd.bdate_range(end="2026-09-21", periods=30)
        old = pd.bdate_range(end="2024-01-01", periods=30)
        store.save("cb", "113050", "fresh", _frame(fresh), incremental=False)
        store.save("cb", "128145", "old", _frame(old), incremental=False)

        rep = check_category(store, "cb", stale_days=10)
        stale = [i for i in rep.issues if i["kind"] == "数据滞后"]
        assert len(stale) == 1, "只应有 1 个标的滞后"
        assert stale[0]["code"] == "128145", "滞后的应是 old 那个"

    def test_market_latest_date(self, store):
        """全市场最新交易日应取所有标的的最大值。"""
        store.save("cb", "113050", "a",
                   _frame(pd.bdate_range(end="2026-09-21", periods=25)), incremental=False)
        store.save("cb", "128145", "b",
                   _frame(pd.bdate_range(end="2026-09-18", periods=25)), incremental=False)
        rep = check_category(store, "cb")
        assert rep.latest_market_date == pd.Timestamp("2026-09-21")


class TestEmptyAndSummary:
    def test_empty_category(self, store):
        rep = check_category(store, "cb")
        assert rep.checked == 0
        assert "没有找到任何数据文件" in rep.summary()

    def test_summary_reports_counts(self, store):
        store.save("cb", "113050", "x", _frame(["2024-01-02"]))
        rep = check_category(store, "cb", min_rows=20)
        s = rep.summary()
        assert "检查 1 个标的" in s
        assert "行数过少" in s
