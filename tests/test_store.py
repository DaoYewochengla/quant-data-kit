# -*- coding: utf-8 -*-
"""存储层单元测试：原子写入、增量合并去重、拒绝空数据。

这些是不需要网络的纯逻辑测试，用真实 fixture 数据（非 mock），
确保行为与真实运行一致。
"""
import pandas as pd
import pytest

from qdk.store import DataStore


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path)


@pytest.fixture
def df_a():
    return pd.DataFrame({
        "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "open": [10.0, 10.1, 10.2],
        "close": [10.1, 10.2, 10.3],
    })


class TestSaveBasics:
    def test_writes_file(self, store, df_a):
        r = store.save("cb", "113050", "测试转债", df_a)
        assert r["written"] is True
        assert r["total_rows"] == 3
        assert r["added_rows"] == 3
        assert store.path_for("cb", "113050", "测试转债").exists()

    def test_rejects_empty(self, store):
        """最重要的一条：空数据必须被拒绝，否则会伪装成正常数据。"""
        with pytest.raises(ValueError, match="拒绝写入空数据"):
            store.save("cb", "113050", "x", pd.DataFrame())
        with pytest.raises(ValueError, match="拒绝写入空数据"):
            store.save("cb", "113050", "x", None)

    def test_rejects_all_nan_dates(self, store):
        """日期列全为 NaN 也应被拒绝（等价于无有效数据）。"""
        df = pd.DataFrame({"date": [None, None], "close": [1.0, 2.0]})
        with pytest.raises(ValueError):
            store.save("cb", "113050", "x", df)

    def test_no_tmp_file_left(self, store, df_a):
        """原子写入不应残留 .tmp 文件。"""
        store.save("cb", "113050", "x", df_a)
        leftovers = list(store.root.rglob("*.tmp"))
        assert leftovers == []


class TestIncremental:
    def test_added_rows_counts_only_new(self, store, df_a):
        store.save("cb", "113050", "x", df_a)

        # 第二批：1 条重复 + 1 条新
        df_b = pd.DataFrame({
            "date": ["2024-01-04", "2024-01-05"],
            "open": [10.2, 10.3], "close": [10.3, 10.4],
        })
        r = store.save("cb", "113050", "x", df_b, incremental=True)

        assert r["added_rows"] == 1, "只有 01-05 是新增"
        assert r["total_rows"] == 4, "总行数应为 3+1 去重后"

    def test_dedup_keeps_last(self, store, df_a):
        """同一日期重复时保留后写入的（用于修正错误数据）。"""
        store.save("cb", "113050", "x", df_a)
        fix = pd.DataFrame({"date": ["2024-01-03"], "open": [99.0], "close": [99.9]})
        store.save("cb", "113050", "x", fix, incremental=True)

        out = store.load("cb", "113050", "x")
        row = out[out["date"] == pd.Timestamp("2024-01-03")].iloc[0]
        assert row["close"] == 99.9, "应保留新写入的值"

    def test_idempotent(self, store, df_a):
        """重复写入同一份数据，总行数不变、新增为 0（幂等的核心保证）。"""
        store.save("cb", "113050", "x", df_a)
        r = store.save("cb", "113050", "x", df_a, incremental=True)
        assert r["added_rows"] == 0
        assert r["total_rows"] == 3

    def test_sorted_by_date(self, store):
        """乱序输入必须落盘为按日期升序。"""
        df = pd.DataFrame({
            "date": ["2024-03-01", "2024-01-01", "2024-02-01"],
            "close": [3.0, 1.0, 2.0],
        })
        store.save("cb", "113050", "x", df)
        out = store.load("cb", "113050", "x")
        assert list(out["close"]) == [1.0, 2.0, 3.0]


class TestLastDate:
    def test_none_when_missing(self, store):
        assert store.last_date("cb", "999999", "不存在") is None

    def test_returns_max(self, store, df_a):
        store.save("cb", "113050", "x", df_a)
        assert store.last_date("cb", "113050", "x") == pd.Timestamp("2024-01-04")

    def test_none_on_corrupt_file(self, store):
        """损坏文件应返回 None 而非抛异常（否则一条脏文件会让整条管道崩）。"""
        p = store.path_for("cb", "113050", "坏文件")
        p.write_text("这不是CSV\x00\x01乱码", encoding="utf-8")
        assert store.load("cb", "113050", "坏文件") is None


class TestStats:
    def test_stats_counts(self, store, df_a):
        store.save("cb", "113050", "a", df_a)
        store.save("cb", "128145", "b", df_a)
        s = store.stats("cb")
        assert s["files"] == 2
        assert s["total_rows"] == 6
        assert s["latest_date"] == "2024-01-04"

    def test_stats_empty_dir(self, store):
        s = store.stats("不存在的类别")
        assert s["files"] == 0
        assert s["latest_date"] is None
