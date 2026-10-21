"""环境变量与 .env 的优先级。"""

from __future__ import annotations

from pathlib import Path

import pytest

from relay_demo.config import load_settings, parse_dotenv


def test_defaults_when_env_is_empty():
    loaded = load_settings({})
    assert loaded.host == "127.0.0.1"
    assert loaded.port == 8017
    assert loaded.data_dir == Path("data")
    assert loaded.default_batch_size == 3
    assert loaded.max_batch_size == 5
    assert loaded.event_log_max_bytes == 2 * 1024 * 1024
    assert loaded.event_log_backups == 3
    assert loaded.monitor_max_lag_seconds == 15
    assert loaded.base_url == "http://127.0.0.1:8017"


def test_environment_overrides_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# local overrides",
                "RELAY_PORT=9000",
                'RELAY_HOST="127.0.0.1"',
                "export RELAY_DEFAULT_BATCH_SIZE=4",
                "RELAY_DATA_DIR=./var/demo # runtime files",
                "UNRELATED_TOKEN=should-not-matter",
                "",
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_settings({"RELAY_PORT": "9001"}, dotenv_path=env_file)
    assert loaded.port == 9001
    assert loaded.host == "127.0.0.1"
    assert loaded.default_batch_size == 4
    assert loaded.data_dir == Path("./var/demo")
    assert loaded.base_url == "http://127.0.0.1:9001"


def test_explicit_base_url_is_trimmed():
    loaded = load_settings({"RELAY_BASE_URL": "http://127.0.0.1:9000/"})
    assert loaded.base_url == "http://127.0.0.1:9000"


def test_invalid_values_raise():
    with pytest.raises(ValueError, match="RELAY_PORT"):
        load_settings({"RELAY_PORT": "nope"})
    with pytest.raises(ValueError, match="RELAY_DEFAULT_BATCH_SIZE"):
        load_settings({"RELAY_DEFAULT_BATCH_SIZE": "9", "RELAY_MAX_BATCH_SIZE": "2"})
    with pytest.raises(ValueError, match="RELAY_MONITOR_MAX_LAG_SECONDS"):
        load_settings({"RELAY_MONITOR_MAX_LAG_SECONDS": "0"})


def test_parse_dotenv_ignores_comments_and_blank_lines():
    parsed = parse_dotenv("# comment\n\nNO_EQUALS\nexport FOO='bar'\n")
    assert parsed == {"FOO": "bar"}
