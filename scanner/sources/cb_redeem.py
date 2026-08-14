"""数据源 5：转债退出提醒（阶段 2，v5.1）。

接口：akshare.bond_cb_redeem_jsl()（集思录·强赎汇总，实测 319 行 × 18 列）

这一栏补的是本工具最大的空白：**它管你进场，不管你离场**。
过了最后交易日还没动手，这只债就不能卖了，剩下的只能按条款清算。

──────────────────────────────────────────────────────────────────────
这一版**刻意只读一列**：最后交易日

`diag_redeem.py` 坐实了这一列拿得到（docs/probes/probe.txt），`diag_redeem2.py` 回答了
「拿到的这一列能不能分开强赎与到期摘牌」—— **退出码 1：分不开**（docs/probes/probe2.txt）。

判不了的原因不是缺列，而是**样本一边倒**：08-10 那天 319 行里只有 6 行带
最后交易日，而这 6 行的到期日全都紧贴最后交易日（间隔 4-6 天，都是自然到期
摘牌形态），远期那一组是 **0 行**。没有远期样本，就无法验证 `强赎状态` 的
字面意思和间隔证据对不对得上 —— 两个证据里只剩一个时不下结论。

    齐翔转2  最后交易日 08-14  到期日 2026-08-20   ← 间隔 6 天
    嘉泽转债  最后交易日 08-18  到期日 2026-08-24   ← 间隔 6 天

（v5.1-rc 的注释里拿「正帆 间隔 1684 天」当强赎样例，**那是错的**：
probe2 按代码回配发现 118053 根本不在这张表里，第一次探针是按名称模糊
匹配捞错了行。这条错例已经删掉 —— 现在这张表里一个远期样本都没有。）

`强赎状态` 的取值倒是探明了（4 个：空 263 / 公告不强赎 48 / 已公告强赎 6 /
公告要强赎 2），但**探明取值 ≠ 可以拿它下判断**：它是否真能分开两者还没被
证伪过一次。所以本源仍然一列不碰它，等某天远期组攒够 ≥2 行再跑一次探针。

所以本栏的措辞一律用「最后交易日」这五个字，**一个字的强赎判断都不下**：

  · 动作词里不出现「强赎」「到期摘牌」这类**针对某一只债的分类**；
  · 但栏目级的那句「本栏不区分强赎与到期摘牌」必须说 —— 那是在**声明限制**，
    和替某只债下判断是相反的两件事。选择性沉默才是这里真正的风险。
  · 到期日一并印出来：读者拿两个原始日期自己就能看出个大概，
    而工具替他把这一步做掉、还印成结论，才是纪律 2 拦的东西。

──────────────────────────────────────────────────────────────────────
三档，缺一不可

  ① 窗口内   最后交易日 ∈ [今天, 今天+N 交易日]   紧急度按剩余交易日
  ② 已过     最后交易日 ∈ [今天-lookback, 今天)   观察档，提醒你核对持仓
  ③ 名单内但日期未取到                            观察档，**不给动作词**

第 ③ 档的判据被 probe2 改过一次，过程值得留着：
v5.1-rc 设它是为了春23/应流 —— docs/probes/probe.txt 显示这两只在表里**有行、日期是空的**，
只按「日期在窗口内」出条它们会静默消失（纪律 5 的头号敌人）。
但 probe2 把这一档的真实规模测出来了：**313/319 行日期为空（98%）**。
也就是说「空」不是异常，是这张表的常态 —— 它的含义不是「这只债的日期丢了」，
而是「这只债还没有最后交易日」。按代码排序截 20 条的后果离线复演过：
印出来的是代码最小那批噪音，而要救的春23(113667)/应流(113697) 恰好落在配额外面。
→ 所以「空占多数」时**一条都不逐行印**，改在栏目级把覆盖率说全（见 §③档的收口）。
少印可以，少说不可以；假装有覆盖不行。

`强赎天计数` 那一列只在第 ③ 档原样透传，**本工具不解析、不解释它**，
连 `parse_date` 都不许调 —— `'12/15 | 30'` 会被解析成 `0001-12-15`
（按空格截断成 `12/15`，pandas 兜底读成 12 月 15 日，年份缺省 0001），
而且只在首个数字 ≤12 时中招，恰好是倒计时刚开始的那批。
probe2 还测出这一列**会带 HTML**（临近到期那批的取值形如
`临近到期 <span style="color:red;font-weight:bold;">!</span><br>2026-08-14 最后交易`），
所以透传前过一遍 `strip_html`：只删标签、不动文本，也照旧不解释它的含义。
"""
from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd

