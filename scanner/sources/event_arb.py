"""数据源 4：事件套利公告。

接口：akshare.stock_zh_a_disclosure_report_cninfo(market='沪深京', keyword=...)
按关键词（要约收购 / 现金选择权 / 换股吸收合并 / 吸收合并）在巨潮做全市场滚动检索。

定位：这一层只做“线索发现”。真正的套利空间取决于对价与现价之差
（要约价 / 现金选择权价 / 换股比例），必须点开公告人工核对 —— 所以每条都带
“需人工核对条款”标签，不给出臆造的收益数字。

两个接口原始数据的坑，落库前统一清掉（实现见 utils）：
- 巨潮是**搜索**接口，命中的关键词会被 `<em>` 高亮包起来，「公告标题」和
  「简称」都可能带标签 → `strip_html`
- akshare 拼「公告链接」时把带空格的公告时间直接塞进查询串
  （`&announcementTime=2026-08-01 00:00:00`），markdown 链接语法遇空格截断
  → `clean_url` 编成 %20
"""
from __future__ import annotations

import time
from datetime import date as _date
from datetime import timedelta
from functools import partial

# 解析不出公告时间的行排在最后，而不是被当成"最新"顶到前面
_DATE_MIN = _date.min

import pandas as pd

from .base import Source
from ..models import Kind, Opportunity, SourceResult, Urgency
from ..utils import clean_url, days_ago, parse_date, retry_call, strip_html

# 巨潮检索的服务端封顶：probe6 实测**唯一数整整 3000 = 100 整页**（两路大数都是），
# 而七路小结果集重复行为 0，是完美的对照组。到这个数就说明结果被截过了。
_CNINFO_PAGE_CAP = 3000

# 关键词之间的间隔（秒）：巨潮检索接口连打同样会被掐
_INTER_CALL_GAP = 1.0

# 关键词分两组，**不同的巨潮栏目**，这是最容易踩空的地方：
# `stock_zh_a_disclosure_report_cninfo` 的 market 参数决定查哪个库
# （沪深京→szse / 基金→fund / 债券→bond …）。基金的终止上市公告在「基金」栏目里，
# 用默认的「沪深京」去搜「终止上市」，一条都搜不到，而且和「今天没有」长得一样 ——
# 和配债栏那个 0 条是同一类陷阱。
_STOCK_MARKET = "沪深京"
_FUND_MARKET = "基金"

# 基金退出类线索的动作词。原来那句「读公告核对条款（对价 vs 现价、比例、时间表）」
# 是给要约收购/吸并写的，套到「某 LOF 终止上市」上完全不通。
_ACT_STOCK = "读公告核对条款（对价 vs 现价、比例、时间表）"
_ACT_FUND = "读公告确认：退出方式（清盘/转型）、最后交易日、投资者选择期"
_FLAG_STOCK = "需人工核对：套利空间=对价与现价之差，含比例/税费/时间成本"
_FLAG_FUND = "需人工核对：退出日程与折价收敛路径，与日常折价套利不是同一笔交易"


