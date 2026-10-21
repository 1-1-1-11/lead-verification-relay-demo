"""核心安全路径测试:身份校验、全局暂停、写回白名单。"""

from __future__ import annotations

import time

import pytest

from relay_demo.app import events, state, store
from relay_demo.config import settings
from relay_demo.models import LEAD_LEVELS, WRITABLE_COLUMNS, AnalysisResult, ClaimedItem, LeadRow
from relay_demo.store import FakeSheetStore
from relay_demo.verify import IdentityMismatch, compute_fingerprint, verify_identity


def _results(batch: dict) -> list[dict]:
    return [
        {
            "customer_id": item["customer_id"],
            "task_id": item["task_id"],
            "row_number": item["row_number"],
            "fingerprint": item["fingerprint"],
            "analysis": "主体核验:可部分验证。建议:确认规格。",
            "lead_level": "B-有效",
            "source_url": "https://example.com",
        }
        for item in batch["items"]
    ]


def test_happy_path_claim_and_writeback(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 3}).json()
    assert [item["row_number"] for item in batch["items"]] == [2, 3, 4]
    assert len({item["customer_id"] for item in batch["items"]}) == 1

    response = client.post(f"/api/batches/{batch['job_id']}/writeback", json=_results(batch))
    assert response.status_code == 200
    assert response.json()["written"] == 3
    for item in batch["items"]:
        row = store.rows[item["row_number"]]
        assert row.status == "已背调"
        assert row.lead_level == "B-有效"
        assert row.verified_time


def test_claim_uses_configured_default_batch_size(client):
    batch = client.post("/api/batches/claim").json()
    assert len(batch["items"]) == min(settings.default_batch_size, 4)
    assert batch["items"][0]["row_number"] == 2


def test_batch_size_out_of_range_is_rejected(client):
    for size in (0, settings.max_batch_size + 1):
        response = client.post("/api/batches/claim", params={"batch_size": size})
        assert response.status_code == 422
    assert all(row.status == "" for row in store.rows.values())


def test_fingerprint_mismatch_rejects_batch_and_pauses(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 2}).json()
    results = _results(batch)
    results[0]["fingerprint"] = "0" * 16

    response = client.post(f"/api/batches/{batch['job_id']}/writeback", json=results)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "identity_mismatch"

    for item in batch["items"]:
        row = store.rows[item["row_number"]]
        assert row.status == "背调异常"
        assert row.analysis == ""

    assert client.post("/api/batches/claim").status_code == 423
    assert client.post("/api/ops/resume").status_code == 200
    assert client.post("/api/batches/claim").status_code == 200


def test_changed_source_row_is_rejected_even_with_original_fingerprint(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 2}).json()
    row_number = batch["items"][0]["row_number"]
    store.rows[row_number].product = "changed after claim"

    response = client.post(f"/api/batches/{batch['job_id']}/writeback", json=_results(batch))
    assert response.status_code == 409
    assert state["paused"] is True
    assert store.rows[row_number].status == "背调异常"
    assert store.rows[row_number].analysis == ""
    logged = events.path.read_text(encoding="utf-8")
    assert "changed after claim" not in logged


def test_rejected_batch_stays_closed_after_resume(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 2}).json()
    tampered = _results(batch)
    tampered[0]["fingerprint"] = "0" * 16
    assert (
        client.post(f"/api/batches/{batch['job_id']}/writeback", json=tampered).status_code == 409
    )

    assert client.post("/api/ops/resume").json()["resumed"] is True
    again = client.post(f"/api/batches/{batch['job_id']}/writeback", json=_results(batch))
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "batch_closed"
    for item in batch["items"]:
        assert store.rows[item["row_number"]].status == "背调异常"
        assert store.rows[item["row_number"]].analysis == ""


def test_successful_batch_cannot_be_written_twice(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 1}).json()
    url = f"/api/batches/{batch['job_id']}/writeback"
    assert client.post(url, json=_results(batch)).status_code == 200
    second = _results(batch)
    second[0]["lead_level"] = "A-高潜力"
    second[0]["analysis"] = "第二次不该写进去"
    response = client.post(url, json=second)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "batch_closed"
    row = store.rows[batch["items"][0]["row_number"]]
    assert row.lead_level == "B-有效"
    assert row.analysis != "第二次不该写进去"


def test_partial_writeback_does_not_commit_or_pause(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 3}).json()
    partial = client.post(
        f"/api/batches/{batch['job_id']}/writeback",
        json=_results(batch)[:2],
    )
    assert partial.status_code == 409
    assert partial.json()["detail"]["error"] == "incomplete_batch"
    assert state["paused"] is False
    for item in batch["items"]:
        row = store.rows[item["row_number"]]
        assert row.status == "处理中"
        assert row.analysis == ""

    done = client.post(f"/api/batches/{batch['job_id']}/writeback", json=_results(batch))
    assert done.status_code == 200