from .base import Source
from ..models import Kind, Opportunity, SourceResult, Urgency
from ..utils import (WINDOW_UNKNOWN_MARGIN, fmt_date, load_trade_calendar,
                     parse_date, retry_call, strip_html, to_float,
                     trading_days_between, trading_window_end, within)

# bond_cb_redeem_jsl 的 18 列，列名照 docs/probes/probe.txt 抄。
# 本源只用其中 5 列：代码 / 名称 / 最后交易日 / 到期日 / 现价，
# 外加第 ③ 档原样透传的 强赎天计数。其余 12 列一列不碰。
_COL_CODE = "代码"
_COL_NAME = "名称"
_COL_LAST_TRADE = "最后交易日"
_COL_MATURITY = "到期日"
_COL_PRICE = "现价"
_COL_COUNTDOWN = "强赎天计数"


def _mock_df() -> pd.DataFrame:
    """离线自检用：列名严格对齐 bond_cb_redeem_jsl() 的真实输出（18 列）。

    四行分别走完三档 + 一条窗口外的对照，缺一条就有一条路径在 --mock 下走不到。
    """
    from datetime import date as _d
    t = _d.today()

    def row(code, name, last_trade, maturity, price, countdown=None, status=""):
        return {
            "代码": code, "名称": name, "现价": price,
            "正股代码": "600000", "正股名称": "示例股份",
            "规模": 10.0, "剩余规模": 9.8,
            "转股起始日": t - timedelta(days=400),
            "最后交易日": last_trade, "到期日": maturity,
            "转股价": 12.0, "强赎触发比": "130", "强赎触发价": 15.6,
            "正股价": 16.2, "强赎价": 100.162,
            "强赎天计数": countdown, "强赎条款": "15/30, 130%",
            # 取值照 probe2 实测（空 / 公告不强赎 / 已公告强赎 / 公告要强赎）。
            # 本源一列不碰它 —— 探明取值不等于可以拿它下判断。
            "强赎状态": status,
        }

    return pd.DataFrame([
        # ① 今天就是最后交易日
        row("113001", "今日转债", t, t + timedelta(days=6), 128.5),
        # ① 窗口内，还有几天
        row("123002", "临近转债", t + timedelta(days=3), t + timedelta(days=9), 131.2),
        # ② 已过最后交易日
        row("113003", "已过转债", t - timedelta(days=2), t + timedelta(days=4), 100.4),
        # ③ 在表里、最后交易日为空（春23 / 应流就是这一档），带倒计时原始值
        row("113004", "无日期转债", None, t + timedelta(days=900), 119.8,
            "12/15 | 30", "公告要强赎"),
        # 对照：窗口外，不该出条
        row("123005", "远期转债", t + timedelta(days=45), t + timedelta(days=51), 105.0),
    ])


