"""有界 JSONL 事件日志:轮转 + 字段白名单。

日志不能成为新的泄露面:不记录提示词、回复正文、URL、PII、完整指纹。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ALLOWED_FIELDS = frozenset({"stage", "job_id", "task_id", "customer_id", "row_number", "result", "detail"})


class EventLog:
    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024, backups: int = 3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, **event) -> None:
        clean = {k: v for k, v in event.items() if k in ALLOWED_FIELDS}
        clean["ts"] = round(time.time(), 3)
        line = json.dumps(clean, ensure_ascii=False)
        self._rotate_if_needed(len(line))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def tail(self, n: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(ln) for ln in lines[-n:]]

    def _rotate_if_needed(self, incoming: int) -> None:
        if not self.path.exists() or self.path.stat().st_size + incoming <= self.max_bytes:
            return
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_suffix(f".jsonl.{i}")
            dst = self.path.with_suffix(f".jsonl.{i + 1}")
            if src.exists():
                src.replace(dst)
        self.path.replace(self.path.with_suffix(".jsonl.1"))
