"""数据源 1：可转债打新（信用申购）。

接口：akshare.bond_zh_cov()
- 申购提醒：申购日期 落在 [今天, 今天+lookahead] → 到点下单（信用申购，T 日不占款）
- 缴款提醒：中签号发布日 落在 [今天, 今天+2] → 中签需缴款，务必留足现金
  （连续 3 次未缴款会被拉黑半年，这是这条线唯一的“硬性风险”，单独提示）
- 上市提醒：上市时间 落在 [今天, 今天+2交易日]（v5.0 加）

为什么补上市提醒：打新这条线上**唯一需要你自己做判断的那一天就是上市日**
（卖还是留、什么价卖），而工具原来把你推进仓位之后就再也不提这只债了 ——
申购、缴款两句话说完就断片，最后那一步全靠自己记。

同时刻意划一条界：这一条只报**时点**，不报价格。市面上那种「合理价值 137」
是模型输出、没有出处，一旦印进来，这份报告「规则即收益、不预测」的契约就破了。
工具的职责到「今天该看它」为止。
"""
from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd

from .base import Source
from ..models import Kind, Opportunity, SourceResult, Urgency
from ..utils import (WINDOW_UNKNOWN_MARGIN, fmt_date, load_trade_calendar,
                     parse_date, retry_call, to_float, trading_window_end, within)

# 信用评级序（越高越好），用于 min_rating 过滤
_RATING_RANK = {
    "AAA": 9, "AAA-": 8, "AA+": 7, "AA": 6, "AA-": 5,
    "A+": 4, "A": 3, "A-": 2, "BBB+": 1, "BBB": 0,
}


def _norm_rating(v) -> str:
    """评级归一：去空白、转大写。

    两侧都要过一遍。akshare 那一列实测会带尾随空格（'AA '），而配置是人手写的
    （'aa+' / 'AA '）。上一版两边都直接拿原串查表，于是 `_rating_ok('AA ', 'AA')`
    和 `_rating_ok('AAA', 'aa+')` 全是 False —— 一个空格就能把整条申购线关掉。
    """
    return str(v or "").strip().upper()


def _rating_known(min_rating: str) -> bool:
    """配的这个 min_rating 认不认得出。"""
    return _norm_rating(min_rating) in _RATING_RANK


def _blank(v) -> bool:
    """这一格是不是空的。写法照 `cb_redeem._blank` —— 同一个坑，同一个拦法。

    `str(nan)` 是 `'nan'`、`str(None)` 是 `'None'`，两个都会**当成有值**穿到报告里。
    """
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip() in ("", "nan", "None", "NaN", "-", "—", "NaT")


def _rating_text(v, empty: str = "—") -> str:
    """评级的展示值：空就给占位符，别把 `nan` / `None` 印给读者。

    `_num` 给 `—`、`fmt_date` 给 `—`，这一列原来是 `str(rating)` 直通（v5.9.5 补）。

    **v5.9.6 更正**：v5.9.5 的注释和文档都写着「全项目唯一一处」，那句话是错的 ——
    `cb_allotment` 读的是同一张表的同一列、同样直通，空评级照样印 `nan`。
    现在那边 import 这个函数（不抄第二份），并由
    `test_no_source_prints_nan_where_a_rating_should_be` **按列**钉住两栏。
    """
    return empty if _blank(v) else str(v).strip()


def _rating_ok(rating, min_rating: str) -> bool:
    """评级达标没有。**两侧认不出都放行，不是拦掉**。

    配置侧（v5.9.3）：阈值原来是 `_RATING_RANK.get(min_rating, 99)` —— 认不出就是
    99，比 AAA 还高，于是一个笔误把整条申购线静默关掉。

    数据侧（v5.9.4）：`_RATING_RANK.get(rating, -1)` 是同一个错误的镜像 ——
    接口给的评级认不出就当成「低于一切」。**实盘 08-13 撞上了**：
    `N特宝转（118074）` 的信用评级列是 `AA+sti`，表里只有 `AA+`。
    门一开它就被拦掉，而 v5.9.3 新加的那句话会说「评级低于 min_rating=AA」——
    比静默拦掉更糟：那是**主动断言了一件代码判定不了的事**（AA+sti 大概率是
    AA+ 的某种后缀写法，但「大概率」不是本工具能印出来的东西，见纪律「不造数」）。

    所以：认不出就**不参与这道门**，照常出条，另由 `fetch()` 逐只标出来 ——
    门是减法，减不掉的照实说，不替它猜、也不冒充判过。
    """
    mr = _norm_rating(min_rating)
    if not mr or mr not in _RATING_RANK:
        return True
    r = _norm_rating(rating)
    if r not in _RATING_RANK:
        return True
    return _RATING_RANK[r] >= _RATING_RANK[mr]


