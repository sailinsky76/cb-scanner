"""数据源 6：转债获批公告（阶段 3，v5.9）。

接口：akshare.stock_zh_a_disclosure_report_cninfo(market='沪深京', keyword=…)
     —— 和 `event_arb` 是**同一张表、同一批列**（代码 / 简称 / 公告标题 /
     公告时间 / 公告链接）。那一栏从 v4 起就在实盘用它的「代码」「简称」两列，
     列名、翻页行为、服务端封顶全都量过 → 所以这一栏**一个新探针都不需要**。
     （v5.8 曾写下「拿不到正股代码」，那是一句错话，v5.8.1 已更正。）

这一栏补的是转债生命周期最前面那一段：**批文下来了、发行公告还没出**。
`cb_ipo` / `cb_allotment` 用的 `bond_zh_cov` 是**已发行转债**的表，那一段不在里面。

──────────────────────────────────────────────────────────────────────
这一栏印什么、不印什么

印（四个字段全部有现成出处，全是那张表里的原始值或纯算术）：

    正股代码、简称   ← 检索表的「代码」「简称」列
    获批日期         ← 「公告时间」列
    距获批多少天     ← 上一列减今天，纯减法
    公告链接         ← 「公告链接」列
    发行方式         ← 公告标题里的原文字样（v5.9.1，下面单说）
    发行状态         ← 拿正股代码回配 bond_zh_cov（下面单说）

**不印**：含权量、打平溢价、任何评分或排序度量、任何方向性措辞。
含权量要的是「已获批未发行」那一段的**拟发行规模**表，akshare 里**没有这张表**
—— 这一条已经证死（§6.9），算不出来就不印，不许估、不许拟合（纪律 2）。
排序按获批日期（越新越靠前，同 `event_arb`：这一栏的日期是**已发生**的公告时间），
**不按「好坏」** —— 这一栏给的是时点，不是方向。

形态照 `cb_redeem`：**给时点和事实，不给判断**。就像强赎那栏只印「最后交易日」、
一个字的强赎判断都不下（probe2 的结论）。

──────────────────────────────────────────────────────────────────────
两条栏目级说明**写死在代码里**，不做配置项（措辞抄 `diag_cbplan.decide()`
退出码 0 那一支，§6.5 记的 B 风险）：

  · **名单是下限，不是全集** —— 巨潮检索被服务端截在 3000 条（=100 整页，
    probe6 量到底的），截掉的那部分偏没偏本工具没量过。只许说「这些确实获批了」，
    **不许说「获批的就这些」**。
  · **上了名单 ≠ 值得埋伏** —— 这一栏给的是时点，不是方向（纪律 8）。

两句都必须出现在**栏目级**（贴着条目印，而不是沉到报告末尾）：读的人会拿
「上了名单」当成「可以动手」，这个误读离条目越远越拦不住。

──────────────────────────────────────────────────────────────────────
标题分类：**顺序就是判据，反向写法排在最前面**

规则表照 `diag_cbplan.classify_stage()` 抄过来（只取这一栏用得到的两组）。
**是抄不是 import** —— 探针不进 `scanner/`（纪律 1），源不该依赖一个诊断脚本。

  ① 反向：不予注册 / 终止 / 中止 / 撤回 / 失效 …   → 一律剔掉
  ② 获批：同意注册 / 注册批复 / 予以注册 / 核准批复 → 留下

顺序反了方向就反了，而这里方向反了的代价是：**照着一只已经黄了的债去埋伏正股**。
（「不予注册」含「注册」、「注册批复到期失效」含「注册批复」。）

**先 strip_html 再匹配**。巨潮把命中的每个词各自裹一层 `<em>`，
`同意注册` 回来是 `<em>同意</em><em>注册</em>` —— 四字关键词被标签劈成两半，
`"同意注册" in s` 直接为假。而漏判是**有方向的**：反向写法（终止/中止/失效/撤回）
都是两字、裹一层照样命中，正向写法全是四字、全被劈开 → 正向漏、反向照判，
恰好是最坏的那个方向。probe3 栽的就是这一次。

再筛一道**转债字样**（可转债 / 可转换公司债券）：检索是全市场的，定增
（向特定对象发行股票）的注册批复长得几乎一样，实测获批档里六分之五是它。

──────────────────────────────────────────────────────────────────────
发行状态：为什么是「标记」而不是「剔除」

检索窗口 180 天，里面有些债这会儿早发完了，不该再算「待发」。判发没发的出处是
`bond_zh_cov`（东财转债总表，项目里已经在用），但**只能按正股代码回配** ——
而一家公司可以有转2、转3。按代码一律剔除的后果是：**同一家公司的老债**会被
当成「这次获批的债已经发行」，把一条真正待发的静默删掉。

所以两道收窄 + 一个默认：

  · 只认**申购日不早于获批日**的那一只 —— 批文之前就申购完的债，不可能是
    这一次批文说的那只。这是纯日期比较，不是推断。
  · 回配不上、或总表本次没取到 → 状态记成「未核对」，**不是**「待发」（纪律 5）。
  · 默认 `hide_issued=false`：已发行的**留在栏里并标出来**，不隐去。
    它本身是有用的事实（这条获批线索已经走完，不用再等），印出来只多一行；
    删掉却可能删掉一条判错了的。想要干净就把它设成 true —— 那时**隐了几条会照说**。

「获批满 N 天还没发行」（`stale_days`，默认 90）同理：**只标事实，不给原因**。
原文章说这类可能中止，但「可能已发而总表没收录 / 在等发行窗口 / 真的中止」
这几种本工具分不开，写「可能中止」就是拿一句推断当结论（纪律 2）。
**一条都不过滤** —— 该说的说，不许 silently 少给。

──────────────────────────────────────────────────────────────────────
v5.9.1 修的两处（都是 v5.9-rc 实盘 08-12 那份报告暴露出来的）

**① `max_items` 的截断方向和 `stale_days` 是反的。**
排序按获批日期倒序、截断留最新的 N 条 → 砍掉的永远是最老的；而
「获批满 N 天仍未发行」按定义就是最老的那一批。两个方向对着干，
stale 那一档**结构性地必被砍光**，而栏目级还写着「已逐条标出，一条都没过滤」。
实盘那份：命中 50 只、印 30 只，栏目级说「其中 1 只获批已满 90 天…一条都没
过滤」，可印出来的 30 条距获批只到 83 天 —— 那一只在被砍的 20 只里，报告里
根本找不到。而且这不是偶发，50 只/180 天 vs `max_items=30`，截断是常态。

改法：**`max_items` 只管「已发行」那一档**。「未查到发行记录/未核对」一条不砍
（它们的动作词还没走完），已发行那一档的动作词本身就是「这条线索走完了」，
让它先让位是本栏定义使然，不是在判好坏。未走完的多到超过 `max_items` 时
本栏会超长，那时**照说**，不静默截。
另外栏目级那句话现在按**印出来的**算（`shown_stale`），不按算出来的算 ——
说的和印的必须是同一件事，这是纪律 5 本身。

**② 公募转债和定向可转债混在一栏里。**
`_CB_WORDS` 只筛「有没有转债字样」，于是「向特定对象发行可转换公司债券」
和「发行可转债购买资产」也进来了。它们不面向公众：没有网上申购、没有原股东
配售、`bond_zh_cov` 里本来就不会有 —— 所以它们的「未查到发行记录」是
**结构性的**，会一直挂着攒天数，最后被 `stale_days` 标成「距获批 N 天仍未查到
发行记录」，读起来像出事了，其实是本来就不会有。实盘那 30 条里有 2 条是这一类。

改法：**标记，不剔除**，并且**不计入 stale**。判据是标题里的原文字样
（向不特定对象 / 向特定对象 / 购买资产），是字样不是推断；标题没写明的照印，
不替它猜（`_offer_kind` 返回空串那一支）。
"""
from __future__ import annotations