def _mock_df(keyword: str) -> pd.DataFrame:
    """离线自检样例。

    标题里**故意保留 `<em>` 高亮标签、链接里故意留空格**，与巨潮真实返回一致，
    这样 `python run.py --mock` 就能真正验到清洗逻辑，而不是清洗坏了也看不出来。
    """
    from datetime import date as _d
    t = _d.today()
    demo = {
        "要约收购": [("600123", "示例要约", "关于<em>要约收购</em>报告书摘要的提示性公告")],
        "现金选择权": [("000456", "示例吸并", "关于本次换股吸收合并<em>现金选择权</em>实施的公告")],
        "换股吸收合并": [("000456", "示例吸并", "<em>换股吸收合并</em>暨关联交易实施进展公告")],
        "吸收合并": [],
        # 基金栏目：LOF 退出线索。动作词与上面几条不同，--mock 要能验到分支。
        "终止上市": [("161130", "示例标普LOF",
                      "关于旗下基金<em>终止上市</em>并转型为场外基金的公告")],
        "基金合同终止": [],
        "投资者选择期": [("501050", "示例价值LOF",
                          "关于<em>投资者选择期</em>安排及份额转托管的提示性公告")],
    }
    atime = (t - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [{"代码": c, "简称": n, "公告标题": title, "公告时间": atime,
             "公告链接": (f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={c}"
                          f"&announcementId=1234567890&orgId=gssh0{c}"
                          f"&announcementTime={atime}")}
            for c, n, title in demo.get(keyword, [])]
    return pd.DataFrame(rows)


def _fmt_atime(s: str) -> str:
    """公告时间展示：巨潮多数条目时分秒是 00:00:00，去掉这段噪音只留日期。"""
    s = strip_html(s)
    return s[:-9].strip() if s.endswith(" 00:00:00") else s

class EventArbSource(Source):
    kind = Kind.EVENT

    def fetch(self) -> SourceResult:
        res = SourceResult(kind=self.kind)
        c = self.cfg.get("event_arb", {})
        window = int(c.get("window_days", 7))
        keywords = list(c.get("keywords", []))
        fund_keywords = list(c.get("fund_keywords", []))
        max_items = int(c.get("max_items", 20) or 0)
        start = days_ago(window, self.ctx.today).strftime("%Y%m%d")
        end = self.ctx.today.strftime("%Y%m%d")

        frames = []
        failed_kws = []
        capped_kws = []
        groups = [(_STOCK_MARKET, "stock", keywords),
                  (_FUND_MARKET, "fund", fund_keywords)]
        call_i = 0
        for market, group, kws in groups:
            for kw in kws:
                if self.ctx.mock:
                    df = _mock_df(kw)
                else:
                    import akshare as ak
                    if call_i:
                        time.sleep(_INTER_CALL_GAP)
                    call_i += 1
                    df, err = retry_call(
                        partial(ak.stock_zh_a_disclosure_report_cninfo,
                                symbol="", market=market, keyword=kw,
                                start_date=start, end_date=end),
                        label=f"cninfo检索({market}/{kw})",
                        attempts=2, backoff=(2.0,),
                        # 关键：无命中是正常结果，不能当失败重试，
                        # 否则每个冷门关键词每天都要白打两次请求。
                        reject_empty=False,
                    )
                    if df is None:
                        # 单个关键词失败不致命，记下来继续跑其余关键词
                        res.error = (res.error or "") + f"[{kw}:{err}] "
                        failed_kws.append(kw)
                        continue
                if df is not None and not df.empty:
                    df = df.copy()
                    df["_kw"] = kw
                    df["_group"] = group
                    frames.append(df)
                    # v5.7：巨潮检索有服务端封顶，probe6 量到是 **3000 条 = 100 整页**，
                    # 而 akshare 越过封顶继续翻页、回来的是重复行（见 diag_cbplan 的
                    # `page_bound_note()` / `dup_rows()`）。封顶之上「窗口里实际有多少条」
                    # 就不知道了，**截掉的那部分有没有偏也没量过**。
                    #
                    # 这一栏结构上够不到那个封顶：它给巨潮传了 start_date/end_date
                    # （7 天窗），关键词又窄（要约收购/现金选择权/换股吸收合并/吸收合并）,
                    # 所以服务端返回集本来就在三位数以内。**但那是推理，不是保证** ——
                    # 窗口配大、关键词配宽都能把它推到封顶上，而封顶的表现形式是
                    # 「静默地少给」，正是纪律 5 的头号敌人。
                    #
                    # 所以留一道哑守卫：它**只会多说一句话，永远不会少印一条**。
                    # 平时一次都不触发（mock 和两份回放都是个位数行），
                    # 真触发了就说明这一栏已经不可信，那时候必须让读者看见。
                    if len(df) >= _CNINFO_PAGE_CAP:
                        capped_kws.append(f"{kw}({len(df)}行)")

        if failed_kws:
            res.notes.append(
                f"关键词 {'/'.join(failed_kws)} 未检索成功，这几类线索本次缺失")
        if capped_kws:
            # 措辞照 diag_cbplan 的规矩：**不贴方向**。这个数不是计数，
            # 是「30 × 翻了多少页」；底下究竟漏了多少、漏的那部分偏不偏，都没量过。
            res.notes.append(
                f"关键词 {'/'.join(capped_kws)} 的返回量已达检索接口的整页封顶"
                f"（{_CNINFO_PAGE_CAP} 条）——**这一栏本次很可能没列全**，"
                f"漏了多少、漏的那批偏不偏，本工具都没量过。把窗口或关键词收窄再跑。")

        if not frames:
            # v5.9.2：这一栏原来是六个源里**唯一**一个 0 条时什么都不说的 ——
            # 报告只印一个「无」，分不出「7 天真空窗」和「关键词全部空返回」。
            # 分寸照其余五个源：只在整栏一条都没有时才多说话。
            self._explain_zero(res, window, keywords, fund_keywords, failed_kws)
            return res

        alldf = pd.concat(frames, ignore_index=True)
        # 同一条公告可能被多个关键词同时命中，按链接去重。
        # akshare 的列名偶有变动，缺列时退化为按 (代码, 公告标题) 去重，
        # 再不行就不去重——去重失败不该让整个数据源没结果。
        for subset in (["公告链接"], ["代码", "公告标题"]):
            if all(col in alldf.columns for col in subset):
                alldf.drop_duplicates(subset=subset, inplace=True)
                break
        res.rows_scanned = len(alldf)

        cutoff = days_ago(window, self.ctx.today)

        # 先按时间倒序，再截断。上一版是按 concat 顺序（关键词分组的先后）截的，
        # 却在提示里写「只列出**最近的** N 条」—— 被砍掉的完全可能比留下的更新。
        # 这一栏本来就是线索发现，砍掉最新的那条是最不能接受的一种砍法。
        rows = []
        for _, r in alldf.iterrows():
            atime = strip_html(r.get("公告时间", ""))
            adate = parse_date(atime)          # parse_date 自己会切掉时间部分
            if adate is not None and adate < cutoff:
                continue
            rows.append((adate or _DATE_MIN, atime, r))
        rows.sort(key=lambda x: (x[0], x[1]), reverse=True)

        truncated = bool(max_items) and len(rows) > max_items
        for _, atime, r in (rows[:max_items] if max_items else rows):
            adate = parse_date(atime)
            is_fund = str(r.get("_group", "stock")) == "fund"
            res.opportunities.append(Opportunity(
                kind=self.kind,
                code=strip_html(r.get("代码", "")),
                name=strip_html(r.get("简称", "")),
                action=_ACT_FUND if is_fund else _ACT_STOCK,
                # 这一栏的日期是**已发生**的公告时间，不是将到的时点 —— 越新越该先看，
                # 也才和上面「按公告时间倒序截断」一致（详见 Opportunity.sort_key）。
                action_date=adate, urgency=Urgency.WATCH, date_desc=True,
                metrics={"类型": strip_html(r.get("_kw", "")),
                         "公告": strip_html(r.get("公告标题", "")),
                         "时间": _fmt_atime(atime)},
                flags=[_FLAG_FUND if is_fund else _FLAG_STOCK],
                link=clean_url(r.get("公告链接", "")),
            ))
        # 基金栏目是全市场检索，「终止上市」这类词在退市潮里会一天几十条。
        # 截断本身不该静默 —— 报告变长和报告骗人一样糟，但「悄悄少给你几条」更糟。
        # 判据是 `> max_items` 不是 `>= max_items`：恰好 20 条时一条都没砍，
        # 上一版照样印「命中超过 20 条」，那是白撒一句不成立的话。
        if truncated:
            res.notes.append(
                f"命中 {len(rows)} 条，按公告时间倒序只列出最近的 {max_items} 条"
                "（改 event_arb.max_items 调整）")
        if not res.opportunities:
            self._explain_zero(res, window, keywords, fund_keywords, failed_kws,
                               scanned=res.rows_scanned)
        return res

    @staticmethod
    def _explain_zero(res, window: int, keywords, fund_keywords,
                      failed_kws, scanned: int = 0) -> None:
        """0 条时说清是哪种 0。分寸照 cb_approved._explain_zero 那套。

        这一栏的 0 条有四种，报告里长得一模一样：
          ⓞ 两组关键词都没配 —— 配置写坏了
          ① 全部检索都失败 —— 取数问题（失败的那几个上面已各记一句）
          ② 检索通了、窗口内确实没有这几类公告 —— 真空窗（7 天窗口，常态）
          ③ 有命中行但全部落在窗口外 —— 巨潮的 start/end 没按预期收窄
        """
        if not keywords and not fund_keywords:
            res.notes.append(
                "本栏 0 条 = **两组关键词都没配**（event_arb.keywords / fund_keywords "
                "空了），这一栏本次什么都没问 —— 不是「没有这类公告」")
            return
        n_kw = len(keywords) + len(fund_keywords)
        if failed_kws and len(failed_kws) >= n_kw:
            res.notes.append(
                "本栏 0 条 = 公告检索**一个关键词都没成功**，不是「这几天没有这类公告」；"
                "换一天重跑，连着两天这样再当接口变了")
            return
        if scanned:
            res.notes.append(
                f"本栏 0 条 = 检索命中 {scanned} 行，但全部落在近 {window} 天窗口之外 —— "
                f"接口正常，多半是窗口参数没按预期收窄")
            return
        res.notes.append(
            f"本栏 0 条 = 近 {window} 天这几类公告确实一条都没有（接口正常）。"
            f"这一栏是线索发现，空窗是常态，不必每天都有货")
