"""演示脚本走同一套 HTTP 接口，而不是进程内另一份状态。"""

from __future__ import annotations

from relay_demo.app import state, store
from relay_demo.demo_flow import run_demo


def test_run_demo_happy_path(client):
    assert run_demo(client, tamper=False, batch_size=3) == 0
    assert state["paused"] is False


def test_run_demo_tamper_pauses_and_can_resume(client):
    assert run_demo(client, tamper=True, batch_size=2) == 0
    assert state["paused"] is False
    failed = [row for row in store.rows.values() if row.status == "背调异常"]
    assert len(failed) == 2
