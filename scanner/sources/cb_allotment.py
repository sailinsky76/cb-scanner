"""数据源 2：配债一手党（原股东优先配售）。

思路：登记日收盘持有正股 → 次日按“每股配售额”获得优先配售权 → 缴款配债。
接口：
- 名单/条款：akshare.bond_zh_cov()（原股东配售-股权登记日 / 每股配售额 / 正股代码）
- 正股 3 月涨幅：akshare.stock_zh_a_hist(adjust='qfq')

筛选规则（对应讨论里的口径）：正股“发债前三个月涨幅 < 30%”才算安全的抢权标的；
涨幅越高，登记日后正股回调吃掉配债收益的风险越大 → 超阈值标红。

占用资金估算（v5.0 起分市场）：
  沪市（11x/118x）最小配售单位 = 1 手 = 10 张 = 1,000 元面值
  深市（12x）    最小配售单位 = 1 张 =        100 元面值
  配 1 个单位所需股数 = ceil(面值 / 每股配售额)，占用市值 = 所需股数 × 正股价。

**v4.6.1 及以前两市统一按 1,000 元算，深市转债的持股需求和占用市值全被放大了
10 倍。** 好消息是这一栏常年 0 条，所以还没害到人；坏消息是它一直印在那儿。

含权量（v5.0 加）= 每股配售额 ÷ 正股价，即每 100 元正股市值能配到多少元转债。
这一栏加它不是为了多个卖点 —— 恰恰相反，它是**警告标签**：本栏靠「股权登记日」
出条，而登记日只在发行公告之后才有值，那时正股通常已经高开完了。含权量正好
把「这笔抢权经不经得起正股回调」量化出来，见 footnote。
"""
from __future__ import annotations

import math
from datetime import timedelta
from functools import partial

import pandas as pd

from .base import Source
from ..models import Kind, Opportunity, SourceResult, Urgency
from ..utils import fmt_date, parse_date, retry_call, to_float, within


def _mock_df() -> pd.DataFrame:
    from .cb_ipo import _mock_df as ipo_mock
    return ipo_mock()  # 复用同一张表（列名一致）


# 「评级」这一列怎么印，由 cb_ipo 那边收口（同一张表的同一列，别抄第二份）
from .cb_ipo import _rating_text        # noqa: E402


# 离线自检用的假涨幅（正股代码 -> 3月涨幅%）
_MOCK_RETURNS = {"300999": 12.0, "600888": 45.0, "300777": 8.0, "600666": 5.0}


def _unit_by_code(code: str):
    """按债券代码前缀判断最小配售单位 → (面值元, 单位名, 是否判定成功)。

    沪市 110/111/113/118 段：不足 1 **手**（10 张 / 1,000 元面值）的按精确算法取整。
    深市 12x 段：走中国结算深圳分公司的配股业务指引，最小单位是 1 **张**（100 元）。

    「一手党」这个江湖名字本身就是沪市专属 —— 它来自沪市不足 1 手时的进位规则，
    深市根本没有「手」这个配售单位。

    判不出前缀时回落到沪市口径（1,000 元）并让调用方标一句：宁可高估占用资金，
    也不要静悄悄地按小单位算出一个买不到的数。
    """
    c = str(code or "").strip()
    if c.startswith("11"):          # 110/111/113/118 —— 沪市（含科创板 118）
        return 1000.0, "手", True
    if c.startswith("12"):          # 123/127/128 —— 深市
        return 100.0, "张", True
    return 1000.0, "手", False