import time
from functools import partial

import pandas as pd

from .base import Source
from ..models import Kind, Opportunity, SourceResult, Urgency
from ..utils import (clean_url, days_ago, fmt_date, parse_date, retry_call,
                     strip_html)

# （v5.9.3 删掉了这里的 `_DATE_MIN = date.min`：它是从 event_arb 抄过来的，
#  但这一栏的排序键 `_k()` 用的是「没日期就给 0」，从来没引用过它。）

# 巨潮公告检索：这一栏走「沪深京」栏目（正股公告都在这个库里）
_MARKET = "沪深京"

# 关键词之间的间隔（秒）：巨潮检索接口连打会被掐（同 event_arb）
_INTER_CALL_GAP = 1.0

# 服务端整页封顶：probe6 实测**唯一数整整 3000 = 100 整页**。到这个数就说明被截过。
# 这一栏的四个正向写法实测都在三位数（同意注册 ~200 / 注册批复 ~240 /
# 证监会核准 0-1 / 核准批复 ~10），结构上够不到封顶 —— **但那是推理，不是保证**：
# window_days 配大就能把它推上去，而封顶的表现形式是「静默地少给」。
# 所以留一道哑守卫：它只会多说一句话，永远不会少印一条。
_CNINFO_PAGE_CAP = 3000