def _rating_gate_open(min_rating: str) -> bool:
    """这道门这次到底开着没有：配了、且配的那个值认得出。

    门本来就没开时，下面两个判定一律 False —— 那时谈不上「没参与筛选」，不该多话。
    """
    mr = _norm_rating(min_rating)
    return bool(mr) and mr in _RATING_RANK


def _rating_unreadable(rating, min_rating: str) -> bool:
    """门开着、这只债**有评级值、但本工具认不出这个写法**（如 `AA+sti`）。

    v5.9.5 收窄了一格：原来「空值」也落在这里，于是报告会印
    `窗口内有 3 只的评级取值本工具认不出（AA+sti、nan、）` —— 那个 `nan`
    和那个空串**都不是取值**，而「3 只」后面只列得出 2 个值。

    这两件事在本项目里从来是分开的（`min_convert_value` 那一半 v5.9.4 就分开了）：
      · **认不出** = 有值、写法不认识 → 取值能印出来，让人自己判；
      · **没取到** = 这一格是空的     → 印不出取值，只能说「这次没拿到」。
    合成一句的代价不是多印几个字，是**拿一个空值去填「取值是 x」这个句式**。
    """
    if not _rating_gate_open(min_rating):
        return False
    if _blank(rating):
        return False
    return _norm_rating(rating) not in _RATING_RANK


def _rating_missing(rating, min_rating: str) -> bool:
    """门开着、而这只债的评级**本次没取到**（空 / nan / None）。

    和 `_rating_unreadable` 互斥。同样是 fail-open（`_rating_ok` 里空值放行），
    这里只负责把「它没参与过这道门的筛」照实说出来 —— 空不等于不达标。
    """
    return _rating_gate_open(min_rating) and _blank(rating)


def _mock_df() -> pd.DataFrame:
    """离线自检用：列名严格对齐 bond_zh_cov() 的真实输出。"""
    from datetime import date as _d
    t = _d.today()
    return pd.DataFrame([
        {"债券代码": "123999", "债券简称": "示例转债", "申购日期": t,
         "申购代码": "123999", "申购上限": 100.0, "正股代码": "300999", "正股简称": "示例股份",
         "正股价": 15.30, "转股价值": 98.5, "债现价": 100.0, "转股溢价率": 25.0,
         "原股东配售-股权登记日": t - timedelta(days=1), "原股东配售-每股配售额": 1.3,
         "发行规模": 8.2, "中签号发布日": None, "中签率": None, "上市时间": None, "信用评级": "AA"},
        {"债券代码": "113888", "债券简称": "缴款转债", "申购日期": t - timedelta(days=2),
         "申购代码": "113888", "申购上限": 100.0, "正股代码": "600888", "正股简称": "缴款股份",
         "正股价": 8.60, "转股价值": 105.0, "债现价": 100.0, "转股溢价率": 18.0,
         "原股东配售-股权登记日": t - timedelta(days=3), "原股东配售-每股配售额": 2.1,
         "发行规模": 15.0, "中签号发布日": t, "中签率": 0.0123, "上市时间": None, "信用评级": "AA+"},
        {"债券代码": "123777", "债券简称": "低评级转债", "申购日期": t + timedelta(days=3),
         "申购代码": "123777", "申购上限": 100.0, "正股代码": "300777", "正股简称": "小盘股份",
         "正股价": 9.50, "转股价值": 82.0, "债现价": 100.0, "转股溢价率": 40.0,
         "原股东配售-股权登记日": t + timedelta(days=2), "原股东配售-每股配售额": 0.8,
         "发行规模": 3.1, "中签号发布日": None, "中签率": None, "上市时间": None, "信用评级": "A+"},
        # v5.0：一只“申购已经是三周前、今天上市”的债。没有它，--mock 跑不出上市提醒，
        # 而这一栏是新加的，正是最需要在离线自检里走一遍的那条路径。
        {"债券代码": "113666", "债券简称": "上市转债", "申购日期": t - timedelta(days=25),
         "申购代码": "113666", "申购上限": 100.0, "正股代码": "600666", "正股简称": "上市股份",
         "正股价": 12.40, "转股价值": 103.5, "债现价": 100.0, "转股溢价率": 12.0,
         "原股东配售-股权登记日": t - timedelta(days=26), "原股东配售-每股配售额": 1.1,
         "发行规模": 6.5, "中签号发布日": t - timedelta(days=22), "中签率": 0.0089,
         "上市时间": t, "信用评级": "AA"},
    ])