class CBRedeemSource(Source):
    kind = Kind.CB_REDEEM

    def fetch(self) -> SourceResult:
        res = SourceResult(kind=self.kind)
        c = self.cfg.get("cb_redeem", {})

        if self.ctx.mock:
            df = _mock_df()
            cal = None
        else:
            import akshare as ak
            fn = getattr(ak, "bond_cb_redeem_jsl", None)
            if fn is None:
                res.error = "本机 akshare 没有 bond_cb_redeem_jsl（版本太旧？）"
                res.notes.append(
                    "本栏 0 条 = 接口不存在，**不是**「近期没有债要退出」；"
                    "先 pip install -U akshare")
                return res
            df, err = retry_call(fn, attempts=3, backoff=(2.0, 5.0),
                                 cache_key=f"bond_cb_redeem_jsl::{self.ctx.today}",
                                 ttl_seconds=3600)
            if df is None:
                res.error = f"bond_cb_redeem_jsl 拉取失败：{err}"
                res.notes.append(
                    "本栏 0 条 = 完全没取到数；手里有临近退出的债也不会出现在这里")
                return res
            cal = load_trade_calendar()

        res.rows_scanned = len(df)
        cols = {str(x) for x in df.columns}
        if _COL_LAST_TRADE not in cols:
            res.error = f"表里没有「{_COL_LAST_TRADE}」列（实际列：{sorted(cols)}）"
            res.notes.append(
                f"本栏 0 条 = 列名改版，「{_COL_LAST_TRADE}」这一列没了；"
                "跑 `python diag_redeem2.py` 看现在叫什么")
            return res

        t = self.ctx.today
        # v5.9.2：改走 trading_window_end。这一栏对窗口偏短最敏感 —— 漏掉一只
        # = 那只债卖不掉了。交易日历只排到当年年底，跨年那几天回落成「跳周末」
        # 会把元旦当交易日（实测 12-29 跑 +5 交易日算出 01-05，实际应为 01-06）。
        # 没担保时多开 3 个工作日并在栏目级说出来；--mock 关掉边际保持自检可复现。
        win_days = _int(c.get("exit_window_trading_days", 5), 5)
        _kw = {"unknown_margin": 0} if self.ctx.mock else {}
        win_end, win_sure = trading_window_end(t, win_days, cal, **_kw)
        if not self.ctx.mock and not win_sure:
            res.notes.append(
                "交易日历本次没取到，窗口按「跳周末」估 —— 节假日会被当成交易日，"
                "长假前窗口偏短，可能少提醒一天"
                if cal is None else
                f"交易日历没盖住本次窗口，已多开 {WINDOW_UNKNOWN_MARGIN} 个工作日 —— "
                "回落把节假日当交易日会让窗口偏短，而这一栏漏一只就是那只债卖不掉了")
        back_days = _int(c.get("past_lookback_days", 10), 10)
        past_start = t - timedelta(days=back_days)
        show_past = bool(c.get("show_past", True))

        max_unknown = _int(c.get("max_unknown", 20), 20)
        unknown_pool = []           # 第 ③ 档先收着，出完循环再按配额截断
        n_window = n_past = n_unknown = 0
        parsed = 0                  # 有可解析最后交易日的行数
        ord_ok = ord_bad = 0        # 「到期日 ≥ 最后交易日」成立/不成立的行数

        for _, r in df.iterrows():
            code = str(r.get(_COL_CODE, "")).strip()
            name = str(r.get(_COL_NAME, "")).strip()
            last_d = parse_date(r.get(_COL_LAST_TRADE))
            mat_d = parse_date(r.get(_COL_MATURITY))
            price = to_float(r.get(_COL_PRICE))

            # 列位移取证：语义不变量是「到期日不早于最后交易日」。
            # 大面积不成立 = 这两列至少有一列接错了字段。
            if last_d is not None:
                parsed += 1
                if mat_d is not None:
                    if mat_d >= last_d:
                        ord_ok += 1
                    else:
                        ord_bad += 1

            # ---- ③ 在表里但最后交易日未取到 ----------------------------------
            # 放在最前面判，是因为它最容易被写漏 —— 前两档都是 `if within(...)`，
            # 而 within(None, …) 是 False，什么都不写的话这一档就静默消失了。
            if last_d is None:
                n_unknown += 1
                metrics = {"最后交易日": "未取到", "到期日": fmt_date(mat_d, "%Y-%m-%d"),
                           "现价": _num(price)}
                # 倒计时原始值不进 metrics：它的取值形如 `20/15 | 30`，
                # 里面那个 ` | ` 就是 metrics 行的分隔符，塞进去会被读成
                # 两个指标（末尾冒出一个叫「30」的东西）。而「原样透传」
                # 不允许我替它换字符 —— 所以换位置，不换值。
                cd = r.get(_COL_COUNTDOWN)
                note = ""
                if not _blank(cd):
                    # **不解析、不解释**：这一列的含义还没探过，
                    # 而 parse_date 会把 '12/15 | 30' 啃成 0001-12-15。
                    #
                    # 唯一的加工是 strip_html：probe2 实测这一列会带标签，
                    # 临近到期那批的取值是
                    #   临近到期 <span style="color:red;font-weight:bold;">!</span><br>… 最后交易
                    # 裸标签进 markdown 会显示出来、进 html 会真的生效（红色加粗），
                    # 而标签不是这个值的内容，是集思录的排版。删标签不改文本。
                    cd_text = strip_html(cd)
                    if cd_text:
                        note = f"表内「强赎天计数」原样：{cd_text}（本工具不解析它）"
                unknown_pool.append(Opportunity(
                    kind=self.kind, code=code, name=name,
                    # 不给动作词：这是陈述，不是指令。
                    action="最后交易日本次未取到 —— 本栏这次给不出时点，请自查公告",
                    action_date=None, urgency=Urgency.WATCH,
                    metrics=metrics, note=note,
                    flags=["最后交易日未取到 —— 不是「没有」，是本工具这次没拿到"],
                ))
                continue

            # ---- ① 窗口内 ---------------------------------------------------
            if within(last_d, t, win_end):
                n_window += 1
                left = trading_days_between(t, last_d, cal)
                urg = Urgency.TODAY if last_d == t else Urgency.SOON
                action = ("今日为最后交易日：收盘后停止交易，今天做卖出／转股／持有的决定"
                          if urg == Urgency.TODAY
                          else f"最后交易日 {fmt_date(last_d)}"
                               f"（剩 {left} 个交易日）：在此之前定好卖出／转股／持有")
                res.opportunities.append(Opportunity(
                    kind=self.kind, code=code, name=name,
                    action=action, action_date=last_d, urgency=urg,
                    metrics={"最后交易日": fmt_date(last_d),
                             "剩余交易日": left if left is not None else "—",
                             "到期日": fmt_date(mat_d, "%Y-%m-%d"),
                             "现价": _num(price)},
                    flags=["本栏不区分强赎与到期摘牌，两者处置方向相反 —— 动手前看公告"],
                ))
                continue

            # ---- ② 已过最后交易日 --------------------------------------------
            if show_past and past_start <= last_d < t:
                n_past += 1
                res.opportunities.append(Opportunity(
                    kind=self.kind, code=code, name=name,
                    action=f"最后交易日 {fmt_date(last_d)} 已过：核对持仓里还有没有它",
                    action_date=last_d, urgency=Urgency.WATCH,
                    metrics={"最后交易日": fmt_date(last_d),
                             "到期日": fmt_date(mat_d, "%Y-%m-%d"),
                             "现价": _num(price)},
                    flags=["已停止交易，别当成还能卖 —— 剩下按条款清算"],
                ))

        # ---- 第 ③ 档的收口 ------------------------------------------------
        # 这一档存在的理由是春23/应流那种「有行、没日期」的债不能静默消失。
        # v5.1-rc 留了个没探明的风险：这张表里到底有多少行日期是空的
        # （`diag_redeem2.py` 的 Q4）。**probe2 答了：313/319，98%**。
        # 于是原来的做法当场被证伪 —— 离线复演过一遍：按代码排序截 20 条，
        # 印出来的是代码最小那批噪音，而这一档为之设立的春23(113667)/
        # 应流(113697) 正好被截在配额外面。每天 20 条噪音 + 漏掉要救的两只，
        # 比不印更糟：它让读者以为这一档有覆盖。
        #
        # 三条口子分开处理，因为它们是三件事：
        #   · 整列都空（大表）→ 这是**取数失败**，只该说一句「这一列没了」，
        #     逐行印 319 遍「未取到」是把唯一要说的话埋掉；
        #   · 空占多数（大表）→ 「空」是这张表的常态，不含个体信息量。
        #     一条都不逐行印，改在**栏目级**把覆盖率说全：几行有日期、
        #     几行为空、空不等于「没在赎回」。这不是少说，是把一句真话
        #     换掉 20 条假装有覆盖的条目；
        #   · 空是少数派（比如 100 行里空 30 行）→ **那才是异常**，
        #     照旧截断展示，但**把总数说出来**（纪律 5：不许静默归零，
        #     可以少印，不可以少说）。
        unknown_ratio = (n_unknown / res.rows_scanned) if res.rows_scanned else 0.0
        ratio_gate = _ratio(c.get("unknown_ratio_gate", 0.5), 0.5)
        if parsed == 0 and res.rows_scanned >= 20:
            unknown_pool = []
        elif (res.rows_scanned >= 20 and n_unknown > max_unknown
                and unknown_ratio >= ratio_gate):
            unknown_pool = []
            res.notes.append(
                f"本栏 {res.rows_scanned} 行里只有 {parsed} 行带最后交易日，"
                f"其余 {n_unknown} 行该列为空 —— 空不等于「没在赎回」，"
                f"本栏对这 {n_unknown} 只给不出时点，手里有债以公告为准")
        elif len(unknown_pool) > max_unknown:
            unknown_pool.sort(key=lambda o: o.code)          # 截断要可复现
            dropped = len(unknown_pool) - max_unknown
            unknown_pool = unknown_pool[:max_unknown]
            res.notes.append(
                f"另有 {dropped} 只在表内但最后交易日未取到，未逐条列出"
                f"（本栏这一档最多列 {max_unknown} 条，共 {n_unknown} 只）")
        res.opportunities.extend(unknown_pool)

        if res.opportunities:
            res.footnotes.append(
                "「转债退出提醒」只读 `bond_cb_redeem_jsl` 的**最后交易日**一列，"
                "**不区分强赎与到期摘牌** —— 这张表把两者放在同一列里，"
                "而自然到期的最后交易日同样紧贴到期日。两者处置方向相反（强赎是限期离场，"
                "到期是拿本息），所以本栏只给时点、不下分类，"
                "到期日一并印出来供你自己看。动手前请以公告为准。")
            res.footnotes.append(
                "本栏的数全是那张表里的**原始字段**，一个都没有二次加工；"
                "「强赎天计数」原样透传，本工具不解析也不解释它的含义。")

        self._explain_zero(res, n_window, n_past, n_unknown,
                           parsed, ord_ok, ord_bad, win_end, show_past)
        return res

    @staticmethod
    def _explain_zero(res, n_window, n_past, n_unknown,
                      parsed, ord_ok, ord_bad, win_end, show_past) -> None:
        """0 条时说清是哪种 0。分寸照 cb_ipo._explain_listing_zero 那套。

        「列位移」和「整列取不到」无论本栏有没有条目都要报 —— 它们会让这一栏
        天天正常地少一块；正常空窗只在一条都没有时才说话。
        """
        # ① 列位移：先判，因为它最像「正常」。
        rel_tot = ord_ok + ord_bad
        if rel_tot >= 20 and ord_ok / rel_tot < 0.5:
            res.error = (f"列可能移位：{rel_tot} 行里只有 {ord_ok} 行满足"
                         "「到期日不早于最后交易日」，这两列大概率接错了字段")
            res.notes.append(
                "本栏**不可信** —— 跑 `python diag_redeem2.py` 看这张表现在的实际列名")
            return

        # ② 整列取不到：小表不判（自检的 5 行表里本就有一行是空的）。
        if parsed == 0 and res.rows_scanned >= 20:
            res.error = f"扫描 {res.rows_scanned} 行，没有一行有可解析的最后交易日"
            res.notes.append(
                "本栏 0 条 = 最后交易日整列取不到值，**不是**「近期没有债要退出」")
            return

        # ③ 正常空窗。这一栏的 0 条比别栏更要紧：漏掉一只 = 一只债卖不掉了，
        # 所以即便是真空窗，也要把「不在本栏 ≠ 没在赎回」这句话说全。
        if res.opportunities:
            return
        tail = "" if show_past else "（已过那一档被 show_past=false 关掉了）"
        res.notes.append(
            f"本栏 0 条 = 窗口内（至 {fmt_date(win_end)}）没有临近的最后交易日，"
            f"接口正常、{parsed} 行有日期{tail}")
        res.notes.append(
            "但这张表的收录口径未定 —— 不在本栏**不等于**没在赎回，"
            "手里的债仍以公告为准")


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip() in ("", "nan", "None", "-", "—", "NaT")


def _num(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return round(v, 2)


def _int(v, default: int) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def _ratio(v, default: float) -> float:
    """占比配置项：夹到 [0, 1]，读不出来就回落默认值。"""
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return default
