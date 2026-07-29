"""指纹计算与四重身份校验。

任何一条不匹配:整批不写回、整批标记异常、服务进入全局人工暂停。
宁可让业务停下来等人,也不让一行数据写错位置。
"""

from __future__ import annotations

import hashlib
import json

from .models import AnalysisResult, ClaimedItem, LeadRow


class IdentityMismatch(Exception):
    def __init__(self, field: str, row_number: int):
        self.field = field
        self.row_number = row_number
        super().__init__(f"row {row_number}: {field} mismatch")


def compute_fingerprint(row: LeadRow) -> str:
    """源行内容指纹:行内容在批处理期间被改动 → 指纹失配 → 拒绝写回。"""
    canonical = json.dumps(
        {
            "customer_id": row.customer_id,
            "row_number": row.row_number,
            "product": row.product,
            "quantity": row.quantity,
            "country": row.country,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def verify_identity(claimed: ClaimedItem, result: AnalysisResult) -> None:
    if result.customer_id != claimed.customer_id:
        raise IdentityMismatch("customer_id", claimed.row_number)
    if result.task_id != claimed.task_id:
        raise IdentityMismatch("task_id", claimed.row_number)
    if result.row_number != claimed.row_number:
        raise IdentityMismatch("row_number", claimed.row_number)
    if result.fingerprint != claimed.fingerprint:
        raise IdentityMismatch("fingerprint", claimed.row_number)