# ---- 每天一字不变的口径，归 footnotes（全报告去重后只印一次）------------------
_ALLOT_FOOTNOTES = (
    "「配1手/1张需持股」= **保证配满**一个最小单位所需的股数。沪市最小单位是 1 手"
    "（10 张 / 1,000 元面值），深市是 1 张（100 元面值）—— 两市规则不同，"
    "v4.6.1 及以前统一按 1,000 元算，深市这一栏的数被放大了 10 倍。"
    "另一件必须说清的事：两市**不足 1 个单位的零头都会进位**，"
    "但进位是**竞争性**的（沪市按尾数从大到小、深市按数量大小排序，直到总量配完），"
    "你的尾数能不能进上去取决于当天有多少账户排在你前面，事前不可知。"
    "所以这里只印「保证配满」这个确定的数，不印「博进位」—— "
    "后者是一个赌注，不是一个可计算的量。",

    "「含权量(%)」= 每股配售额 ÷ 正股价，即每 100 元正股市值能配到多少元转债；"
    "「正股每跌1%需上市溢价X%才打平」= 100 ÷ 含权量，纯算术恒等式，"
    "不含任何定价假设 —— 拿它对一眼近期新债首日涨幅的实际分布，就知道该不该动手。"
    "**这一栏真正的风险在触发时点**：它靠「股权登记日」出条，而登记日只在"
    "**发行公告之后**才有值，通常只提前 2-3 个交易日 —— 那时正股多半已经高开，"
    "此时抢权正是「公告后做一手党往往亏钱」说的那件事。"
    "真正的埋伏窗口在证监会核准到发行公告之间的 1-2 个月，"
    "而那段的数据不在 bond_zh_cov 里（那是已发行转债的表），本工具现在看不到。",
)


