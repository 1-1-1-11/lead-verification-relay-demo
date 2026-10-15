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


# 只读工作台：浅色运营后台，展示队列 / 单条核对 / 停机原因
_DASHBOARD = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>询盘背调</title>
<style>
:root {
  --bg:#f5f6f8; --panel:#fff; --line:#e5e7eb; --text:#111827;
  --muted:#6b7280; --ok:#15803d; --ok-bg:#f0fdf4; --bad:#b91c1c;
  --bad-bg:#fef2f2; --warn:#b45309; --warn-bg:#fffbeb; --soft:#f9fafb;
  --sans:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",
         "Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 var(--sans)}
.top{
  display:flex;align-items:center;gap:10px;padding:12px 20px;
  background:var(--panel);border-bottom:1px solid var(--line);
}
.brand{font-weight:600;font-size:14px}
.sep{color:#d1d5db}
.mute{color:var(--muted)}
.pill{
  margin-left:auto;font-size:12px;padding:3px 10px;border-radius:999px;
  border:1px solid var(--line);background:var(--soft);color:var(--muted);
}
.pill.on{color:var(--ok);border-color:#bbf7d0;background:var(--ok-bg)}
.pill.off{color:var(--bad);border-color:#fecaca;background:var(--bad-bg)}
.wrap{max-width:1080px;margin:0 auto;padding:18px 20px 32px}
.banner{
  margin-bottom:14px;padding:10px 14px;border-radius:8px;
  border:1px solid #fecaca;background:var(--bad-bg);color:var(--bad);
  font-size:13px;
}
.banner code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--text)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.stat{
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:12px 14px;
}
.stat .k{font-size:12px;color:var(--muted);margin-bottom:4px}
.stat .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.stat .v.ok{color:var(--ok)} .stat .v.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:1.35fr .85fr;gap:14px}
.panel{
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:14px 16px;margin-bottom:14px;
}
.panel h2{
  font-size:13px;font-weight:600;margin-bottom:10px;
  display:flex;align-items:center;justify-content:space-between;
}
.panel h2 .hint{font-weight:400;color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 6px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{
  display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;
  border:1px solid var(--line);background:var(--soft);color:var(--muted);
}
.tag.done{color:var(--ok);border-color:#bbf7d0;background:var(--ok-bg)}
.tag.proc{color:var(--warn);border-color:#fde68a;background:var(--warn-bg)}
.tag.fail{color:var(--bad);border-color:#fecaca;background:var(--bad-bg)}
.tag.wait{color:var(--muted)}
.detail .row{display:grid;grid-template-columns:64px 1fr;gap:6px;margin:6px 0;font-size:13px}
.detail .k{color:var(--muted)}
.detail .box{
  margin-top:8px;padding:10px 12px;background:var(--soft);
  border:1px solid var(--line);border-radius:6px;font-size:13px;color:#374151;
}
.ok{color:var(--ok)} .bad{color:var(--bad)}
.foot{margin-top:8px;color:var(--muted);font-size:12px}
@media (max-width:860px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .grid{grid-template-columns:1fr}
}
</style>
</head><body>
<div class="top">
  <div class="brand">询盘背调</div>
  <span class="sep">·</span>
  <div class="mute" style="font-size:13px">本地演示 · 只读</div>
  __STATUS__
</div>
<div class="wrap">
__BANNER__
<div class="stats">
  <div class="stat"><div class="k">待处理</div><div class="v">__PENDING__</div></div>
  <div class="stat"><div class="k">处理中</div><div class="v">__PROC__</div></div>
  <div class="stat"><div class="k">已背调</div><div class="v ok">__DONE__</div></div>
  <div class="stat"><div class="k">异常</div><div class="v __FAIL_CLASS__">__FAIL__</div></div>
</div>
<div class="grid">
  <div>
    <div class="panel">
      <h2>询盘队列 <span class="hint">同一客户成批领取</span></h2>
      <table>
        <tr>
          <th class="num">行</th><th>客户</th><th>产品</th><th>国家</th><th>状态</th><th>等级</th>
        </tr>
        __LEAD_ROWS__
      </table>
    </div>
    <div class="panel">
      <h2>最近动态 <span class="hint">不含正文与 PII</span></h2>
      <table>
        <tr><th>时间</th><th>阶段</th><th>客户</th><th class="num">行</th><th>结果</th></tr>
        __EVENTS__
      </table>
    </div>
  </div>
  <div>
    <div class="panel">
      <h2>最近一条核对 <span class="hint">已写回的询盘</span></h2>
      __DETAIL__
    </div>
    <div class="panel">
      <h2>写回约定</h2>
      <div class="detail">
        <div class="row"><span class="k">核对</span><span>写回前比对客户、任务、行号与指纹</span></div>
        <div class="row"><span class="k">不一致</span><span>本批不写入，行标为异常，服务进入暂停</span></div>
        <div class="row"><span class="k">可写列</span><span>analysis / lead_level / source_url / verified_time</span></div>
      </div>
    </div>
  </div>
</div>
<div class="foot">本地假表演示 · 正式环境对接 Google Sheet 与网页 ChatGPT</div>
</div>
</body></html>"""


_STAGE_CN = {
    "batch_claimed": "领取批次",
    "row_written": "写回成功",
    "batch_rejected": "整批拒回",
    "manual_resume": "人工恢复",
}


def _status_tag(status: str) -> str:
    if status == "已背调":
        return '<span class="tag done">已背调</span>'
    if status == "处理中":
        return '<span class="tag proc">处理中</span>'
    if status == "背调异常":
        return '<span class="tag fail">异常</span>'
    return '<span class="tag wait">待处理</span>'


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
            f'<div class="banner">本批身份核对未通过，结果未写入：'
            f'{state["pause_reason"]} · 恢复 <code>POST /api/ops/resume</code></div>'
        )
    else:
        status = '<span class="pill on">运行中</span>'
        banner = ""

    lead_rows = "".join(
        f"<tr>"
        f"<td class='num'>{r.row_number}</td>"
        f"<td>{r.customer_id}</td>"
        f"<td>{r.product}</td>"
        f"<td>{r.country}</td>"
        f"<td>{_status_tag(r.status)}</td>"
        f"<td>{r.lead_level or '—'}</td>"
        f"</tr>"
        for r in sorted(store.rows.values(), key=lambda x: x.row_number)
    ) or "<tr><td colspan='6' class='mute'>暂无询盘</td></tr>"

    verified = [r for r in store.rows.values() if r.status == "已背调" and r.analysis]
    if verified:
        sample = sorted(verified, key=lambda r: r.row_number)[-1]
        detail = (
            f'<div class="detail">'
            f'<div class="row"><span class="k">客户</span><span>{sample.customer_id}</span></div>'
            f'<div class="row"><span class="k">行号</span><span>{sample.row_number}</span></div>'
            f'<div class="row"><span class="k">产品</span><span>{sample.product} · {sample.quantity}</span></div>'
            f'<div class="row"><span class="k">等级</span><span class="ok">{sample.lead_level}</span></div>'
            f'<div class="row"><span class="k">状态</span><span>已背调 · 指纹核对通过</span></div>'
            f'<div class="box">{sample.analysis}</div>'
            f'</div>'
        )
    elif state["paused"]:
        detail = (
            '<div class="detail">'
            '<div class="row"><span class="k">结果</span><span class="bad">整批拒回</span></div>'
            f'<div class="row"><span class="k">原因</span><span>{state["pause_reason"]}</span></div>'
            '<div class="box">源行指纹与领取时不一致，可能是表格在处理期间被改过。'
            '本批背调结果未写入；核对表格后可恢复服务。</div>'
            '</div>'
        )
    else:
        detail = '<p class="mute" style="font-size:13px">领取并写回后，这里会显示单条核对结果。</p>'

    evts = "".join(
        f"<tr><td class='mute'>{time.strftime('%H:%M:%S', time.localtime(e.get('ts', 0)))}</td>"
        f"<td>{_STAGE_CN.get(e.get('stage', ''), e.get('stage', ''))}</td>"
        f"<td>{e.get('customer_id', '') or '—'}</td>"
        f"<td class='num'>{e.get('row_number', '') or '—'}</td>"
        f"<td>{e.get('result', '') or e.get('detail', '') or '—'}</td></tr>"
        for e in reversed(events.tail(12))
    ) or "<tr><td colspan='5' class='mute'>还没有事件</td></tr>"

    return (
        _DASHBOARD
        .replace("__STATUS__", status)
        .replace("__BANNER__", banner)
        .replace("__PENDING__", str(pending))
        .replace("__PROC__", str(processing))
        .replace("__DONE__", str(done))
        .replace("__FAIL__", str(failed))
        .replace("__FAIL_CLASS__", "bad" if failed else "")
        .replace("__LEAD_ROWS__", lead_rows)
        .replace("__EVENTS__", evts)
        .replace("__DETAIL__", detail)
    )
