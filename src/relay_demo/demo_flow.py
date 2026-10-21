"""对正在运行的演示服务走一遍领取与写回。

用法:
    python -m relay_demo.demo_flow            # 正常批次
    python -m relay_demo.demo_flow --tamper   # 篡改指纹 → 整批拒绝 + 全局暂停

脚本通过 HTTP 访问 RELAY_BASE_URL（默认 http://127.0.0.1:8017）。
请先在另一个终端启动 `python -m relay_demo`。
"""

from __future__ import annotations

import argparse
from typing import Any

import httpx

from .config import settings


def _make_results(batch: dict, tamper: bool) -> list[dict]:
    results = []
    for index, item in enumerate(batch["items"]):
        fingerprint = item["fingerprint"]
        if tamper and index == 0:
            fingerprint = "0" * 16  # 模拟调用方提交了与领取时不一致的指纹
        results.append(
            {
                "customer_id": item["customer_id"],
                "task_id": item["task_id"],
                "row_number": item["row_number"],
                "fingerprint": fingerprint,
                "analysis": (
                    "主体核验:公开信息可部分验证。"
                    f"需求匹配:{item['row_number']} 号询盘具备采购意向。"
                    "建议动作:确认规格与数量。"
                ),
                "lead_level": "B-有效",
                "source_url": "https://example.com/directory",
            }
        )
    return results


def _json_body(response: Any) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {"raw": getattr(response, "text", "")}
    if isinstance(body, dict):
        return body
    return {"raw": body}


def run_demo(client: Any, *, tamper: bool, batch_size: int = 3) -> int:
    print("== 1. 领取批次 ==")
    response = client.post("/api/batches/claim", params={"batch_size": batch_size})
    body = _json_body(response)
    print(f"HTTP {response.status_code}")
    print(body)
    if response.status_code != 200:
        return 1
    rows = [item["row_number"] for item in body["items"]]
    print(f"job_id={body['job_id']}  customer={body['customer_id']}  rows={rows}")

    print("\n== 2. 提交背调结果" + ("(含篡改指纹) ==" if tamper else " =="))
    response = client.post(
        f"/api/batches/{body['job_id']}/writeback",
        json=_make_results(body, tamper),
    )
    written = _json_body(response)
    print(f"HTTP {response.status_code}")
    print(written)
    if tamper and response.status_code != 409:
        return 1
    if not tamper and response.status_code != 200:
        return 1

    print("\n== 3. 健康检查 ==")
    health = client.get("/healthz")
    health_body = _json_body(health)
    print(f"HTTP {health.status_code}")
    print(health_body)
    if health.status_code != 200:
        return 1
    if not tamper:
        return 0

    print("\n== 4. 暂停状态下尝试领取新批次 ==")
    blocked = client.post("/api/batches/claim")
    blocked_body = _json_body(blocked)
    print(f"HTTP {blocked.status_code}  {blocked_body.get('detail')}")
    if blocked.status_code != 423:
        return 1

    print("\n== 5. 人工恢复 ==")
    resumed = client.post("/api/ops/resume")
    resumed_body = _json_body(resumed)
    print(f"HTTP {resumed.status_code}")
    print(resumed_body)
    if resumed.status_code != 200 or resumed_body.get("resumed") is not True:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对正在运行的演示服务走一遍领取与写回")
    parser.add_argument("--tamper", action="store_true", help="篡改第一条结果的指纹，触发整批拒绝")
    parser.add_argument("--base-url", default=None, help="覆盖 RELAY_BASE_URL")
    parser.add_argument("--batch-size", type=int, default=None, help="领取条数，默认与服务端一致")
    args = parser.parse_args(argv)
    base_url = (args.base_url or settings.base_url).rstrip("/")
    batch_size = settings.default_batch_size if args.batch_size is None else args.batch_size
    print(f"目标服务 {base_url}")
    try:
        with httpx.Client(base_url=base_url, timeout=10) as client:
            try:
                probe = client.get("/healthz")
                probe.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"无法连接 {base_url}（{exc}）。请先启动：python -m relay_demo")
                return 1
            return run_demo(client, tamper=args.tamper, batch_size=batch_size)
    except httpx.HTTPError as exc:
        print(f"请求失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
