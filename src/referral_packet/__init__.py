"""转诊资料最小交付服务。

对外只暴露稳定的用例入口与数据合同；业务规则见 :mod:`referral_packet.catalog`。
"""

from __future__ import annotations

from referral_packet.contracts import DomainRecord, load_record
from referral_packet.service import ReferralPacketService

__all__ = ["DomainRecord", "load_record", "ReferralPacketService"]
