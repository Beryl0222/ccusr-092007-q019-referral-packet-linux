"""读取项目已确认的最小数据合同，不包含业务流程实现。

样例文件可以携带 ``scenario`` 等新增嵌套字段，本合同只读取并冻结以下七个
既有标识；新增字段不得改变它们的含义与时间语义。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_CONTRACT_FIELDS = frozenset(
    {"schema_version", "record_id", "domain", "occurred_at", "revision", "source"}
)


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: Path) -> DomainRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    unknown = _CONTRACT_FIELDS - payload.keys()
    if unknown:
        raise ValueError(f"样例缺少合同字段: {sorted(unknown)}")
    return DomainRecord(**{k: payload[k] for k in _CONTRACT_FIELDS})
