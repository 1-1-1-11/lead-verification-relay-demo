"""核心安全路径测试:身份校验、全局暂停、写回白名单。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay_demo.app import app, state, store
from relay_demo.models import LeadRow
from relay_demo.store import FakeSheetStore
from relay_demo.verify import IdentityMismatch, compute_fingerprint, verify_identity
from relay_demo.models import AnalysisResult, ClaimedItem


@pytest.fixture()
def client():
    state["paused"] = False
    state["pause_reason"] = ""
    state["batches"].clear()
    store.rows = FakeSheetStore().rows
    return TestClient(app)


def _results(batch):
    return [
        {
            "customer_id": i["customer_id"],
            "task_id": i["task_id"],
            "row_number": i["row_number"],
            "fingerprint": i["fingerprint"],
            "analysis": "主体核验:可部分验证。建议:确认规格。",
            "lead_level": "B-有效",
            "source_url": "https://example.com",
        }
        for i in batch["items"]
    ]


def test_happy_path_claim_and_writeback(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 3}).json()
    assert len(batch["items"]) == 3
    assert len({i["customer_id"] for i in batch["items"]}) == 1  # 同批同客户

    resp = client.post(f"/api/batches/{batch['job_id']}/writeback", json=_results(batch))
    assert resp.status_code == 200
    assert resp.json()["written"] == 3
    for item in batch["items"]:
        assert store.rows[item["row_number"]].status == "已背调"
        assert store.rows[item["row_number"]].lead_level == "B-有效"


def test_fingerprint_mismatch_rejects_batch_and_pauses(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 2}).json()
    results = _results(batch)
    results[0]["fingerprint"] = "0" * 16

    resp = client.post(f"/api/batches/{batch['job_id']}/writeback", json=results)
    assert resp.status_code == 409

    # 整批未写回,两行都标记异常
    for item in batch["items"]:
        assert store.rows[item["row_number"]].status == "背调异常"
        assert store.rows[item["row_number"]].analysis == ""

    # 全局暂停:新批次领取被拒绝,人工恢复后可用
    assert client.post("/api/batches/claim").status_code == 423
    client.post("/api/ops/resume")
    assert client.post("/api/batches/claim").status_code == 200


def test_writeback_column_allowlist_is_enforced():
    fs = FakeSheetStore()
    with pytest.raises(PermissionError):
        fs.write_back(2, {"lead_status": "CONTACTED"})  # 非白名单列


def test_identity_verify_all_four_fields():
    row = LeadRow(row_number=5, customer_id="acme", product="motor", quantity="1", country="VN")
    claimed = ClaimedItem(job_id="j", task_id="t", customer_id="acme", row_number=5,
                          fingerprint=compute_fingerprint(row))
    base = {"customer_id": "acme", "task_id": "t", "row_number": 5,
            "fingerprint": claimed.fingerprint, "analysis": "x", "lead_level": "B-有效"}
    verify_identity(claimed, AnalysisResult(**base))  # 合法通过
    for field, bad in [("customer_id", "other"), ("task_id", "x"),
                       ("row_number", 6), ("fingerprint", "0" * 16)]:
        payload = {**base, field: bad}
        with pytest.raises(IdentityMismatch):
            verify_identity(claimed, AnalysisResult(**payload))


def test_healthz_reports_monitor_and_pause(client):
    body = client.get("/healthz").json()
    assert body["web"] == "ok"
    assert body["monitor"]["alive"] is True
    assert body["paused"] is False
