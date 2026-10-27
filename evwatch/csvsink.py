"""
csvsink.py — 追加写 CSV 的「inode 守卫」

为什么需要：
  2026-10-23 实测过一次静默丢数据 —— 跑到一半 `battery.csv` 被外部进程换成了
  同名新文件（内容截到 05:29:43），而进程持有的 fd 仍指向已被 unlink 的旧 inode。
  结果：日志、心跳、状态机全部正常，CSV 却 9 小时 44 分没新增一行，一个字都没报错。

  `open(path,'a')` 只认打开那一刻的目录项。修法是每次写之前比一次 inode：
      os.stat(path).st_ino != os.fstat(fh.fileno()).st_ino  →  重开 + 告警

用法：
    sink = CsvSink('battery.csv', ['ts', 'soc'])
    if sink.write([1, 82]):
        push('CSV 被外部替换，已重新打开')
"""
from __future__ import annotations

import csv
import os


class CsvSink:
    def __init__(self, path: str, header: list[str]):
        self.path = path
        self.header = header
        self.reopens = 0
        self.fh = None
        self.w = None
        self._open()

    # ── 内部 ──
    def _open(self) -> None:
        fresh = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self.fh = open(self.path, 'a', newline='', encoding='utf-8')
        self.w = csv.writer(self.fh)
        if fresh:
            self.w.writerow(self.header)
            self.fh.flush()

    def _reopen(self) -> None:
        try:
            if self.fh:
                self.fh.close()
        except Exception:
            pass
        self.reopens += 1
        self._open()

    # ── 对外 ──
    def replaced(self) -> bool:
        """文件是否被换掉（含被删除）"""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return True
        if self.fh is None or self.fh.closed:
            return True
        try:
            return st.st_ino != os.fstat(self.fh.fileno()).st_ino
        except (OSError, ValueError):
            return True

    def write(self, row: list) -> bool:
        """追加一行；返回 True 表示这次写入前发现文件被替换、已重开"""
        was = self.replaced()
        if was:
            self._reopen()
        self.w.writerow(row)
        self.fh.flush()
        return was

    def close(self) -> None:
        try:
            if self.fh:
                self.fh.close()
        except Exception:
            pass