# ---- 标题分类：顺序就是判据，反向组必须排最前 ----------------------------
# 抄自 diag_cbplan._STAGE_RULES 的前两组（那里是探针，这里是源，不 import）。
_REVERSE_WORDS = ("不予注册", "不予核准", "未获", "终止", "中止", "撤回", "失效")
_APPROVE_WORDS = ("同意注册", "注册批复", "予以注册", "证监会核准", "核准批复")
# 全市场检索里挑出转债的那一道：其余多为定增（向特定对象发行股票）
_CB_WORDS = ("可转债", "可转换公司债券")

# ---- 公募 / 定向：**标题里的原文字样，不是推断**（v5.9.1）------------------
# 「向不特定对象发行」= 公募转债，会有网上申购和原股东配售 —— 这一栏真正指向的那一段。
# 「向特定对象发行」/「购买资产」= 定向可转债（含重组配套），**不面向公众**：
# 不会进 bond_zh_cov、不会有网上申购、不会有原股东配售。
#
# 为什么必须分：不分的代价不是多印一行。这两类的发行状态**永远**是
# 「未查到发行记录」—— 它们会一直挂在栏里攒天数，最后被 stale_days 标成
# 「距获批 N 天仍未查到发行记录」，读起来像是这只债出了事，其实是本来就不会有。
# 实盘 08-12 那份里 30 条印出来的有 2 条是这一类（盖世食品 920826「向特定对象
# 发行可转换公司债券…注册批复」、芯导科技 688230「发行可转换公司债券及支付现金
# 购买资产…同意注册」），占了「未查到发行记录」11 条里的 2 条。
#
# 处理方式是**标记，不剔除**（同 hide_issued 那一处的理由）：判据是标题里的字样，
# 不是推断，所以标得出来；但标题写法会变，剔除等于把变了写法的那些静默删掉。
# 两组顺序上互斥（「向不特定对象」里不含「向特定对象」这五个连续字），
# 仍然先判公募 —— 顺序写死，不给以后的人留搞反的机会。
_PUBLIC_OFFER_WORDS = ("向不特定对象",)
_PRIVATE_OFFER_WORDS = ("向特定对象", "购买资产")

# bond_zh_cov 里这一栏只用三列（列名照 tests_sources 那张脏表 / docs/probes/probe.txt）
_COV_BOND_CODE = "债券代码"
_COV_BOND_NAME = "债券简称"
_COV_STOCK_CODE = "正股代码"
_COV_APPLY = "申购日期"


def _title_is_approval(title) -> bool:
    """标题是不是「获批」那一步。**反向写法先判**，命中反向一律不算获批。"""
    s = strip_html(title or "")
    if any(w in s for w in _REVERSE_WORDS):
        return False
    return any(w in s for w in _APPROVE_WORDS)


def _title_is_cb(title) -> bool:
    """标题里有没有转债字样（把定增的注册批复筛出去）。"""
    s = strip_html(title or "")
    return any(w in s for w in _CB_WORDS)


def _offer_kind(title) -> str:
    """标题写的是公募还是定向：返回 '公募' / '定向' / ''（认不出就不猜）。

    只认标题里的**原文字样**。认不出返回空串，调用方既不标也不因此改判 ——
    「没写明」不等于「是公募」，也不等于「是定向」（纪律 5 的同一条道理）。
    """
    s = strip_html(title or "")
    if any(w in s for w in _PUBLIC_OFFER_WORDS):
        return "公募"
    if any(w in s for w in _PRIVATE_OFFER_WORDS):
        return "定向"
    return ""


def _norm_code(v) -> str:
    """代码归一：去标签去空白。**不补零、不改写** —— 只做匹配用的清洗。"""
    return strip_html(v).strip()


