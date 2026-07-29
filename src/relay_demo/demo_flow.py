"""演示脚本:完整批次流程 / 指纹篡改攻击演示。

用法:
    python -m relay_demo.demo_flow            # 正常批次
    python -m relay_demo.demo_flow --tamper   # 篡改指纹 → 整批拒绝 + 全局暂停
"""

from __future__ import annotations

import argparse

from fastapi.testclient import TestClient

from .app import app


def _make_results(batch: dict, tamper: bool) -> list[dict]:
    results = []
    for i, item in enumerate(batch["items"]):
        fingerprint = item["fingerprint"]
        if tamper and i == 0:
            fingerprint = "0" * 16  # 模拟源行在批处理期间被改动
        results.append({
            "customer_id": item["customer_id"],
            "task_id": item["task_id"],
            "row_number": item["row_number"],
            "fingerprint": fingerprint,
            "analysis": f"主体核验:公开信息可部分验证。需求匹配:{item['row_number']} 号询盘具备采购意向。建议动作:确认规格与数量。",
            "lead_level": "B-有效",
            "source_url": "https://example.com/directory",
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tamper", action="store_true")
    args = parser.parse_args()

    client = TestClient(app)

    print("== 1. 领取批次 ==")
    resp = client.post("/api/batches/claim", params={"batch_size": 3})
    batch = resp.json()
    print(f"job_id={batch['job_id']}  customer={batch['customer_id']}  rows="
          f"{[i['row_number'] for i in batch['items']]}")

    print("\n== 2. 提交背调结果" + ("(含篡改指纹) ==" if args.tamper else " =="))
    resp = client.post(f"/api/batches/{batch['job_id']}/writeback", json=_make_results(batch, args.tamper))
    print(f"HTTP {resp.status_code}")
    print(resp.json())

    print("\n== 3. 健康检查 ==")
    print(client.get("/healthz").json())

    if args.tamper:
        print("\n== 4. 暂停状态下尝试领取新批次 ==")
        resp = client.post("/api/batches/claim")
        print(f"HTTP {resp.status_code}  {resp.json()['detail']}")
        print("\n== 5. 人工恢复 ==")
        print(client.post("/api/ops/resume").json())


if __name__ == "__main__":
    main()
