"""FakeSheetStore:内存表,模拟 Google Sheets 的读写边界。

生产环境对应 Google Sheets API(服务账号);这里保留相同语义:
- 只可领取 status 为空的行
- 领取即标记「处理中」
- 写回仅允许白名单列
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import WRITABLE_COLUMNS, LeadRow


def _seed_rows() -> list[LeadRow]:
    rows: list[LeadRow] = []
    demo = [
        ("acme-motors", "3 phase motor for conveyor", "10 pcs", "VN"),
        ("acme-motors", "IE3 2.2kW 4 pole", "distributor range", "ZA"),
        ("acme-motors", "gear motor", "2 units", "PH"),
        ("acme-motors", "flange type motor", "2", "PH"),
        ("delta-pumps", "water pump 1.5kW", "12", "PH"),
        ("delta-pumps", "submersible pump", "1 for now", "GH"),
        ("delta-pumps", "booster pump set", "every three months", "DZ"),
    ]
    for i, (cid, product, qty, country) in enumerate(demo, start=2):  # 行号从 2 开始,模拟表头
        rows.append(
            LeadRow(row_number=i, customer_id=cid, product=product, quantity=qty, country=country)
        )
    return rows


class FakeSheetStore:
    def __init__(self) -> None:
        self.rows: dict[int, LeadRow] = {r.row_number: r for r in _seed_rows()}

    def claimable_customers(self) -> list[str]:
        seen: dict[str, int] = {}
        for r in self.rows.values():
            if r.status == "":
                seen.setdefault(r.customer_id, r.row_number)
        return [cid for cid, _ in sorted(seen.items(), key=lambda kv: kv[1])]

    def claim(
        self,
        customer_id: str,
        batch_size: int,
        *,
        max_batch_size: int = 5,
    ) -> list[LeadRow]:
        """领取同一客户最早的若干条待办（行号升序），并立即标记「处理中」。"""
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be >= 1")
        limit = min(batch_size, max_batch_size)
        picked = [
            row
            for row in sorted(self.rows.values(), key=lambda row: row.row_number)
            if row.customer_id == customer_id and row.status == ""
        ][:limit]
        for row in picked:
            row.status = "处理中"
        return picked

    def write_back(self, row_number: int, fields: dict) -> None:
        illegal = set(fields) - WRITABLE_COLUMNS
        if illegal:
            raise PermissionError(f"columns not writable: {sorted(illegal)}")
        row = self.rows[row_number]
        for key, value in fields.items():
            setattr(row, key, value)
        row.status = "已背调"
        row.verified_time = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    def mark_failed(self, row_number: int) -> None:
        self.rows[row_number].status = "背调异常"

    def stats(self) -> dict:
        out: dict[str, dict[str, int]] = {}
        for r in self.rows.values():
            bucket = out.setdefault(
                r.customer_id, {"pending": 0, "processing": 0, "done": 0, "failed": 0}
            )
            key = {"": "pending", "处理中": "processing", "已背调": "done"}.get(r.status, "failed")
            bucket[key] += 1
        return out