class CBAllotmentSource(Source):
    kind = Kind.CB_ALLOT

    def _prior_return(self, stock_code: str):
        """正股近 N 日（≈3 月）前复权涨幅%。mock 下走假数据，避免联网。"""
        if self.ctx.mock:
            return _MOCK_RETURNS.get(stock_code)
        import akshare as ak
        c = self.cfg.get("cb_allotment", {})
        win = int(c.get("prior_window_days", 95))
        start = (self.ctx.today - timedelta(days=win)).strftime("%Y%m%d")
        end = self.ctx.today.strftime("%Y%m%d")
        df, _err = retry_call(
            partial(ak.stock_zh_a_hist, symbol=stock_code, period="daily",
                    start_date=start, end_date=end, adjust="qfq"),
            label=f"stock_zh_a_hist({stock_code})",
            attempts=2, backoff=(1.5,),
            cache_key=f"hist::{stock_code}::{start}::{end}",
            ttl_seconds=6 * 3600,
        )
        if df is None or df.empty or "收盘" not in df.columns:
            return None
        closes = pd.to_numeric(df["收盘"], errors="coerce").dropna()
        if len(closes) < 2 or closes.iloc[0] == 0:
            return None
        return (closes.iloc[-1] / closes.iloc[0] - 1) * 100

    def fetch(self) -> SourceResult:
        res = SourceResult(kind=self.kind)
        c = self.cfg.get("cb_allotment", {})
        max_ret = float(c.get("max_prior_return_pct", 30))

        if self.ctx.mock:
            df = _mock_df()
        else:
            import akshare as ak
            df, err = retry_call(ak.bond_zh_cov, attempts=3, backoff=(2.0, 5.0),
                                 cache_key=f"bond_zh_cov::{self.ctx.today}",
                                 ttl_seconds=3600)
            if df is None:
                res.error = f"bond_zh_cov 拉取失败：{err}"
                res.notes.append("本栏 0 条 = 完全没取到数，不代表近期没有配债登记日")
                return res

        t = self.ctx.today
        end = t + timedelta(days=self.ctx.lookahead)
        res.rows_scanned = len(df)      # 与 cb_ipo 口径一致：表长，用于判断接口是否真的取到数
        cand = 0

        # ---- 「0 条」的取证 -------------------------------------------------
        # 这一栏长期出 0 条，而报告只印一个「无」，看不出是哪种 0：
        #   ① 真没标的 —— 登记日 = 申购日 −1 交易日，只在发行公告出来后才有值，
        #      一只券在 10 天窗口里可见约 2-3 天，空窗很正常；
        #   ② 列位移了 —— akshare 是**按位置**给 bond_zh_cov 那 70 多列命名的，
        #      东财加一列，「原股东配售-股权登记日」就静默接到别的字段上，
        #      拿到的全是过去的日期，天天 0 条且看起来完全正常。
        # 两种 0 在报告里长得一模一样，于是每次都要重做一轮考古。下面三个数
        # 把它们分开：有几行有可解析的登记日、最晚的登记日是哪天、以及
        # 「登记日 ≈ 申购日 −1」这条语义关系还成不成立（成立=列位对，不成立=移位了）。
        parsed = 0          # 有可解析登记日的行数
        latest = None       # 最晚的登记日（可能在过去）
        latest_name = ""
        rel_ok = rel_bad = 0

        for _, r in df.iterrows():
            reg_d = parse_date(r.get("原股东配售-股权登记日"))
            per_share = to_float(r.get("原股东配售-每股配售额"))
            if reg_d is not None:
                parsed += 1
                if latest is None or reg_d > latest:
                    latest, latest_name = reg_d, str(r.get("债券简称", "")).strip()
                apply_d = parse_date(r.get("申购日期"))
                if apply_d is not None:
                    # 登记日应在申购日前 1 天（跨周末最多 3 天）。这条不成立
                    # 就说明这两列里至少有一列接错了字段。
                    if 0 < (apply_d - reg_d).days <= 3:
                        rel_ok += 1
                    else:
                        rel_bad += 1
            if not within(reg_d, t, end) or not per_share or per_share <= 0:
                continue
            cand += 1

            stock_code = str(r.get("正股代码", "")).strip()
            stock_name = str(r.get("正股简称", "")).strip()
            stock_px = to_float(r.get("正股价"))
            bond_code = str(r.get("债券代码", "")).strip()

            face, unit, unit_known = _unit_by_code(bond_code)
            need_shares = math.ceil(face / per_share)   # 配 1 个最小单位所需股数
            occupy = (need_shares * stock_px) if stock_px else None

            # 含权量 = 每股配售额 ÷ 正股价。纯比值，无量纲，与面值无关。
            # 打平溢价 = 100 ÷ 含权量(%)：正股跌 1% 亏掉 占用市值×1%，
            # 而占用市值 = 面值 ÷ 含权量，所以要靠转债这边赚回 1/含权量 的溢价。
            # 两个数都是算术恒等式，一个定价假设都不含。
            eq_w = (per_share / stock_px * 100) if stock_px else None
            breakeven = (100 / eq_w) if eq_w else None

            ret = self._prior_return(stock_code)
            flags = []
            if breakeven is not None:
                # 这句是本栏的核心警示，排在涨幅那句前面：涨幅高不高是概率，
                # 「跌 1% 要多少溢价才打平」是当场就能验的算术。
                # 高含权量的标的打平溢价只有个位数，抹成整数会把 3.3% 印成 3%；
                # 但 8.0% 也不该印成「8.0%」。留一位，再把无意义的 .0 去掉。
                be = (f"{breakeven:.0f}" if breakeven >= 10
                      else f"{breakeven:.1f}".rstrip("0").rstrip("."))
                flags.append(f"正股每跌1%，需上市溢价{be}%才打平")
            if not unit_known:
                flags.append(f"债券代码 {bond_code} 认不出沪深，按沪市1手/1000元估")
            if ret is None:
                flags.append("正股涨幅取数失败，需手动核对")
            elif ret > max_ret:
                flags.append(f"抢权风险：正股3月涨{ret:.0f}% > {max_ret:.0f}%")

            urg = Urgency.TODAY if reg_d == t else Urgency.SOON
            res.opportunities.append(Opportunity(
                kind=self.kind, code=str(r.get("债券代码", "")).strip(),
                name=f"{r.get('债券简称','')}／{stock_name}",
                action=("今日收盘前持有正股（登记日）" if urg == Urgency.TODAY
                        else f"登记日买入正股（{fmt_date(reg_d)}）"),
                action_date=reg_d, urgency=urg,
                metrics={
                    "正股代码": stock_code,
                    "正股价": _num(stock_px),
                    "每股配售额": _num(per_share),
                    "含权量(%)": (_num(eq_w) if eq_w is not None else "—"),
                    # 列名跟着单位走：深市印「配1张需持股」，沪市印「配1手需持股」。
                    # 不跟的话，深市那一行的列名会和它下面的数对不上。
                    f"配1{unit}需持股": need_shares,
                    "占用市值(元)": _num(occupy, 0),
                    "正股3月涨幅(%)": (_num(ret) if ret is not None else "—"),
                    # v5.9.6：这一列原来是 `str(r.get("信用评级", ""))` 直通，
                    # 空评级会印成 `nan`。**和 cb_ipo 那处是同一列、同一张表、
                    # 同一个毛病** —— v5.9.5 只修了 cb_ipo 一边，还在文档里写了
                    # 「全项目唯一一处」，那句话当时就是错的。
                    # 借 cb_ipo 的 `_rating_text`（这个文件本来就从那边借
                    # `_mock_df`，两处读的是 bond_zh_cov 的同一列）——
                    # 不在这里再抄一份「怎么算空」，抄第二份就是下一轮的活。
                    "评级": _rating_text(r.get("信用评级")),
                },
                flags=flags,
                note=(f"配债收益 ≈ 上市溢价 × 面值{face:.0f}元（1{unit}）；"
                      "抢权成本 = 登记日后正股可能回调"),
            ))

        # 口径说明只在**本栏真有条目**时挂上：0 条的日子不该为了自证而变长。
        if cand:
            res.footnotes.extend(_ALLOT_FOOTNOTES)

        self._explain_zero(res, cand, parsed, latest, latest_name, rel_ok, rel_bad, t, end)
        return res

    @staticmethod
    def _explain_zero(res, cand, parsed, latest, latest_name,
                      rel_ok, rel_bad, t, end) -> None:
        """0 条时说清是哪种 0。有条目时什么都不印 —— 报告不该为了自证而变长。"""
        if cand:
            return

        # 列位移是最要命的一种：报告天天正常，天天 0 条。先判它。
        rel_tot = rel_ok + rel_bad
        if rel_tot >= 20 and rel_ok / rel_tot < 0.5:
            res.error = (f"列位可能移位：{rel_tot} 行里只有 {rel_ok} 行满足"
                         "「登记日 = 申购日前 1 天」，这两列大概率接错了字段")
            res.notes.append(
                "本栏 0 条**不可信** —— 跑 `python diag_allotment.py` 看实际列内容；"
                "akshare 的 bond_zh_cov 按位置命名列，东财加一列就会整体错位")
            return

        if parsed == 0:
            res.error = f"扫描 {res.rows_scanned} 行，但没有一行有可解析的股权登记日"
            res.notes.append(
                "本栏 0 条 = 登记日整列取不到值，不是「近期没有配债登记日」。"
                "跑 `python diag_allotment.py` 确认是列名改版还是接口返回变了")
            return

        # 到这里就是正常的空窗。把「下一个登记日是哪天」印出来，
        # 免得每次看到 0 条都要重新怀疑一遍代码。
        tail = ""
        if latest is not None:
            if latest > end:
                tail = (f"表里最晚的登记日是 {fmt_date(latest)}"
                        f"（{latest_name}），落在 {fmt_date(end)} 的窗口之外")
            else:
                tail = (f"表里最晚的登记日是 {fmt_date(latest)}"
                        f"（{latest_name}），已经过去")
        res.notes.append(
            f"本栏 0 条 = 窗口内确实没有登记日（接口正常，{parsed} 行有登记日）。"
            + tail)


def _num(v, nd=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return round(v, nd) if nd else int(round(v))