def test_duplicate_results_do_not_double_write(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 2}).json()
    results = _results(batch)
    results.append(results[0])
    response = client.post(f"/api/batches/{batch['job_id']}/writeback", json=results)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "incomplete_batch"
    assert state["paused"] is False
    assert all(store.rows[item["row_number"]].analysis == "" for item in batch["items"])


def test_unknown_lead_level_is_rejected(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 1}).json()
    results = _results(batch)
    results[0]["lead_level"] = "HOT"
    response = client.post(f"/api/batches/{batch['job_id']}/writeback", json=results)
    assert response.status_code == 422
    assert state["paused"] is False
    assert store.rows[batch["items"][0]["row_number"]].status == "处理中"
    assert set(LEAD_LEVELS) == {"A-高潜力", "B-有效", "C-待确认"}


def test_unknown_batch_returns_404(client):
    response = client.post("/api/batches/does-not-exist/writeback", json=[])
    assert response.status_code == 404


def test_stale_monitor_blocks_new_claims(client):
    state["heartbeat"] = 0
    blocked = client.post("/api/batches/claim")
    assert blocked.status_code == 503
    assert all(row.status == "" for row in store.rows.values())
    health = client.get("/healthz").json()
    assert health["monitor"]["alive"] is False

    state["heartbeat"] = time.time()
    assert client.post("/api/batches/claim").status_code == 200


def test_event_log_omits_analysis_url_and_fingerprint(client):
    batch = client.post("/api/batches/claim", params={"batch_size": 1}).json()
    results = _results(batch)
    results[0]["analysis"] = "UNIQUE_ANALYSIS_BODY"
    results[0]["source_url"] = "https://example.com/secret-path"
    assert client.post(f"/api/batches/{batch['job_id']}/writeback", json=results).status_code == 200
    text = events.path.read_text(encoding="utf-8")
    assert "UNIQUE_ANALYSIS_BODY" not in text
    assert "secret-path" not in text
    assert batch["items"][0]["fingerprint"] not in text
    assert "B-有效" in text


def test_dashboard_renders_queue_and_escapes_untrusted_text(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "acme-motors" in page.text
    assert "待处理" in page.text
    assert "运行中" in page.text
    assert "写回前比对客户、任务、行号与指纹" in page.text
    assert "__RULES__" not in page.text
    assert "__PENDING__" not in page.text

    batch = client.post("/api/batches/claim", params={"batch_size": 1}).json()
    results = _results(batch)
    results[0]["analysis"] = '<script>alert("x")</script>'
    assert client.post(f"/api/batches/{batch['job_id']}/writeback", json=results).status_code == 200
    written = client.get("/").text
    assert '<script>alert("x")</script>' not in written
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in written

    state["paused"] = True
    state["pause_reason"] = "<img src=x onerror=alert(1)>"
    paused = client.get("/").text
    assert "<img src=x" not in paused
    assert "&lt;img src=x onerror=alert(1)&gt;" in paused


def test_writeback_column_allowlist_is_enforced():
    sheet = FakeSheetStore()
    with pytest.raises(PermissionError):
        sheet.write_back(2, {"lead_status": "CONTACTED"})
    assert WRITABLE_COLUMNS == {"analysis", "lead_level", "source_url", "verified_time"}


def test_claim_rejects_non_positive_batch_size():
    sheet = FakeSheetStore()
    with pytest.raises(ValueError):
        sheet.claim("acme-motors", 0)


def test_identity_verify_all_four_fields():
    row = LeadRow(row_number=5, customer_id="acme", product="motor", quantity="1", country="VN")
    claimed = ClaimedItem(
        job_id="j",
        task_id="t",
        customer_id="acme",
        row_number=5,
        fingerprint=compute_fingerprint(row),
    )
    base = {
        "customer_id": "acme",
        "task_id": "t",
        "row_number": 5,
        "fingerprint": claimed.fingerprint,
        "analysis": "x",
        "lead_level": "B-有效",
    }
    verify_identity(claimed, AnalysisResult(**base))
    for field, bad in [
        ("customer_id", "other"),
        ("task_id", "x"),
        ("row_number", 6),
        ("fingerprint", "0" * 16),
    ]:
        payload = {**base, field: bad}
        with pytest.raises(IdentityMismatch):
            verify_identity(claimed, AnalysisResult(**payload))


def test_healthz_reports_monitor_and_pause(client):
    body = client.get("/healthz").json()
    assert body["web"] == "ok"
    assert body["version"] == "0.1.0"
    assert body["monitor"]["alive"] is True
    assert body["paused"] is False
