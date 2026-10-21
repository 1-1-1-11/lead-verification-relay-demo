"""从环境变量和可选的 .env 文件读取运行配置。

已导出的环境变量优先于 .env，.env 优先于默认值。只读取 RELAY_* 这组键。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class Settings:
    host: str
    port: int
    data_dir: Path
    default_batch_size: int
    max_batch_size: int
    event_log_max_bytes: int
    event_log_backups: int
    monitor_max_lag_seconds: float
    base_url: str


def parse_dotenv(text: str) -> dict[str, str]:
    """解析简单 KEY=VALUE 文件。忽略空行、注释和没有等号的行。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def _require_int(name: str, raw: str, *, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = "none" if maximum is None else str(maximum)
        raise ValueError(f"{name} must be between {minimum} and {upper}, got {value}")
    return value


def load_settings(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    file_vars: dict[str, str] = {}
    if dotenv_path is None and environ is None:
        candidate = Path.cwd() / ".env"
        if candidate.is_file():
            dotenv_path = candidate
    if dotenv_path is not None and Path(dotenv_path).is_file():
        file_vars = parse_dotenv(Path(dotenv_path).read_text(encoding="utf-8"))

    def raw(key: str) -> str | None:
        if key in env and env[key] != "":
            return env[key]
        if key in file_vars and file_vars[key] != "":
            return file_vars[key]
        return None

    host = raw("RELAY_HOST") or "127.0.0.1"
    port = _require_int("RELAY_PORT", raw("RELAY_PORT") or "8017", minimum=1, maximum=65535)
    data_dir = Path(raw("RELAY_DATA_DIR") or "data")
    default_batch = _require_int(
        "RELAY_DEFAULT_BATCH_SIZE",
        raw("RELAY_DEFAULT_BATCH_SIZE") or "3",
        minimum=1,
    )
    max_batch = _require_int(
        "RELAY_MAX_BATCH_SIZE",
        raw("RELAY_MAX_BATCH_SIZE") or "5",
        minimum=1,
    )
    if default_batch > max_batch:
        raise ValueError("RELAY_DEFAULT_BATCH_SIZE cannot exceed RELAY_MAX_BATCH_SIZE")
    max_bytes = _require_int(
        "RELAY_EVENT_LOG_MAX_BYTES",
        raw("RELAY_EVENT_LOG_MAX_BYTES") or str(_DEFAULT_MAX_BYTES),
        minimum=1,
    )
    backups = _require_int(
        "RELAY_EVENT_LOG_BACKUPS",
        raw("RELAY_EVENT_LOG_BACKUPS") or "3",
        minimum=1,
    )
    lag_raw = raw("RELAY_MONITOR_MAX_LAG_SECONDS") or "15"
    try:
        lag = float(lag_raw)
    except ValueError as exc:
        raise ValueError(
            f"RELAY_MONITOR_MAX_LAG_SECONDS must be a number, got {lag_raw!r}"
        ) from exc
    if lag <= 0:
        raise ValueError("RELAY_MONITOR_MAX_LAG_SECONDS must be positive")
    base_url = (raw("RELAY_BASE_URL") or f"http://{host}:{port}").rstrip("/")
    return Settings(
        host=host,
        port=port,
        data_dir=data_dir,
        default_batch_size=default_batch,
        max_batch_size=max_batch,
        event_log_max_bytes=max_bytes,
        event_log_backups=backups,
        monitor_max_lag_seconds=lag,
        base_url=base_url,
    )


settings = load_settings()
