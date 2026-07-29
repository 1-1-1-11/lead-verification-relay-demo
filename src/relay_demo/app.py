"""FastAPI 服务:领取、写回、全局人工暂停、健康检查、只读监控台。"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .events import EventLog
from .models import AnalysisResult, Batch, ClaimedItem
from .store import FakeSheetStore
from .verify import IdentityMismatch, compute_fingerprint, verify_identity

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
store = FakeSheetStore()
events = EventLog(DATA_DIR / "relay-events.jsonl")

state: dict = {
    "paused": False,
    "pause_reason": "",
    "batches": {},          # job_id -> Batch
    "heartbeat": time.time(),
}


async def _heartbeat() -> None:
    while True:
        state["heartbeat"] = time.time()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_heartbeat())
    yield
    task.cancel()


app = FastAPI(title="Lead Verification Relay Demo", lifespan=lifespan)


# ------------------------------------------------------------------ 健康
@app.get("/healthz")
def healthz() -> dict:
    monitor_lag = round(time.time() - state["heartbeat"], 1)
    return {
        "web": "ok",
        "monitor": {"alive": monitor_lag < 15, "lag_seconds": monitor_lag},
        "paused": state["paused"],
        "pause_reason": state["pause_reason"],
    }


# ------------------------------------------------------------------ 领取
@app.post("/api/batches/claim", response_model=Batch)
def claim_batch(batch_size: int = 3) -> Batch:
    if state["paused"]:
        raise HTTPException(status_code=423, detail=f"全局人工暂停中:{state['pause_reason']}")
    customers = store.claimable_customers()
    if not customers:
        raise HTTPException(status_code=404, detail="没有待处理询盘")

    customer_id = customers[0]
    rows = store.claim(customer_id, batch_size)
    job_id = uuid.uuid4().hex[:12]
    items = [
        ClaimedItem(
            job_id=job_id,
            task_id=uuid.uuid4().hex[:12],
            customer_id=customer_id,
            row_number=r.row_number,
            fingerprint=compute_fingerprint(r),
        )
        for r in rows
    ]
    batch = Batch(job_id=job_id, customer_id=customer_id, items=items)
    state["batches"][job_id] = batch
    events.append(stage="batch_claimed", job_id=job_id, customer_id=customer_id,
                  detail=f"{len(items)} rows")
    return batch


# ------------------------------------------------------------------ 写回
@app.post("/api/batches/{job_id}/writeback")
def writeback(job_id: str, results: list[AnalysisResult]) -> JSONResponse:
    batch = state["batches"].get(job_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="未知批次")
    if state["paused"]:
        raise HTTPException(status_code=423, detail=f"全局人工暂停中:{state['pause_reason']}")

    claimed_by_task = {item.task_id: item for item in batch.items}

    # 先全量校验,再统一写回:任何一条不匹配,整批拒绝
    mismatches = []
    for result in results:
        claimed = claimed_by_task.get(result.task_id)
        if claimed is None:
            mismatches.append({"row_number": result.row_number, "field": "task_id(未知)"})
            continue
        try:
            verify_identity(claimed, result)
        except IdentityMismatch as exc:
            mismatches.append({"row_number": exc.row_number, "field": exc.field})

    if mismatches:
        for item in batch.items:
            store.mark_failed(item.row_number)
        state["paused"] = True
        state["pause_reason"] = f"批次 {job_id} 身份校验失败,需人工核对"
        events.append(stage="batch_rejected", job_id=job_id, customer_id=batch.customer_id,
                      result="global_pause", detail=str(mismatches)[:200])
        raise HTTPException(status_code=409, detail={
            "error": "identity_mismatch", "mismatches": mismatches,
            "action": "整批未写回,已进入全局人工暂停",
        })

    for result in results:
        store.write_back(result.row_number, {
            "analysis": result.analysis,
            "lead_level": result.lead_level,
            "source_url": result.source_url,
        })
        events.append(stage="row_written", job_id=job_id, task_id=result.task_id,
                      customer_id=result.customer_id, row_number=result.row_number,
                      result=result.lead_level)
    return JSONResponse({"written": len(results), "job_id": job_id})


# ------------------------------------------------------------------ 人工操作
@app.post("/api/ops/resume")
def resume() -> dict:
    state["paused"] = False
    state["pause_reason"] = ""
    events.append(stage="manual_resume", result="ok")
    return {"resumed": True}


# ------------------------------------------------------------------ 监控台
_DASHBOARD = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>Relay 监控台</title>
<style>
  body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0f1216;color:#d7dde4;
       max-width:880px;margin:40px auto;padding:0 20px}
  h1{font-size:18px} h2{font-size:14px;color:#8b96a1;margin-top:28px}
  table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid #232a32;
       padding:6px 10px;text-align:left;font-size:13px}
  .pause{background:#3a1d1d;border:1px solid #7f3030;padding:10px 14px;border-radius:6px}
  .ok{color:#5ec98a} .bad{color:#e06c60}
</style></head><body>
<h1>Lead Verification Relay · 监控台</h1>
__PAUSE__
<h2>客户队列</h2>
<table><tr><th>客户</th><th>待处理</th><th>处理中</th><th>已背调</th><th>异常</th></tr>
__ROWS__
</table>
<h2>最近事件(无 PII)</h2>
<table><tr><th>时间</th><th>阶段</th><th>客户</th><th>行</th><th>结果</th></tr>
__EVENTS__
</table>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    pause = (f'<div class="pause">⏸ 全局人工暂停:{state["pause_reason"]} '
             f'— POST /api/ops/resume 恢复</div>') if state["paused"] else '<p class="ok">● 运行中</p>'
    rows = "".join(
        f"<tr><td>{cid}</td><td>{s['pending']}</td><td>{s['processing']}</td>"
        f"<td class='ok'>{s['done']}</td><td class='bad'>{s['failed']}</td></tr>"
        for cid, s in store.stats().items()
    )
    evts = "".join(
        f"<tr><td>{time.strftime('%H:%M:%S', time.localtime(e.get('ts', 0)))}</td>"
        f"<td>{e.get('stage', '')}</td><td>{e.get('customer_id', '')}</td>"
        f"<td>{e.get('row_number', '')}</td><td>{e.get('result', '')}</td></tr>"
        for e in reversed(events.tail(15))
    )
    return _DASHBOARD.replace("__PAUSE__", pause).replace("__ROWS__", rows).replace("__EVENTS__", evts)
