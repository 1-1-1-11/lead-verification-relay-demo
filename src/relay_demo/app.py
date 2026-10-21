"""FastAPI 服务:领取、写回、全局人工暂停、健康检查、只读监控台。"""

from __future__ import annotations

import asyncio
import html
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .config import settings
from .events import EventLog
from .models import AnalysisResult, Batch, ClaimedItem
from .store import FakeSheetStore
from .verify import IdentityMismatch, compute_fingerprint, verify_identity

store = FakeSheetStore()
events = EventLog(
    settings.data_dir / "relay-events.jsonl",
    max_bytes=settings.event_log_max_bytes,
    backups=settings.event_log_backups,
)

state: dict[str, Any] = {
    "paused": False,
    "pause_reason": "",
    "batches": {},
    "closed_jobs": set(),
    "heartbeat": time.time(),
}


def reset_runtime() -> None:
    """测试用:回到种子数据,并清空暂停、批次和事件日志。"""
    state["paused"] = False
    state["pause_reason"] = ""
    state["batches"].clear()
    state["closed_jobs"].clear()
    state["heartbeat"] = time.time()
    store.rows = FakeSheetStore().rows
    events.clear()


async def _heartbeat() -> None:
    while True:
        state["heartbeat"] = time.time()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["heartbeat"] = time.time()
    task = asyncio.create_task(_heartbeat())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Lead Verification Relay Demo",
    version=__version__,
    lifespan=lifespan,
)


def _monitor_lag() -> float:
    return time.time() - state["heartbeat"]


@app.get("/healthz")
def healthz() -> dict:
    monitor_lag = _monitor_lag()
    return {
        "web": "ok",
        "version": __version__,
        "monitor": {
            "alive": monitor_lag < settings.monitor_max_lag_seconds,
            "lag_seconds": round(monitor_lag, 1),
        },
        "paused": state["paused"],
        "pause_reason": state["pause_reason"],
    }


@app.post("/api/batches/claim", response_model=Batch)
def claim_batch(batch_size: int | None = None) -> Batch:
    if state["paused"]:
        raise HTTPException(status_code=423, detail=f"全局人工暂停中:{state['pause_reason']}")
    if _monitor_lag() >= settings.monitor_max_lag_seconds:
        raise HTTPException(status_code=503, detail="监控心跳停滞，已停止领取新批次")
    size = settings.default_batch_size if batch_size is None else batch_size
    if size < 1 or size > settings.max_batch_size:
        raise HTTPException(
            status_code=422,
            detail=f"batch_size 必须在 1 到 {settings.max_batch_size} 之间",
        )
    customers = store.claimable_customers()
    if not customers:
        raise HTTPException(status_code=404, detail="没有待处理询盘")

    customer_id = customers[0]
    rows = store.claim(customer_id, size, max_batch_size=settings.max_batch_size)
    if not rows:
        raise HTTPException(status_code=404, detail="没有待处理询盘")
    job_id = uuid.uuid4().hex[:12]
    items = [
        ClaimedItem(
            job_id=job_id,
            task_id=uuid.uuid4().hex[:12],
            customer_id=customer_id,
            row_number=row.row_number,
            fingerprint=compute_fingerprint(row),
        )
        for row in rows
    ]
    batch = Batch(job_id=job_id, customer_id=customer_id, items=items)
    state["batches"][job_id] = batch
    events.append(
        stage="batch_claimed",
        job_id=job_id,
        customer_id=customer_id,
        detail=f"rows={len(items)}",
    )
    return batch


def _reject_batch(job_id: str, batch: Batch, mismatches: list[dict[str, object]]) -> None:
    for item in batch.items:
        store.mark_failed(item.row_number)
    state["paused"] = True
    state["pause_reason"] = f"批次 {job_id} 身份校验失败,需人工核对"
    state["closed_jobs"].add(job_id)
    events.append(
        stage="batch_rejected",
        job_id=job_id,
        customer_id=batch.customer_id,
        result="global_pause",
        detail=str(mismatches)[:200],
    )


@app.post("/api/batches/{job_id}/writeback")
def writeback(job_id: str, results: list[AnalysisResult]) -> JSONResponse:
    batch = state["batches"].get(job_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="未知批次")
    if job_id in state["closed_jobs"]:
        raise HTTPException(
            status_code=409,
            detail={"error": "batch_closed", "action": "该批次已结束，不能再次写回"},
        )
    if state["paused"]:
        raise HTTPException(status_code=423, detail=f"全局人工暂停中:{state['pause_reason']}")

    claimed_by_task = {item.task_id: item for item in batch.items}
    mismatches: list[dict[str, object]] = []
    for result in results:
        claimed = claimed_by_task.get(result.task_id)
        if claimed is None:
            mismatches.append({"row_number": result.row_number, "field": "task_id(未知)"})
            continue
        try:
            verify_identity(claimed, result)
            row = store.rows.get(claimed.row_number)
            if row is None or compute_fingerprint(row) != claimed.fingerprint:
                raise IdentityMismatch("fingerprint", claimed.row_number)
        except IdentityMismatch as exc:
            mismatches.append({"row_number": exc.row_number, "field": exc.field})

    if mismatches:
        _reject_batch(job_id, batch, mismatches)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "identity_mismatch",
                "mismatches": mismatches,
                "action": "整批未写回,已进入全局人工暂停",
            },
        )

    claimed_ids = {item.task_id for item in batch.items}
    result_ids = [result.task_id for result in results]
    if len(result_ids) != len(claimed_ids) or set(result_ids) != claimed_ids:
        events.append(
            stage="batch_incomplete",
            job_id=job_id,
            customer_id=batch.customer_id,
            result="rejected",
            detail=f"expected {len(claimed_ids)} got {len(result_ids)}",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "incomplete_batch",
                "expected": len(claimed_ids),
                "received": len(result_ids),
                "action": "结果必须覆盖本批全部任务且不能重复；本次未写回，服务未暂停",
            },
        )

    for result in results:
        store.write_back(
            result.row_number,
            {
                "analysis": result.analysis,
                "lead_level": result.lead_level,
                "source_url": result.source_url,
            },
        )
        events.append(
            stage="row_written",
            job_id=job_id,
            task_id=result.task_id,
            customer_id=result.customer_id,
            row_number=result.row_number,
            result=result.lead_level,
        )
    state["closed_jobs"].add(job_id)
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
        __RULES__
      </div>
    </div>
  </div>