class CBIpoSource(Source):
    kind = Kind.CB_IPO

    def fetch(self) -> SourceResult:
        res = SourceResult(kind=self.kind)
        c = self.cfg.get("cb_ipo", {})

        if self.ctx.mock:
            df = _mock_df()
            cal = None
        else:
            import akshare as ak
            df, err = retry_call(ak.bond_zh_cov, attempts=3, backoff=(2.0, 5.0),
                                 cache_key=f"bond_zh_cov::{self.ctx.today}",
                                 ttl_seconds=3600)
            if df is None:
                res.error = f"bond_zh_cov 拉取失败：{err}"
                res.notes.append("本栏 0 条 = 完全没取到数；申购与缴款提醒本次均不可信")
                return res
            cal = load_trade_calendar()

        res.rows_scanned = len(df)
        t = self.ctx.today
        look_end = t + timedelta(days=self.ctx.lookahead)

        # 缴款窗口按**交易日**推，不能用日历天。
        # 原来写 t + timedelta(days=2)：周五跑覆盖到周日，下周一的缴款日直接落在窗外，
        # 而缴款是这条线唯一的硬性风险（连续 3 次不缴款拉黑半年）——本该提前提醒的事
        # 变成了当天才提醒。改成交易日后，周五 +2 交易日 = 下周二，周一稳稳盖住。
        # v5.9.2：改走 trading_window_end —— 交易日历只排到当年年底，跨年那几天
        # 「跳周末」回落会把节假日当交易日，窗口**偏短**（实测 12-30 跑，+2 交易日
        # 算出 2027-01-01 元旦当天，实际应为 01-04）。没担保时多开 3 个工作日，
        # 并在栏目级说出来。--mock 关掉边际，自检要可复现。
        margin = 0 if self.ctx.mock else None
        _kw = {} if margin is None else {"unknown_margin": margin}
        pay_days = int(c.get("pay_window_trading_days", 2))
        pay_end, pay_sure = trading_window_end(t, pay_days, cal, **_kw)

        # 上市窗口同样按交易日推：理由和缴款那条一样，周五跑不能把周一漏掉。
        list_days = int(c.get("list_window_trading_days", 2))
        list_end, list_sure = trading_window_end(t, list_days, cal, **_kw)

        if not self.ctx.mock and not (pay_sure and list_sure):
            # 「多开 N 个工作日」的 N 从常量取，不写死在字符串里（v5.9.3）：
            # 改 utils.WINDOW_UNKNOWN_MARGIN 或传 unknown_margin= 时，
            # 报告会跟着改，不会印一个和实际行为对不上的数。
            res.notes.append(
                "交易日历本次没取到，窗口按「跳周末」估 —— 节假日会被当成交易日，"
                "长假前窗口偏短，可能少提醒一天"
                if cal is None else
                f"交易日历没盖住本次窗口，已多开 {WINDOW_UNKNOWN_MARGIN} 个工作日 —— "
                "回落把节假日当交易日会让窗口偏短，长假前宁可多提醒一天")

        # ---- 两道门的计数（v5.9.3）------------------------------------------
        # v5.9.2 修的是「门写成 continue，跳掉整行」，方向对，但门本身**一条都不数**。
        # 于是 `min_rating: "AA"` 一开，窗口里的低评级债静默消失，而下面
        # `_explain_listing_zero` 的第 ③ 支还会说「窗口内确实没有申购/缴款/上市
        # （接口正常）」—— 明明有，是被门吃了。同一轮里 fund_premium 的三个开关
        # 都补了 `n_skip_* / dropped` 并照说，打新这两个漏了。
        #
        # 只数**窗口内**被拦下的：全表 400 多只里绝大多数根本不在申购窗口，
        # 把它们算进来就会印出「另有 380 只被评级门略去」这种没人能用的数
        # —— 那正是这一轮要修的 fund_premium.n_skip_cross 的毛病。
        min_rating = c.get("min_rating", "")
        min_cv = c.get("min_convert_value", 0)
        n_gate_rating = 0       # 窗口内因评级被拦下的申购提醒
        n_gate_cv = 0           # 窗口内因转股价值被拦下的申购提醒
        # v5.9.4：门开着、但这一只的取值本工具判不了 —— **它没被拦，但也没被判过**。
        # 这两个数和上面两个是不同的东西：上面是「按规则减掉的」，这里是
        # 「规则够不着的」。混在一起报会让人以为全表都过了一遍筛子。
        n_unread_rating, unread_vals = 0, []
        # v5.9.5：「取值认不出」和「本次没取到」再拆一层 —— 前者印得出取值，
        # 后者印不出，塞进同一句会让空值冒充成取值（`（AA+sti、nan、）`）。
        n_miss_rating = 0
        n_unread_cv = 0
        if min_rating and not _rating_known(min_rating):
            res.notes.append(
                f"cb_ipo.min_rating 配的是「{min_rating}」，认不出这个评级 "
                f"（可选：{'/'.join(_RATING_RANK)}）—— **本次按「不设评级门」跑**，"
                "没有静默拦掉任何一只")

        # ---- 「上市时间」那一列的取证 ---------------------------------------
        # 这一列和配债的「登记日」是同一类静默陷阱，而且更隐蔽：
        # akshare 是**按位置**给 bond_zh_cov 那 70 多列命名的，东财加一列就整体错位。
        # 错位之后，申购/缴款照常出条（那两列各自错到别的日期字段上，看着仍是日期），
        # 上市提醒却天天 0 条 —— 和「本周没有新债上市」长得一模一样。
        # 语义不变量是现成的：**上市日必在申购日之后**（实务上隔 2-4 周）。
        # 这条一旦大面积不成立，就说明这两列至少有一列接错了字段。
        lst_hits = 0            # 窗口内命中的上市提醒条数
        lst_parsed = 0          # 有可解析上市日的行数
        lst_ok = lst_bad = 0    # 「上市日 > 申购日」成立/不成立的行数
        lst_next = None         # 下一只将上市的债（用于空窗时说清「下次看它」）
        lst_next_name = ""

        for _, r in df.iterrows():
            code = str(r.get("债券代码", "")).strip()
            name = str(r.get("债券简称", "")).strip()
            rating = r.get("信用评级")
            apply_d = parse_date(r.get("申购日期"))
            lot_d = parse_date(r.get("中签号发布日"))
            list_d = parse_date(r.get("上市时间"))
            cv = to_float(r.get("转股价值"))

            if list_d is not None:
                lst_parsed += 1
                if apply_d is not None:
                    if (list_d - apply_d).days > 0:
                        lst_ok += 1
                    else:
                        lst_bad += 1
                if list_d > t and (lst_next is None or list_d < lst_next):
                    lst_next, lst_next_name = list_d, name

            # —— 申购提醒 ——
            # v5.9.2：这两道门原来写成 `continue`，而 `continue` 跳的是**整行**，
            # 于是同一只债的缴款提醒和上市提醒被一起吃掉 —— 和下面那句
            # 「不套 min_rating / min_convert_value」自相矛盾，代码结构上就做不到。
            # 复现：一只今天申购、今天发中签号、评级 A 的债，配 min_rating="AA"，
            # 出条数从 2 掉到 0（缴款提醒静默消失）。而缴款是这条线唯一的硬性风险。
            # 现在门只关**申购**这一条，其余两条照走。
            bad_rating = not _rating_ok(rating, min_rating)
            bad_cv = bool(min_cv and cv is not None and cv < min_cv)
            # 门开着、但这只的取值判不了：没被拦，也没被判过（v5.9.4）。
            # 两种「判不了」分开（v5.9.5）：有值但认不出 / 压根没取到。
            unread_rating = _rating_unreadable(rating, min_rating)
            miss_rating = _rating_missing(rating, min_rating)
            unread_cv = bool(min_cv and cv is None)
            gated = bad_rating or bad_cv
            in_window = within(apply_d, t, look_end)
            if in_window and gated:         # 只数窗口内的，见上面那段注释
                if bad_rating:
                    n_gate_rating += 1
                if bad_cv:
                    n_gate_cv += 1
            if in_window and not gated:
                if unread_rating:
                    n_unread_rating += 1
                    if _rating_text(rating) not in unread_vals:
                        unread_vals.append(_rating_text(rating))
                if miss_rating:
                    n_miss_rating += 1
                if unread_cv:
                    n_unread_cv += 1
                urg = Urgency.TODAY if apply_d == t else Urgency.SOON
                metrics = {
                    "申购代码": str(r.get("申购代码", "")),
                    "评级": _rating_text(rating),
                    "转股价值": _num(cv),
                    "转股溢价率(%)": _num(to_float(r.get("转股溢价率"))),
                    "发行规模(亿)": _num(to_float(r.get("发行规模"))),
                    "申购上限(万元)": _num(to_float(r.get("申购上限"))),
                }
                flags = []
                if cv is not None and cv < 90:
                    flags.append(f"转股价值偏低({cv:.0f})，破发风险")
                # 门开着的时候才说 —— 门没开时每只都挂一句是噪音（单句 ≤60 字）
                if unread_rating:
                    flags.append(f"评级「{_rating_text(rating)}」本工具认不出，没过评级门的筛")
                elif miss_rating:
                    # 空值不许去填「评级「x」认不出」那个句式（v5.9.5）
                    flags.append("评级本次没取到，没过评级门的筛 —— 空不等于不达标")
                if unread_cv:
                    flags.append("转股价值本次没取到，没过 min_convert_value 的筛")
                res.opportunities.append(Opportunity(
                    kind=self.kind, code=code, name=name,
                    action=("今日顶格申购" if urg == Urgency.TODAY else f"待申购（{fmt_date(apply_d)}）"),
                    action_date=apply_d, urgency=urg, metrics=metrics, flags=flags,
                    note="信用申购，申购日无需资金；历史中一签首日盈利约 400–500 元（热度产物，不可外推）",
                ))

            # —— 缴款提醒（同一批债，中签结果日临近）——
            if within(lot_d, t, pay_end):
                urg = Urgency.TODAY if lot_d == t else Urgency.SOON
                res.opportunities.append(Opportunity(
                    kind=self.kind, code=code, name=name,
                    action=f"缴款提醒：确保账户留足现金（中签×1000/签）",
                    action_date=lot_d, urgency=urg,
                    metrics={"中签号发布日": fmt_date(lot_d),
                             "中签率(%)": _num(_pct(to_float(r.get("中签率"))))},
                    flags=["未缴款影响信用，连续3次拉黑半年"],
                ))

            # —— 上市提醒 ——
            # 不套 min_rating / min_convert_value：那两道门是**要不要申购**的判据，
            # 已经在手里的债不该因为评级低就不提醒你它今天上市。
            if within(list_d, t, list_end):
                lst_hits += 1
                urg = Urgency.TODAY if list_d == t else Urgency.SOON
                res.opportunities.append(Opportunity(
                    kind=self.kind, code=code, name=name,
                    action=("新债今日上市：今天要做卖／留的决定"
                            if urg == Urgency.TODAY
                            else f"新债 {fmt_date(list_d)} 上市：先定好卖／留的判据"),
                    action_date=list_d, urgency=urg,
                    metrics={
                        "上市日": fmt_date(list_d),
                        "转股价值": _num(cv),
                        "发行规模(亿)": _num(to_float(r.get("发行规模"))),
                        # 上市提醒不套评级门，但**空值的印法要一致**：
                        # 同一份报告里同一列不该一处印「—」一处印「nan」（v5.9.5）
                        "评级": _rating_text(rating),
                    },
                    note="上市日是打新这条线上唯一要你判断的一天；本栏只报时点，不报价格",
                ))

        if lst_hits:
            res.footnotes.append(
                "「新债上市」这一条只报**时点**，不报价格 —— 不印合理价值、预期涨幅、"
                "目标价这类数。它们是模型输出、没有出处，而这份报告的全部可信度就建立在"
                "「每个数都能指回一个规则或一张表」上。想要参照，去查近期新债首日涨幅的"
                "实际分布（那是可查的事实），别让工具替你造一个数。"
                "印出来的「转股价值」是 bond_zh_cov 表里的原始字段，不是估值")

        # 门略去了几条，照说。措辞刻意只说**申购**那一条被略去 —— 缴款和上市
        # 提醒不受这两道门影响（v5.9.2 修的就是这件事），别让读者以为整只债没了。
        if n_gate_rating:
            res.notes.append(
                f"另有 {n_gate_rating} 只在申购窗口内、但评级低于 "
                f"cb_ipo.min_rating={_norm_rating(min_rating)}，**申购提醒**未列出"
                "（缴款/上市提醒不受这道门影响，照常出条）")
        if n_gate_cv:
            res.notes.append(
                f"另有 {n_gate_cv} 只在申购窗口内、但转股价值低于 "
                f"cb_ipo.min_convert_value={min_cv}，**申购提醒**未列出"
                "（缴款/上市提醒不受这道门影响，照常出条）")
        # 「减掉的」和「够不着的」分开报（v5.9.4）：上面两句是按规则减掉的，
        # 下面这两句是规则判不了、照常出条的 —— 它们不该被读成「都过了筛」。
        if n_unread_rating:
            res.notes.append(
                f"窗口内有 {n_unread_rating} 只的评级取值本工具认不出"
                f"（{'、'.join(unread_vals)}），**没参与评级门的筛、照常出条** —— "
                "认不出≠不达标，本栏不替它猜")
        # 「取值认不出」和「本次没取到」是两句（v5.9.5）：前者印得出取值，
        # 后者印不出。合成一句就会拿空值去冒充取值。
        if n_miss_rating:
            res.notes.append(
                f"窗口内有 {n_miss_rating} 只的信用评级**本次没取到**（这一列是空的），"
                "**没参与评级门的筛、照常出条** —— 空不等于不达标")
        if n_unread_cv:
            res.notes.append(
                f"窗口内有 {n_unread_cv} 只的转股价值本次没取到，"
                "**没参与 min_convert_value 的筛、照常出条** —— 空不等于低")

        self._explain_listing_zero(res, lst_hits, lst_parsed, lst_ok, lst_bad,
                                   lst_next, lst_next_name, list_end,
                                   n_gated=n_gate_rating + n_gate_cv)
        return res

    @staticmethod
    def _explain_listing_zero(res, hits, parsed, ok_n, bad_n,
                              nxt, nxt_name, list_end, n_gated: int = 0) -> None:
        """上市提醒 0 条时说清是哪种 0。

        分寸和配债那边一致：**只在整栏一条都没有时才多说话**。
        申购/缴款出了条、只是这两天没有新债上市 —— 那是常态，
        每天印一句「本周无新债上市」就是在给一栏已经有内容的地方加噪音。

        但「列位移」和「整列取不到」是另一回事：它们会让这一栏天天正常地
        少一块，所以无论本栏有没有别的条目都要报。
        """
        # ① 列位移：先判它，因为它最像“正常”。
        rel_tot = ok_n + bad_n
        if rel_tot >= 20 and ok_n / rel_tot < 0.5:
            res.error = (f"列位可能移位：{rel_tot} 行里只有 {ok_n} 行满足"
                         "「上市日在申购日之后」，这两列大概率接错了字段")
            res.notes.append(
                "上市提醒**不可信** —— akshare 按位置命名 bond_zh_cov 的列，"
                "东财加一列就整体错位；跑 `python diag_allotment.py` 看实际列内容")
            return

        # ② 整列取不到：小表不判（自检的 4 行表里本就只有 1 行有上市日）。
        if parsed == 0 and res.rows_scanned >= 20:
            res.error = f"扫描 {res.rows_scanned} 行，但没有一行有可解析的上市时间"
            res.notes.append(
                "上市提醒 0 条 = 上市时间整列取不到值，不是「近期没有新债上市」")
            return

        # ③ 正常空窗：本栏还有别的条目就闭嘴。
        if hits or res.opportunities:
            return
        # v5.9.3：门吃掉过条目时，这句话不许再说「确实没有」。
        # 复现（两只债今天/三天后申购、评级 A+/A，配 min_rating="AA"）：
        #   旧代码 → 0 条 + 「窗口内确实没有申购/缴款/上市（接口正常）」
        # 窗口里明明有两只。上面已经逐项报了数，这里只需把「确实没有」这个断言
        # 收回来 —— 它是整份报告里最容易被当成结论的一句。
        if n_gated:
            res.notes.append(
                f"本栏 0 条 = 窗口内的 {n_gated} 条申购提醒**全部被上面那两道门略去**，"
                "**不是**「近期没有新债可打」；要看全就把 cb_ipo 的 "
                "min_rating / min_convert_value 放开")
            return
        tail = (f"，下一只是 {nxt_name}（{fmt_date(nxt)}）" if nxt is not None else "")
        res.notes.append(
            f"本栏 0 条 = 窗口内确实没有申购/缴款/上市（接口正常，"
            f"{parsed} 行有上市日{tail}）")


def _num(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return round(v, 2)


def _pct(v):
    # 中签率原始值可能是 0.0123（=1.23%），统一乘 100 展示
    return None if v is None else v * 100
