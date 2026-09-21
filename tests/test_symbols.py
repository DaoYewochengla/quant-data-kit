# -*- coding: utf-8 -*-
"""符号工具单元测试（纯函数，无需网络）。"""
import pytest

from qdk.symbols import normalize, exchange_of, with_exchange_prefix, sanitize_filename


class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("113050", "113050"),
        (113050, "113050"),
        ("sh113050", "113050"),
        ("SH113050", "113050"),
        ("113050.SH", "113050"),
        ("sz128145", "128145"),
        (" 600000 ", "600000"),
        # 不足 6 位要左补零
        ("1234", "001234"),
        (12345, "012345"),
    ])
    def test_accepted_forms(self, raw, expected):
        assert normalize(raw) == expected

    @pytest.mark.parametrize("bad", [None, "", "abcdef", "12a456"])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            normalize(bad)


class TestExchange:
    @pytest.mark.parametrize("code,ex", [
        # 沪市
        ("600000", "sh"), ("601398", "sh"), ("688981", "sh"),
        ("113050", "sh"), ("110075", "sh"), ("118045", "sh"),
        ("510300", "sh"), ("511010", "sh"),
        # 深市
        ("000001", "sz"), ("002594", "sz"), ("300750", "sz"),
        ("128145", "sz"), ("123286", "sz"), ("127102", "sz"),
        ("159915", "sz"), ("161607", "sz"),
        # 北交所
        ("920000", "bj"), ("830799", "bj"), ("430047", "bj"),
    ])
    def test_exchange_detection(self, code, ex):
        assert exchange_of(code) == ex

    def test_prefix_form(self):
        # 这是传给腾讯/新浪接口的形态
        assert with_exchange_prefix("128145") == "sz128145"
        assert with_exchange_prefix("113050") == "sh113050"
        # 传入已带前缀的也不应重复叠加
        assert with_exchange_prefix("sh113050") == "sh113050"


class TestSanitizeFilename:
    @pytest.mark.parametrize("raw,expected", [
        ("国科转债", "国科转债"),
        ("*ST东北", "ST东北"),          # 星号是真实场景（ST 股）
        ("A/B\\C", "ABC"),
        ("名字 带 空格", "名字带空格"),
        ("", "unnamed"),
        (None, "unnamed"),
        ('a*b?c"d<e>f|g', "abcdefg"),
    ])
    def test_cleanup(self, raw, expected):
        assert sanitize_filename(raw) == expected