</div>
<div class="foot">本地假表演示 · 正式环境对接 Google Sheet 与网页 ChatGPT</div>
</div>
</body></html>"""

_PLACEHOLDER = re.compile(r"__([A-Z0-9_]+)__")

_STAGE_CN = {
    "batch_claimed": "领取批次",
    "row_written": "写回成功",
    "batch_rejected": "整批拒回",
    "batch_incomplete": "批次不完整",
    "manual_resume": "人工恢复",
}

_DASHBOARD_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fill(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _PLACEHOLDER.sub(replace, template)


def _kv(label: str, value: str, value_class: str = "") -> str:
    klass = f' class="{value_class}"' if value_class else ""
    return f'<div class="row"><span class="k">{label}</span><span{klass}>{value}</span></div>'


def _status_tag(status: str) -> str:
    safe = _esc(status)
    if status == "已背调":
        return f'<span class="tag done">{safe}</span>'
    if status == "处理中":
        return f'<span class="tag proc">{safe}</span>'
    if status == "背调异常":
        return f'<span class="tag fail">{safe}</span>'
    return f'<span class="tag wait">{safe or "待处理"}</span>'


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    stats = store.stats()
    pending = sum(item["pending"] for item in stats.values())
    processing = sum(item["processing"] for item in stats.values())
    done = sum(item["done"] for item in stats.values())
    failed = sum(item["failed"] for item in stats.values())

    if state["paused"]:
        status = '<span class="pill off">已暂停</span>'
        banner = (
            '<div class="banner">本批身份核对未通过，结果未写入：'
            f"{_esc(state['pause_reason'])} · 恢复 <code>POST /api/ops/resume</code></div>"
        )
    else:
        status = '<span class="pill on">运行中</span>'
        banner = ""

    lead_rows = (
        "".join(
            "<tr>"
            f"<td class='num'>{_esc(row.row_number)}</td>"
            f"<td>{_esc(row.customer_id)}</td>"
            f"<td>{_esc(row.product)}</td>"
            f"<td>{_esc(row.country)}</td>"
            f"<td>{_status_tag(row.status)}</td>"
            f"<td>{_esc(row.lead_level or '—')}</td>"
            "</tr>"
            for row in sorted(store.rows.values(), key=lambda item: item.row_number)
        )
        or "<tr><td colspan='6' class='mute'>暂无询盘</td></tr>"
    )

    verified = [row for row in store.rows.values() if row.status == "已背调" and row.analysis]
    if verified:
        sample = sorted(verified, key=lambda row: row.row_number)[-1]
        product = f"{_esc(sample.product)} · {_esc(sample.quantity)}"
        detail = (
            '<div class="detail">'
            + _kv("客户", _esc(sample.customer_id))
            + _kv("行号", _esc(sample.row_number))
            + _kv("产品", product)
            + _kv("等级", _esc(sample.lead_level), "ok")
            + _kv("状态", "已背调 · 指纹核对通过")
            + f'<div class="box">{_esc(sample.analysis)}</div>'
            + "</div>"
        )
    elif state["paused"]:
        detail = (
            '<div class="detail">'
            + _kv("结果", "整批拒回", "bad")
            + _kv("原因", _esc(state["pause_reason"]))
            + '<div class="box">源行指纹与领取时不一致，可能是表格在处理期间被改过。'
            + "本批背调结果未写入；核对表格后可恢复服务。</div>"
            + "</div>"
        )
    else:
        detail = '<p class="mute" style="font-size:13px">领取并写回后，这里会显示单条核对结果。</p>'

    def _event_row(event: dict) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.get("ts", 0)))
        stage = _STAGE_CN.get(event.get("stage", ""), event.get("stage", ""))
        customer = event.get("customer_id", "") or "—"
        row_number = event.get("row_number", "") or "—"
        outcome = event.get("result", "") or event.get("detail", "") or "—"
        return (
            "<tr>"
            f"<td class='mute'>{_esc(stamp)}</td>"
            f"<td>{_esc(stage)}</td>"
            f"<td>{_esc(customer)}</td>"
            f"<td class='num'>{_esc(row_number)}</td>"
            f"<td>{_esc(outcome)}</td>"
            "</tr>"
        )

    evts = (
        "".join(_event_row(event) for event in reversed(events.tail(12)))
        or "<tr><td colspan='5' class='mute'>还没有事件</td></tr>"
    )

    page = _fill(
        _DASHBOARD,
        {
            "STATUS": status,
            "BANNER": banner,
            "PENDING": str(pending),
            "PROC": str(processing),
            "DONE": str(done),
            "FAIL": str(failed),
            "FAIL_CLASS": "bad" if failed else "",
            "LEAD_ROWS": lead_rows,
            "EVENTS": evts,
            "DETAIL": detail,
            "RULES": (
                _kv("核对", "写回前比对客户、任务、行号与指纹")
                + _kv("不一致", "本批不写入，行标为异常，服务进入暂停")
                + _kv("可写列", "analysis / lead_level / source_url / verified_time")
            ),
        },
    )
    return HTMLResponse(page, headers=_DASHBOARD_HEADERS)
