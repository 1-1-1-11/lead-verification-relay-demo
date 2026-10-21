"""事件日志:字段白名单、按字节轮转、坏行不影响读取。"""

from __future__ import annotations

import json

from relay_demo.events import EventLog


def test_unknown_fields_are_dropped_and_long_values_truncated(tmp_path):
    log = EventLog(tmp_path / "relay-events.jsonl", max_bytes=10_000, backups=2)
    log.append(
        stage="row_written",
        customer_id="demo-motors",
        row_number=2,
        detail="x" * 500,
        analysis="should-not-persist",
        source_url="https://example.com/secret",
        email="person@example.com",
    )
    saved = log.tail(1)[0]
    assert saved["stage"] == "row_written"
    assert saved["customer_id"] == "demo-motors"
    assert saved["row_number"] == 2
    assert len(saved["detail"]) == 200
    assert "analysis" not in saved
    assert "source_url" not in saved
    assert "email" not in saved


def test_rotation_counts_utf8_bytes_and_keeps_backup_cap(tmp_path):
    path = tmp_path / "relay-events.jsonl"
    log = EventLog(path, max_bytes=80, backups=2)
    log.append(stage="s", detail="测" * 40)
    assert not path.with_name(path.name + ".1").exists()
    assert path.stat().st_size > 80

    for index in range(6):
        log.append(stage="s", detail=f"item-{index:02d}-" + ("x" * 40))

    assert path.with_name(path.name + ".1").exists()
    assert path.with_name(path.name + ".2").exists()
    assert not path.with_name(path.name + ".3").exists()
    assert path.stat().st_size <= 80 + 400
    active = path.read_text(encoding="utf-8")
    assert "测" not in active
    assert json.loads(active.splitlines()[-1])["stage"] == "s"


def test_tail_skips_malformed_lines(tmp_path):
    path = tmp_path / "relay-events.jsonl"
    path.write_text('{"stage":"ok","ts":1}\nnot-json\n\n', encoding="utf-8")
    log = EventLog(path, max_bytes=10_000, backups=1)
    assert log.tail(5) == [{"stage": "ok", "ts": 1}]
