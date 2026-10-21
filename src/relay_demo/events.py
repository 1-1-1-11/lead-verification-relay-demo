"""有界 JSONL 事件日志:轮转 + 字段白名单。

日志不能成为新的泄露面:不记录提示词、回复正文、URL、PII、完整指纹。
字符串值会被截断,避免调用方把正文塞进 detail。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ALLOWED_FIELDS = frozenset(
    {"stage", "job_id", "task_id", "customer_id", "row_number", "result", "detail"}
)
_MAX_VALUE_CHARS = 200


class EventLog:
    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024, backups: int = 3):
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if backups < 1:
            raise ValueError("backups must be >= 1")
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, **event: object) -> None:
        clean: dict[str, object] = {
            key: _clean_value(value) for key, value in event.items() if key in ALLOWED_FIELDS
        }
        clean["ts"] = round(time.time(), 3)
        payload = (json.dumps(clean, ensure_ascii=False) + "\n").encode("utf-8")
        self._rotate_if_needed(len(payload))
        with self.path.open("ab") as fh:
            fh.write(payload)
            fh.flush()

    def clear(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        for index in range(1, self.backups + 1):
            backup = self._backup(index)
            if backup.exists():
                backup.unlink()

    def tail(self, n: int = 20) -> list[dict]:
        if n <= 0 or not self.path.exists():
            return []
        parsed: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-n:]:
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return parsed

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate_if_needed(self, incoming: int) -> None:
        if not self.path.exists() or self.path.stat().st_size + incoming <= self.max_bytes:
            return
        for index in range(self.backups - 1, 0, -1):
            src = self._backup(index)
            if src.exists():
                src.replace(self._backup(index + 1))
        self.path.replace(self._backup(1))


def _clean_value(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    text = value if isinstance(value, str) else str(value)
    return text[:_MAX_VALUE_CHARS]
