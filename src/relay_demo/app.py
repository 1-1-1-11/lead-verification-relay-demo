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
    "batches": {},
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


@app.get("/healthz")
def healthz() -> dict:
    monitor_lag = round(time.time() - state["heartbeat"], 1)
    return {
        "web": "ok",
        "monitor": {"alive": monitor_lag < 15, "lag_seconds": monitor_lag},
        "paused": state["paused"],
        "pause_reason": state["pause_reason"],
    }


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


@app.post("/api/batches/{job_id}/writeback")
def writeback(job_id: str, results: list[AnalysisResult]) -> JSONResponse:
    batch = state["batches"].get(job_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="未知批次")
    if state["paused"]:
        raise HTTPException(status_code=423, detail=f"全局人工暂停中:{state['pause_reason']}")

    claimed_by_task = {item.task_id: item for item in batch.items}

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


@app.post("/api/ops/resume")
def resume() -> dict:
    state["paused"] = False
    state["pause_reason"] = ""
    events.append(stage="manual_resume", result="ok")
    return {"resumed": True}


# 只读监控台：偏值班台，不做成演示首页
_DASHBOARD = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>relay · ops</title>
<style>
:root {
  --bg:#111315; --panel:#181b1e; --line:#2a2f35; --text:#c8ced6;
  --muted:#7a8490; --ok:#6fba8a; --bad:#d17a72; --warn:#c9a45c;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:13px/1.45 var(--mono);padding:0}
.top{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{font-weight:600;letter-spacing:.2px}
.sep{color:var(--line)}
.pill{padding:2px 8px;border:1px solid var(--line);border-radius:3px;color:var(--muted);font-size:12px}
.pill.on{border-color:#2f5a40;color:var(--ok);background:#142019}
.pill.off{border-color:#5a2f2f;color:var(--bad);background:#221616}
.main{padding:14px 16px 28px;max-width:980px}
.grid{display:grid;grid-template-columns:180px 1fr;gap:14px;margin-bottom:16px}
.stat{background:var(--panel);border:1px solid var(--line);padding:10px 12px}
.stat .k{color:var(--muted);font-size:11px;margin-bottom:6px}
.stat .v{font-size:20px;font-weight:600}
.stat .v.bad{color:var(--bad)}
.stat .v.warn{color:var(--warn)}
h2{font-size:12px;color:var(--muted);font-weight:500;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.6px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:11px;background:#15181b}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)} .bad{color:var(--bad)} .mute{color:var(--muted)}
.banner{margin:0 0 14px;padding:8px 10px;border:1px solid #5a2f2f;background:#1c1414;color:var(--bad)}
.banner code{color:var(--text)}
.foot{margin-top:16px;color:var(--muted);font-size:11px}
</style>
</head><body>
<div class="top">
  <div class="brand">relay</div>
  <span class="sep">/</span>
  <div>询盘背调</div>
  <span class="sep">·</span>
  <div class="mute" style="color:var(--muted)">只读</div>
  <div style="flex:1"></div>
  __STATUS__
</div>
<div class="main">
__BANNER__
<div class="grid">
  <div class="stat"><div class="k">待处理</div><div class="v">__PENDING__</div></div>
  <div class="stat"><div class="k">处理中 / 已背调 / 异常</div><div class="v">__PROC__ <span class="mute">/</span> <span class="ok">__DONE__</span> <span class="mute">/</span> <span class="__FAIL_CLASS__">__FAIL__</span></div></div>
</div>
<h2>客户队列</h2>
<table>
  <tr><th>客户</th><th class="num">待处理</th><th class="num">处理中</th><th class="num">已背调</th><th class="num">异常</th></tr>
  __ROWS__
</table>
<h2>最近事件 · 无 PII</h2>
<table>
  <tr><th>时间</th><th>阶段</th><th>客户</th><th class="num">行</th><th>结果</th></tr>
  __EVENTS__
</table>
<div class="foot">demo 本地假表 · 写回前四重身份校验 · 对不上整批拒、全局停</div>
</div>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    stats = store.stats()
    pending = sum(s["pending"] for s in stats.values())
    processing = sum(s["processing"] for s in stats.values())
    done = sum(s["done"] for s in stats.values())
    failed = sum(s["failed"] for s in stats.values())

    if state["paused"]:
        status = '<span class="pill off">已暂停</span>'
        banner = (
            f'<div class="banner">全局人工暂停：{state["pause_reason"]} · '
            f'恢复 <code>POST /api/ops/resume</code></div>'
        )
    else:
        status = '<span class="pill on">运行中</span>'
        banner = ""

    rows = "".join(
        f"<tr><td>{cid}</td>"
        f"<td class='num'>{s['pending']}</td>"
        f"<td class='num'>{s['processing']}</td>"
        f"<td class='num ok'>{s['done']}</td>"
        f"<td class='num bad'>{s['failed']}</td></tr>"
        for cid, s in stats.items()
    ) or "<tr><td colspan='5' class='mute'>暂无客户</td></tr>"

    evts = "".join(
        f"<tr><td class='mute'>{time.strftime('%H:%M:%S', time.localtime(e.get('ts', 0)))}</td>"
        f"<td>{e.get('stage', '')}</td>"
        f"<td>{e.get('customer_id', '') or '—'}</td>"
        f"<td class='num'>{e.get('row_number', '') or '—'}</td>"
        f"<td>{e.get('result', '') or e.get('detail', '') or '—'}</td></tr>"
        for e in reversed(events.tail(20))
    ) or "<tr><td colspan='5' class='mute'>还没有事件</td></tr>"

    return (
        _DASHBOARD
        .replace("__STATUS__", status)
        .replace("__BANNER__", banner)
        .replace("__PENDING__", str(pending))
        .replace("__PROC__", str(processing))
        .replace("__DONE__", str(done))
        .replace("__FAIL__", str(failed))
        .replace("__FAIL_CLASS__", "bad" if failed else "mute")
        .replace("__ROWS__", rows)
        .replace("__EVENTS__", evts)
    )
