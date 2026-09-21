# -*- coding: utf-8 -*-
"""本地存储层：增量合并、断点续传、原子写入。

三个必须做对的点（做错任何一个都会静默污染数据）：

1. **原子写入**。先写 .tmp 再 rename。直接覆盖写时若进程被杀，
   会留下半截文件，而下次运行会把它当成"已有数据"跳过 —— 脏数据就此固化。

2. **增量合并去重**。读旧数据 + 新数据，按 date 去重（保留新的），排序后写回。
   不能简单 concat，否则重复运行会产生重复行，让统计口径全错。

3. **last_date 记录**。用于决定每个标的要拉哪一段。全量重拉在标的数量大时
   完全不可行，但只靠"文件存在就跳过"又会导致永久断更。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd

__all__ = ["DataStore"]


class DataStore:
    """按 <data_dir>/<category>/<code>_<name>.csv 组织 CSV 文件。

    为什么用 CSV 而不是数据库：用户可以直接用 Excel 打开看、被 git diff 到、
    出问题时肉眼可查。数据量在这个场景下 CSV 完全够用。
    """

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, category: str, code: str, name: str = "") -> Path:
        """返回某标的的落盘路径。"""
        d = self.root / category
        d.mkdir(parents=True, exist_ok=True)
        fname = f"{code}_{name}.csv" if name else f"{code}.csv"
        return d / fname

    # ---------------------------------------------------------------- 读

    def load(self, category: str, code: str, name: str = "") -> Optional[pd.DataFrame]:
        """读取已有数据，不存在返回 None。"""
        p = self.path_for(category, code, name)
        if not p.exists() or p.stat().st_size == 0:
            return None
        try:
            df = pd.read_csv(p)
        except Exception:
            # 损坏的文件不应该让整条管道崩掉，但也绝不静默当成功
            return None
        if "date" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        return df.dropna(subset=["date"])

    def last_date(self, category: str, code: str, name: str = "") -> Optional[pd.Timestamp]:
        """返回该标的已落盘的最后一个交易日，无数据返回 None。

        这是增量更新的依据：只拉 last_date 之后的部分。
        """
        df = self.load(category, code, name)
        if df is None or len(df) == 0:
            return None
        return df["date"].max()

    # ---------------------------------------------------------------- 写

    def save(
        self,
        category: str,
        code: str,
        name: str = "",
        df: Optional[pd.DataFrame] = None,
        incremental: bool = True,
    ) -> dict:
        """写入数据。incremental=True 时与已有数据按 date 合并去重。

        Returns
        -------
        dict: {written, total_rows, added_rows, path}
        """
        if df is None or len(df) == 0:
            # 明确拒绝写空数据：这是本项目最重要的防线
            raise ValueError(
                f"拒绝写入空数据 ({category}/{code})。"
                "空数据往往意味着接口异常或非交易日，写入后会伪装成正常数据。"
            )

        p = self.path_for(category, code, name)
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.sort_values("date")

        # 二次校验：日期列全为 NaN 时 dropna 后为空，此时同样必须拒绝。
        # 只检查 len(df)==0 是不够的 —— 长度非零但日期全无效的 DataFrame
        # 会绕过第一道防线，最终落盘成一个只有表头的空文件。
        if len(df) == 0:
            raise ValueError(
                f"拒绝写入空数据 ({category}/{code})："
                "所有日期均无效（解析后无有效行）。"
                "写入后会产生只有表头的空文件，伪装成正常数据。"
            )

        added = len(df)
        if incremental:
            old = self.load(category, code, name)
            if old is not None and len(old) > 0:
                before = old["date"].nunique()
                merged = pd.concat([old, df], ignore_index=True)
                # 同一日期保留后写入的（新数据修正旧数据）
                merged = merged.drop_duplicates(subset=["date"], keep="last")
                merged = merged.sort_values("date").reset_index(drop=True)
                added = merged["date"].nunique() - before
                df = merged

        self._atomic_write(p, df)
        return {
            "written": True,
            "total_rows": len(df),
            "added_rows": max(added, 0),
            "path": str(p),
        }

    def _atomic_write(self, path: Path, df: pd.DataFrame) -> None:
        """先写临时文件再原子替换，避免中断产生半截文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        os.close(fd)
        try:
            df.to_csv(tmp, index=False)
            os.replace(tmp, path)  # Windows/POSIX 均为原子操作
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---------------------------------------------------------------- 列举

    def list_existing(self, category: str) -> list[Path]:
        d = self.root / category
        if not d.exists():
            return []
        return sorted(d.glob("*.csv"))

    def stats(self, category: str) -> dict:
        """返回某类别下的文件数、总行数、最新数据日期，用于体检。"""
        files = self.list_existing(category)
        total_rows = 0
        latest = None
        for f in files:
            try:
                df = pd.read_csv(f, usecols=["date"])
                total_rows += len(df)
                d = pd.to_datetime(df["date"], errors="coerce").max()
                if pd.notna(d) and (latest is None or d > latest):
                    latest = d
            except Exception:
                continue
        return {
            "category": category,
            "files": len(files),
            "total_rows": total_rows,
            "latest_date": None if latest is None else latest.strftime("%Y-%m-%d"),
        }
