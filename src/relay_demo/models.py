"""数据模型。写回列白名单是代码级边界,不是约定。"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Relay 只允许写回这些列;其余列(分配、邮件状态等)代码层面不可达
WRITABLE_COLUMNS = frozenset({"analysis", "lead_level", "source_url", "verified_time"})

LEAD_LEVELS = ("A-高潜力", "B-有效", "C-待确认")


class LeadRow(BaseModel):
    row_number: int
    customer_id: str
    # 演示用非 PII 字段
    product: str
    quantity: str
    country: str
    status: str = ""  # "" | 处理中 | 已背调 | 背调异常
    analysis: str = ""
    lead_level: str = ""
    source_url: str = ""
    verified_time: str = ""


class ClaimedItem(BaseModel):
    job_id: str
    task_id: str
    customer_id: str
    row_number: int
    fingerprint: str


class Batch(BaseModel):
    job_id: str
    customer_id: str
    items: list[ClaimedItem]


class AnalysisResult(BaseModel):
    """写回请求:身份四要素 + 分析内容。身份要素缺一不可。"""

    customer_id: str
    task_id: str
    row_number: int
    fingerprint: str
    analysis: str = Field(min_length=1)
    lead_level: str
    source_url: str = ""
