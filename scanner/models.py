"""统一数据模型：把四类异构机会（打新 / 配债 / 折溢价 / 事件套利）
归一到一个 Opportunity 结构，方便排序与统一渲染。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class Kind(str, Enum):
    CB_IPO = "可转债打新"
    CB_ALLOT = "配债一手党"
    FUND_PREM = "LOF/QDII折溢价"
    EVENT = "事件套利"
    # v5.1：栏目名刻意不叫「强赎提醒」——这张表把强赎和自然到期摘牌
    # 放在同一列里，而本版没有能力分开它们（见 sources/cb_redeem.py）。
    CB_REDEEM = "转债退出提醒"
    # v5.9：同样刻意不叫「待发转债」——这一栏里既有还没发的，也有已经发完的
    # （180 天窗口的累计量），发没发只能按正股代码回配、判得出来但判不死。
    # 它能担保的只有一件事：**这些确实获批了**（见 sources/cb_approved.py）。
    CB_APPROVED = "转债获批公告"


# Kind → config.yaml 的 `sources:` 键。**这是唯一一份**（v5.9.3）。
# 以前 run.py 里另有一份手抄的 kmap，只在「源自己没兜住异常」那条兜底分支里用 ——
# 于是新增一个源忘了同步时，**兜底 except 自己会 KeyError**，双保险变单点。
# 现在 run.py 那一支直接用 `cls.kind`，report.py 用这张表反查「这一栏是不是被关了」。
# 三处名单（本表 / run.py._SOURCE_MAP / config.DEFAULTS["sources"]）由
# selftest 的 test_source_registry_has_one_source_of_truth 钉住一致。
SOURCE_KEYS: dict = {}          # 在 Kind 定义之后填（见文件末尾）


class Urgency(str, Enum):
    TODAY = "今日行动"   # 今天必须操作：申购 / 缴款 / 登记日持股
    SOON = "临近"        # 未来几个交易日
    WATCH = "观察"       # 仅跟踪，无硬性时点


# 排序用：今日 > 临近 > 观察
_URGENCY_ORDER = {Urgency.TODAY: 0, Urgency.SOON: 1, Urgency.WATCH: 2}


@dataclass
class Opportunity:
    kind: Kind
    code: str
    name: str
    action: str                          # 建议动作，命令式：如“今日申购”“登记日收盘前持有正股”
    action_date: Optional[date] = None   # 关键时点
    urgency: Urgency = Urgency.WATCH
    metrics: dict = field(default_factory=dict)   # 展示用键值对（有序）
    flags: list = field(default_factory=list)     # 风险 / 提示标签
    link: str = ""
    note: str = ""
    date_desc: bool = False              # True = 这一栏的日期越新越靠前

    def sort_key(self):
        """紧急度 → 日期 → 栏目。日期方向由 `date_desc` 决定。

        大多数栏目的 `action_date` 是**将要到来的时点**（申购日、登记日、缴款日），
        越早越急，所以默认升序、缺日期的排最后。

        事件套利那一栏是反的：它的日期是**已经发生的公告时间**，越新的线索越值钱。
        v4.6 把截断改成了「按公告时间倒序留最新的 N 条」，渲染层却还在升序排，
        于是 08-09 那份里最新的一条（159717 基金合同终止）被排到了最下面，
        而提示还写着「按公告时间倒序」—— 留下的是对的，摆出来的次序是反的。
        `date_desc=True` 让这一栏的渲染跟截断口径对齐。

        用序数而不是直接比 date，是因为倒序要取负；缺日期的两种方向都排最后
        （升序给 date.max，倒序给 0 —— 任何真实日期取负后都是负数）。
        """
        d = self.action_date
        if self.date_desc:
            rank = -d.toordinal() if d else 0
        else:
            rank = (d or date.max).toordinal()
        return (_URGENCY_ORDER[self.urgency], rank, self.kind.value)


@dataclass
class SourceResult:
    """单个数据源的运行结果：机会列表 + 错误 + 口径提示。

    `error` 和 `notes` 分工不同，别混用：
    - `error` 是「这个源没跑对」，进底部健康面板；
    - `notes` 是「结果本身有口径问题」，直接印在**对应栏目标题下面**。

    分开的理由很实际：底部健康面板离栏目太远，而 “LOF 没取到 → 折价 0 条不可信”
    这种话必须贴着那 0 条出现，否则读的人会当成「今天没机会」。

    `footnotes` 是第三层，v4.4 加的，解决的是「每轮都在往里加话、没有一轮往外减」：

        flags     —— **只有这一条才成立**的数值事实（这只 LOF 的滑点是 0.50pt）
        notes     —— **本次运行才成立**的结论（今天折价侧 6 条、合计 910 元）
        footnotes —— **每天都一样**的口径与假设（赎回费为什么按 1.5% 算）

    判据是「换一天跑，这句话会不会变」。不会变的，印在每条上就是把同一句话
    抄 N 遍 —— 08-08 实盘 9 条折价里，光「买卖价差…已折进净收益…」这一句
    111 字就重复了 5 遍。footnotes 全报告去重后只在末尾印一次，正文里留一个
    短标记指过去。信息一句没丢，但决策行边上不再堆常识。
    """
    kind: Kind
    opportunities: list = field(default_factory=list)
    error: Optional[str] = None
    rows_scanned: int = 0
    notes: list = field(default_factory=list)
    footnotes: list = field(default_factory=list)


SOURCE_KEYS.update({
    Kind.CB_IPO: "cb_ipo",
    Kind.CB_ALLOT: "cb_allotment",
    Kind.CB_REDEEM: "cb_redeem",
    Kind.FUND_PREM: "fund_premium",
    Kind.EVENT: "event_arb",
    Kind.CB_APPROVED: "cb_approved",
})
