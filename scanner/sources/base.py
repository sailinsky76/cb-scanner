"""数据源抽象基类 + 运行上下文。

Context —— 一次扫描的全局状态（配置 / “今天”是哪天 / 是否离线自检）。
四个数据源共享同一个实例，所以 `--date`（回溯）和 `--mock`（自检）
天然对全部源同时生效，不会出现一半源用真实日期、一半源用 date.today()
的错位。capital / accounts / lookahead 做成 property 而不是字段，是为了
让 config.yaml 改完立刻生效，不必重建 Context。

Source —— 数据源约定。子类只要做两件事：
  1) 声明类属性 `kind`（Kind 枚举，决定它在报告里归到哪一栏）
  2) 实现 `fetch() -> SourceResult`

**约定 fetch() 不抛异常**：网络错误用 utils.safe_call 兜住，把失败原因写进
SourceResult.error，让报告底部的“数据源健康”面板如实呈现——单源挂掉不该
让整份日报消失。run.py 外层另有一层 try/except 作为双保险，防止子类没兜住。

要新增数据源（比如后面的“审计层”），照这个接口写一个类、在
sources/__init__.py 导出、在 run.py 的 _SOURCE_MAP 注册即可，其余不用动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..models import Kind, SourceResult


@dataclass
class Context:
    """跨数据源共享的运行上下文。"""

    cfg: dict = field(default_factory=dict)
    today: date = field(default_factory=date.today)
    mock: bool = False

    # ---- 从配置派生的常用值（缺字段/写坏了都回落到默认值，不让定时任务挂掉）----
    @property
    def capital(self) -> float:
        """单账户可用资金（元），用于占用/贡献估算。"""
        try:
            return float(self.cfg.get("capital", 150000) or 0)
        except (TypeError, ValueError):
            return 150000.0

    @property
    def accounts(self) -> int:
        """参与打新的账户数。"""
        try:
            return max(1, int(self.cfg.get("accounts", 1) or 1))
        except (TypeError, ValueError):
            return 1

    @property
    def lookahead(self) -> int:
        """打新/配债向前看多少天。"""
        try:
            return max(0, int(self.cfg.get("lookahead_days", 10) or 0))
        except (TypeError, ValueError):
            return 10


class Source:
    """所有数据源的基类。"""

    kind: Optional[Kind] = None

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.cfg  # 各源读自己那一节：self.cfg.get("cb_ipo", {}) 等

    def fetch(self) -> SourceResult:
        raise NotImplementedError(
            f"{type(self).__name__} 必须实现 fetch() -> SourceResult"
        )

    # 便利方法：源内部提前失败时统一返回结构，而不是抛出
    def _failed(self, msg: str) -> SourceResult:
        return SourceResult(kind=self.kind, error=msg)

    def __repr__(self) -> str:  # 日志可读性
        k = self.kind.value if self.kind else "?"
        return f"<{type(self).__name__} kind={k} mock={self.ctx.mock}>"
