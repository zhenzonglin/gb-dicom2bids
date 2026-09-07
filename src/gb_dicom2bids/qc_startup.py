"""Visible startup heartbeat, without workload gates or startup timeouts."""

from __future__ import annotations

import sys
import threading
import time


class StartupProgress:
    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self.started = time.monotonic()
        self.message = "读取配置；不会因 CPU 或磁盘忙碌暂停"
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def _print(self):
        print(
            f"[QC 启动 {time.monotonic() - self.started:.0f}s] {self.message}",
            file=sys.stderr,
            flush=True,
        )

    def stage(self, message: str):
        self.message = message
        self._print()

    def inventory(self, read_bytes: int, total_bytes: int, records: int):
        self.message = (
            f"读取/解析序列清单 {read_bytes / 2**20:.1f}/{total_bytes / 2**20:.1f} MiB"
            f" · 已解析 {records:,} 条（不是重新扫描影像）"
        )

    def _heartbeat(self):
        while not self.stopped.wait(self.interval):
            self._print()

    def __enter__(self):
        self._print()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join()