def _mock_df(keyword: str) -> pd.DataFrame:
    """离线自检样例。

    标题里**故意保留 `<em>` 高亮标签、链接里故意留空格**，与巨潮真实返回一致 ——
    这样 `python run.py --mock` 才真的走到清洗逻辑，而不是清洗坏了也看不出来
    （做法照 event_arb._mock_df）。

    七条覆盖全部分支，缺一条就有一条路径在 --mock 下走不到：
      · 601111 待发（两条公告命中，验按正股归并 + 取最早那条当获批日）
      · 600222 已发行（总表里申购日晚于获批日）
      · 300333 获批 120 天仍未查到发行记录（且该正股另有一只更早的债）
      · 600444 定增的注册批复 → 应被转债字样筛掉
      · 600555 「注册批复到期失效」→ 应被反向组筛掉（**顺序错了它就漏进来**）
      · 002666 公告时间取不到 → 不许静默消失，进「时点给不出」那一档
      · 920777 **向特定对象**发行可转债、且已满 stale_days → 该标「定向」、
        **不该**计进「获批满 N 天」那个数（v5.9.1 加；标题形状抄实盘 08-12
        那份里的盖世食品 920826）

    601111 两条的先后是刻意的：**注册批复那条排在同意注册前面**，这样
    「后来的那条更早 → 覆盖获批日」这一支才真的会被走到。v5.9-rc 的顺序
    反过来，那一支在 --mock 下从来没执行过（覆盖率量出来是漏的）。
    """
    from datetime import date as _d
    t = _d.today()

    def atime(days_back: int) -> str:
        return (t - pd.Timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")

    demo = {
        # 「注册批复」先跑：601111 先拿到 18 天前那条，再被 20 天前那条覆盖
        # —— 「取最早那条」的覆盖分支只有这个顺序才走得到
        "注册批复": [
            ("601111", "示例待发股份",
             "关于向不特定对象发行可转换公司债券<em>注册批复</em>的提示性公告", atime(18)),
            ("300333", "示例久未发股份",
             "关于收到向不特定对象发行可转债<em>注册批复</em>的进展公告", atime(120)),
            ("600444", "示例定增股份",
             "关于向特定对象发行股票<em>注册批复</em>的公告", atime(10)),
            ("600555", "示例失效股份",
             "关于可转换公司债券<em>注册批复</em>到期失效的公告", atime(30)),
            # 定向可转债：标题写「向特定对象发行可转换公司债券」，已满 90 天。
            # 应当标出「定向」，但**不该**进「获批满 N 天」那个数。
            ("920777", "示例定向股份",
             "关于向特定对象发行可转换公司债券获得<em>注册批复</em>的提示性公告", atime(150)),
        ],
        "同意注册": [
            ("601111", "示例待发股份",
             "关于<em>同意注册</em>向不特定对象发行可转换公司债券的批复的公告", atime(20)),
            ("600222", "示例已发股份",
             "关于收到<em>同意注册</em>向不特定对象发行可转换公司债券批复的公告", atime(100)),
        ],
        "证监会核准": [],
        "核准批复": [
            # 公告时间是空串：这一档必须还能出条，只是给不出时点
            ("002666", "示例无日期股份",
             "关于可转换公司债券<em>核准批复</em>的公告", ""),
        ],
    }
    rows = [{"代码": c, "简称": n, "公告标题": title, "公告时间": at,
             "公告链接": (f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={c}"
                          f"&announcementId=90{c}&orgId=gssh0{c}"
                          f"&announcementTime={at}")}
            for c, n, title, at in demo.get(keyword, [])]
    return pd.DataFrame(rows)


def _mock_cov_df() -> pd.DataFrame:
    """离线自检用的转债总表片段：列名对齐 bond_zh_cov()，只放这一栏用得到的三列。

    两行分别验两条不同的路：
      · 600222 申购日在获批日**之后** → 判「已发行」
      · 300333 申购日在获批日**之前**（老债）→ **不算**这一次已发行，
        只在条目注里说一句「该正股另有更早的转债」
    """
    from datetime import date as _d
    t = _d.today()
    return pd.DataFrame([
        {"债券代码": "113222", "债券简称": "已发转债", "正股代码": "600222",
         "申购日期": t - pd.Timedelta(days=70)},
        {"债券代码": "123333", "债券简称": "老债转债", "正股代码": "300333",
         "申购日期": t - pd.Timedelta(days=400)},
    ])


def _cov_index(df) -> dict:
    """bond_zh_cov → {正股代码: [(债券代码, 债券简称, 申购日期), …]}。

    缺列就返回空字典，让调用方把状态记成「未核对」—— 而不是当成「没发行」。
    """
    if df is None or len(df) == 0:
        return {}
    cols = {str(x) for x in df.columns}
    if _COV_STOCK_CODE not in cols or _COV_APPLY not in cols:
        return {}
    idx: dict = {}
    for _, r in df.iterrows():
        sc = _norm_code(r.get(_COV_STOCK_CODE, ""))
        if not sc:
            continue
        idx.setdefault(sc, []).append((
            _norm_code(r.get(_COV_BOND_CODE, "")),
            strip_html(r.get(_COV_BOND_NAME, "")),
            parse_date(r.get(_COV_APPLY)),
        ))
    return idx


class CBApprovedSource(Source):
    kind = Kind.CB_APPROVED

    def fetch(self) -> SourceResult:
        res = SourceResult(kind=self.kind)
        c = self.cfg.get("cb_approved", {})
        window = _int(c.get("window_days", 180), 180)
        keywords = list(c.get("keywords", []) or [])
        max_items = _int(c.get("max_items", 30), 30)
        stale_days = _int(c.get("stale_days", 90), 90)
        hide_issued = bool(c.get("hide_issued", False))

        start = days_ago(window, self.ctx.today).strftime("%Y%m%d")
        end = self.ctx.today.strftime("%Y%m%d")

        # ---- 1. 检索 ------------------------------------------------------
        frames, failed_kws, capped_kws = [], [], []
        for i, kw in enumerate(keywords):
            if self.ctx.mock:
                df = _mock_df(kw)
            else:
                import akshare as ak
                if i:
                    time.sleep(_INTER_CALL_GAP)
                df, err = retry_call(
                    partial(ak.stock_zh_a_disclosure_report_cninfo,
                            symbol="", market=_MARKET, keyword=kw,
                            start_date=start, end_date=end),
                    label=f"cninfo检索({_MARKET}/{kw})",
                    attempts=2, backoff=(2.0,),
                    # 无命中是正常结果，不能当失败重试（同 event_arb）
                    reject_empty=False,
                )
                if df is None:
                    res.error = (res.error or "") + f"[{kw}:{err}] "
                    failed_kws.append(kw)
                    continue
            if df is not None and not df.empty:
                df = df.copy()
                df["_kw"] = kw
                frames.append(df)
                if len(df) >= _CNINFO_PAGE_CAP:
                    capped_kws.append(f"{kw}({len(df)}行)")

        if failed_kws:
            res.notes.append(
                f"关键词 {'/'.join(failed_kws)} 未检索成功，这几种写法的获批公告本次缺失")
        if capped_kws:
            # 措辞不贴方向：这个数不是计数，是「30 × 翻了多少页」。
            res.notes.append(
                f"关键词 {'/'.join(capped_kws)} 已达检索接口整页封顶（{_CNINFO_PAGE_CAP} 条）"
                f"——**本栏本次很可能没列全**，漏了多少、偏不偏都没量过；把 window_days 收窄再跑")

        if not frames:
            self._explain_zero(res, window, n_appr=0, n_cb=0,
                               searched=bool(keywords) and not failed_kws,
                               keywords=keywords)
            return res

        alldf = pd.concat(frames, ignore_index=True)
        # 同一条公告会被多个关键词同时命中，按链接去重；缺列时退化（同 event_arb）
        for subset in (["公告链接"], ["代码", "公告标题"]):
            if all(col in alldf.columns for col in subset):
                alldf.drop_duplicates(subset=subset, inplace=True)
                break
        res.rows_scanned = len(alldf)

        # ---- 2. 筛：反向组先判 → 获批组 → 转债字样 -------------------------
        n_appr = 0          # 归到「获批」那一档的条数（含定增）
        picked: dict = {}   # 正股代码 -> 这只票的获批公告（取最早那条）
        for _, r in alldf.iterrows():
            title = strip_html(r.get("公告标题", ""))
            if not _title_is_approval(title):
                continue
            n_appr += 1
            if not _title_is_cb(title):
                continue
            code = _norm_code(r.get("代码", ""))
            link = clean_url(r.get("公告链接", ""))
            # v5.9.2：归并的 key 和印出来的代码**分开**。
            # 原来写 `code = _norm_code(...) or "未取到"`，于是所有取不到代码的行
            # 共用同一个 key —— 不同公司会被并成一条，只留第一家的简称、取所有家
            # 里最早的那个日期，而注里那句「窗口内另有 N 条同类公告」会被读成
            # 「同一家公司发了 N 条」。实测三家公司 → 出 1 条，两家静默消失。
            # 代码取不到时改用公告链接（再退化到标题）当 key：印出来的仍是
            # 「未取到」，但每一条都还在。
            key = code or ("未取到::" + (link or title))
            atime = strip_html(r.get("公告时间", ""))
            adate = parse_date(atime)       # parse_date 自己会切掉时间部分
            cur = picked.get(key)
            if cur is None:
                picked[key] = {
                    "key": key,
                    "code": code or "未取到",
                    "name": strip_html(r.get("简称", "")) or "简称未取到",
                    "date": adate, "title": title,
                    "link": link,
                    "n": 1,
                }
                continue
            # 同一正股在窗口里有多条获批类公告（同意注册 + 注册批复很常见）：
            # **取最早那条当获批日**，另有几条照说 —— 不合并成一个数（纪律 5）。
            cur["n"] += 1
            old = cur["date"]
            if adate is not None and (old is None or adate < old):
                cur.update(date=adate, title=title, link=link)
        n_cb = len(picked)

        if not picked:
            self._explain_zero(res, window, n_appr=n_appr, n_cb=0, searched=True,
                               keywords=keywords)
            return res

        # ---- 3. 发行状态：拿正股代码回配 bond_zh_cov ------------------------
        cov_idx, cov_err = self._cov()

        t = self.ctx.today
        n_pending = n_issued = n_stale = n_private = 0
        hidden = 0
        # 两档分开装（v5.9.1，理由见下面第 4 步）：
        #   items_open —— 未查到发行记录 / 未核对，**线索还没走完**
        #   items_done —— 已发行，动作词本身就是「这条线索走完了」
        # 元素是 (key, Opportunity)：stale 的对账要按 key 走，不能按 code ——
        # 代码取不到时 code 都是「未取到」，按 code 求交集会把多条算成一条。
        items_open, items_done = [], []
        stale_keys = set()      # 计入「获批满 N 天」的那些，用来核对说的和印的是否一致
        for it in picked.values():
            adate = it["date"]
            days = (t - adate).days if adate else None
            offer = _offer_kind(it["title"])
            if offer == "定向":
                n_private += 1

            status, note, flags = "未核对", "", []
            # v5.9.3：**代码取不到时一次都别查**。
            # 上一版直接拿 `it["code"]` 去查，而代码缺失时它是字符串「未取到」——
            # `cov_idx.get("未取到")` 必然是空，于是走进「未查到发行记录」那一支：
            # 计进 n_pending、够老的话再挂一条「距获批 N 天仍未查到发行记录」。
            # 我们**一次都没查**，却印成了「查过、没找到」。这和 §"回配不上 → 记
            # 未核对，不是待发" 是同一条纪律，只是漏了「压根没法回配」这种情形，
            # 而它恰恰是 v5.9.2 新放行的那一类条目（空代码不再被并条，于是它们
            # 第一次真正走到这里）。空代码时状态照实说，也不参与 stale 计数。
            has_code = bool(it["code"] and it["code"] != "未取到")
            if cov_err is None and not has_code:
                status = "未核对（正股代码未取到，没法回配总表）"
                flags.append("正股代码本次未取到 —— 发没发行本栏这次核对不了")
            elif cov_err is None:
                bonds = cov_idx.get(it["code"], [])
                # 只认「申购日不早于获批日」的那一只 —— 批文之前就申购完的债
                # 不可能是这一次批文说的那只。
                after = [b for b in bonds
                         if b[2] is not None and adate is not None and b[2] >= adate]
                if after:
                    bcode, bname, bd = sorted(after, key=lambda x: x[2])[0]
                    status = f"已发行（{bcode}，申购日{fmt_date(bd)}）"
                    n_issued += 1
                elif adate is None:
                    status = "未核对（获批日期取不到，没法比申购日）"
                else:
                    status = "未查到发行记录"
                    n_pending += 1
                    if bonds:
                        note = (f"该正股另有 {len(bonds)} 只更早的转债"
                                f"（申购日早于本次获批日）—— 不是这一次的")
                    # 定向的**不计入** stale：它的「未查到发行记录」是结构性的，
                    # 不是「批文下来这么久还没动静」。混进来会让这个数越攒越大，
                    # 而这个数本来是要你回头看一眼的信号。
                    if days is not None and days >= stale_days and offer != "定向":
                        n_stale += 1
                        stale_keys.add(it["key"])
                        flags.append(f"距获批 {days} 天仍未查到发行记录 —— 原因本工具不判断")

            if offer == "定向":
                # 单句 ≤60 字（verify_report ⑤ 的预算），细节归 footnotes
                flags.append("标题写的是「向特定对象/购买资产」，总表里本来就不会有它")

            if hide_issued and status.startswith("已发行"):
                hidden += 1
                continue

            if adate is None:
                action = "公告时间未取到 —— 本条给不出获批时点，只有标题和链接"
                flags.append("获批时点本次未取到 —— 不是「没有」，是这次没拿到")
            elif status.startswith("已发行"):
                action = "已获批**且已发行** —— 这条线索走完了，申购/上市看打新那一栏"
            elif status.startswith("未查到"):
                action = f"获批公告 {fmt_date(adate)}（距今 {days} 天）：转债总表里还没有它"
            else:
                action = f"获批公告 {fmt_date(adate)}（距今 {days} 天）：发行状态本次没核对上"

            metrics = {"获批日期": fmt_date(adate, "%Y-%m-%d"),
                       "距获批": f"{days}天" if days is not None else "—",
                       "发行方式": offer or "标题未写明",
                       "发行状态": status,
                       "公告": it["title"]}
            if it["n"] > 1:
                note = ((note + "；") if note else "") + \
                    f"窗口内另有 {it['n'] - 1} 条同类公告，获批日期取最早那条"
            (items_done if status.startswith("已发行") else items_open).append(
                (it["key"], Opportunity(
                    kind=self.kind, code=it["code"], name=it["name"],
                    action=action, action_date=adate, urgency=Urgency.WATCH,
                    # 这一栏的日期是**已发生**的公告时间，越新越该先看（同 event_arb）
                    date_desc=True, metrics=metrics, flags=flags,
                    link=it["link"], note=note,
                )))

        # ---- 4. 排序与截断：**截断只砍「已发行」那一档**（v5.9.1）------------
        # v5.9-rc 是「全栏按获批日期倒序、留最新的 max_items 条」，而那是错的 ——
        # 不是取值错，是**两个方向对着干**：排序留最新的，砍掉的就永远是最老的；
        # 而「获批满 stale_days 天仍未发行」按定义就是最老的那一批。于是
        # stale 那一档结构性地必被砍光，而栏目级还写着「已逐条标出，一条都没过滤」。
        #
        # 实盘 08-12 就撞上了：命中 50 只、印 30 只，说「其中 1 只获批已满 90 天…
        # 一条都没过滤」，而印出来的 30 条距获批只到 83 天 —— 那一只在被砍的 20 只里，
        # 报告里根本找不到。说的和印的相反，正是纪律 5 要拦的那种。
        # 而且这不是偶发：50 只/180 天 vs max_items=30，只要市场维持这个量，截断就是常态。
        #
        # 改法：max_items 变成**「已发行」那一档的配额**，「未查到发行记录/未核对」
        # 一条不砍。这不是在判好坏 —— 已发行那一档的动作词本身就是「这条线索走完了」，
        # 让它先让位是本栏定义使然。代价是未发行的多到超过 max_items 时本栏会超长，
        # 那时**照说**（下面倒数第二条 note），不静默截。
        def _k(pair):
            key, o = pair
            return (-(o.action_date.toordinal()) if o.action_date else 0, o.code, key)

        items_open.sort(key=_k)
        items_done.sort(key=_k)
        n_total = len(items_open) + len(items_done)
        cut_done = 0
        if max_items and n_total > max_items:
            keep_done = max(0, max_items - len(items_open))
            cut_done = len(items_done) - keep_done
            items_done = items_done[:keep_done]
        kept = sorted(items_open + items_done, key=_k)
        res.opportunities = [o for _, o in kept]
        shown_stale = len(stale_keys & {k for k, _ in kept})

        # ---- 5. 栏目级说明 ------------------------------------------------
        # 这两句**写死**，一个配置项都不给（§6.5 记的 B 风险）。
        res.notes.append(
            "本栏名单是**下限，不是全集** —— 巨潮检索被服务端截在 3000 条，"
            "截掉的部分偏没偏没量过；只能说「这些确实获批了」，不能说「获批的就这些」")
        res.notes.append(
            "**上了名单 ≠ 值得埋伏** —— 这一栏给的是时点，不是方向（纪律 8）")
        res.notes.append(
            f"近 {window} 天获批类公告 {n_appr} 条，其中转债 {n_cb} 只："
            f"未查到发行记录 {n_pending} 只、已发行 {n_issued} 只、"
            f"状态未核对 {n_cb - n_pending - n_issued} 只")
        if n_private:
            res.notes.append(
                f"这 {n_cb} 只里有 {n_private} 只标题写的是**向特定对象发行/购买资产** —— "
                f"定向可转债，没有网上申购、没有原股东配售，转债总表里本来就不会有它；"
                f"已逐条标出，且**不计入**下面「获批满 {stale_days} 天」那个数")
        if n_stale:
            # 这句话必须按**印出来的**算，不能按算出来的算 —— v5.9-rc 就是在这里
            # 说了「一条都没过滤」，而那一条被 max_items 砍掉了。
            if shown_stale == n_stale:
                res.notes.append(
                    f"其中 {n_stale} 只获批已满 {stale_days} 天仍未查到发行记录 —— "
                    f"已逐条列出（这一档不参与截断）；"
                    f"原因（已发未收录/等窗口/中止）本栏不判断")
            else:
                # 现在的截断规则下走不到这里（stale 必在 items_open，不参与截断）。
                # 留着是**兜底**：以后谁改了截断口径，报告会自己说出来，而不是继续
                # 印那句「一条都没过滤」。
                res.notes.append(
                    f"其中 {n_stale} 只获批已满 {stale_days} 天仍未查到发行记录，"
                    f"本栏只印出 {shown_stale} 只、**另有 {n_stale - shown_stale} 只没印出来** "
                    f"—— 把 cb_approved.max_items 设 0 才看得全")
        if hidden:
            res.notes.append(
                f"已发行的 {hidden} 只按 cb_approved.hide_issued=true 隐去了 —— "
                f"改成 false 会连同申购日一起印出来")
        if cov_err:
            res.notes.append(
                f"发行状态本次**没核对上**（{cov_err}）—— 栏内每一只都是「未核对」，"
                f"不是「还没发行」")
        if cut_done:
            res.notes.append(
                f"命中 {n_total} 只，其中**已发行的 {cut_done} 只未列出**"
                f"（cb_approved.max_items={max_items}，按获批日期倒序砍最老的）；"
                f"「未查到发行记录/未核对」的 {len(items_open)} 只**一条都没砍**")
        if max_items and len(res.opportunities) > max_items:
            res.notes.append(
                f"本栏列了 {len(res.opportunities)} 只、超过 max_items={max_items}："
                f"未走完的有 {len(items_open)} 只，这一档不参与截断，宁可长也不静默少给")

        res.footnotes.append(
            "「转债获批公告」只印那张检索表里的原始字段（代码/简称/公告时间/链接）"
            "加一个减法（距获批天数）。**不印含权量、打平溢价、任何评分** ——"
            "「已获批未发行」那一段的拟发行规模表 akshare 里没有，算不出来就不印。")
        res.footnotes.append(
            "「发行方式」取自公告标题原文：**向不特定对象**发行 = 公募转债，"
            "会有网上申购与原股东配售；**向特定对象**发行 / 发行可转债购买资产 = "
            "定向可转债，不面向公众，转债总表里本来就不会有它，所以它的「未查到发行记录」"
            "是结构性的、不计入「获批满 N 天」。标题没写明的照印，本栏不替它猜。")
        res.footnotes.append(
            f"本栏的 max_items 只管「已发行」那一档 —— 排序是获批日期倒序，"
            f"截断砍的永远是最老的，而「获批满 {stale_days} 天仍未发行」按定义就是最老的那批，"
            f"两个方向对着干。所以未走完的那一档不参与截断（v5.9.1 修）。")
        res.footnotes.append(
            "获批日期 = 检索窗口内**最早**那条获批类公告的公告时间；"
            "批文早于窗口起点时这个日期会偏晚，跟着「距获批天数」一起偏小。")
        res.footnotes.append(
            "发行状态取自 `bond_zh_cov`（东财转债总表），按**正股代码**回配，"
            "且只认申购日不早于获批日的那一只 —— 同一家公司更早的转债不算这一次。"
            "回配不上只说明这张表里还没有它，不等于「一定没发」。")
        return res

    # ------------------------------------------------------------------
    def _cov(self) -> tuple:
        """取转债总表用于判发没发。返回 (索引, 错误描述或 None)。

        cache_key 和 `cb_ipo` / `cb_allotment` 用的是**同一个** ——
        同一次运行里这张表只拉一次，这一栏白搭一趟车。
        """
        if self.ctx.mock:
            return _cov_index(_mock_cov_df()), None
        import akshare as ak
        df, err = retry_call(ak.bond_zh_cov, attempts=2, backoff=(2.0,),
                             cache_key=f"bond_zh_cov::{self.ctx.today}",
                             ttl_seconds=3600)
        if df is None:
            return {}, f"bond_zh_cov 拉取失败：{err}"
        idx = _cov_index(df)
        if not idx:
            return {}, f"总表里没有「{_COV_STOCK_CODE}」或「{_COV_APPLY}」列"
        return idx, None

    @staticmethod
    def _explain_zero(res, window: int, n_appr: int, n_cb: int,
                      searched: bool, keywords=()) -> None:
        """0 条时说清是哪种 0 —— 这一栏尤其要紧：三种 0 长得一模一样。

        分寸照 `cb_redeem._explain_zero` / `cb_ipo._explain_listing_zero`。
        """
        # ⓞ 一个关键词都没配：配置写坏了会长得和「今天没有」一模一样，说清是哪种
        if not keywords:
            res.notes.append(
                "本栏 0 条 = **一个检索关键词都没配**（cb_approved.keywords 空了），"
                "这一栏本次什么都没问 —— 不是「没有债获批」")
            return
        # ① 一次都没检索成功：这是取数问题，最像「没有」的那一种
        if not searched:
            res.notes.append(
                "本栏 0 条 = 公告检索**没成功**，不是「这段时间没有债获批」；"
                "换一天重跑，连着两天这样再当接口变了")
            return
        # ② 检索通了，但获批那一档一条都没有 —— 多半是写法又变了
        if n_appr == 0:
            res.notes.append(
                f"本栏 0 条 = 近 {window} 天一条获批类公告都没命中。"
                f"注册制后批文叫「同意注册」不叫「核准」，写法若再变这里会静默变空")
            res.notes.append(
                "确认写法：跑 `py -3.11 diag_cbplan.py`（它把每种写法各命中多少打出来）")
            return
        # ③ 获批公告有，但没有一条是转债的（其余多为定增）—— 这才是真空窗
        res.notes.append(
            f"本栏 0 条 = 近 {window} 天获批类公告 {n_appr} 条，"
            f"但标题含转债字样的 {n_cb} 条（其余多为定增），接口正常")
        res.notes.append(
            f"这一栏是 {window} 天的累计量，不是「今天的机会」—— 落到近几天为 0 属正常")


def _int(v, default: int) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default
