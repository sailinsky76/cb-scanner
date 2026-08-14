"""六个数据源的统一出口。

新增数据源时：在此处导出，并到 run.py 的 _SOURCE_MAP 与 config.yaml 的
sources 开关里各加一行——除此之外不需要改动其他文件。
"""
from .base import Context, Source
from .cb_allotment import CBAllotmentSource
from .cb_approved import CBApprovedSource
from .cb_ipo import CBIpoSource
from .cb_redeem import CBRedeemSource
from .event_arb import EventArbSource
from .fund_premium import FundPremiumSource

__all__ = [
    "Context",
    "Source",
    "CBIpoSource",
    "CBAllotmentSource",
    "CBApprovedSource",
    "CBRedeemSource",
    "FundPremiumSource",
    "EventArbSource",
]
