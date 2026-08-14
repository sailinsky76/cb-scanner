"""数据源 3：LOF/QDII 折溢价（v4）。

—— v4 为什么又改：探针推翻了 v3 的核心假设 ——
v3 说「LOF 折价改走 `fund_etf_fund_daily_em()`，它覆盖全部场内基金」。
`diag_lof_coverage.py` 实测（2026-08-08，akshare 1.18.78）证明这句话是错的：

    fund_etf_fund_daily_em  1602 行  →  LOF 段 0 个
    「类型」列取值只有 指数型-股票/海外股票/固收/其他
    相对 ETF 帧只多出 59 个代码（159/511/551/561/520 段），其中 LOF 段 0 个

**不是「LOF 的净值列为空」，是这个接口根本不收录 LOF。** 于是 v3 的 LOF 覆盖门
天天在报「有效样本里没有任何 LOF」，而修法一直找错方向 —— 一直在给一口枯井
换绳子。

—— v4 的 LOF 路：市价与净值分开取，各找各的供应商 ——
折溢价要凑齐**市价**和**净值**两块。东财在受限网络下 push2 系列整片不通
（`fund_lof_spot_em` 走 88.push2，实测 ConnectionError，且它返回列里既无 IOPV
也无折价率，即使通了也算不出来）。所以两块分别换供应商：

    市价 ← 新浪  fund_etf_category_sina("LOF基金")   382 行 / 可用 350 / LOF 段 345
    净值 ← 同花顺 fund_etf_category_ths("LOF")        449 行 / LOF 段 443

端到端试算（2026-08-07 数据）：交集 344 只，中位数 -0.59%，p5 -3.31%、p95 +1.69%
—— 境内 LOF 常态折溢价本就很小，中位数贴着 0 说明两块的口径对得上。

—— 净值列选哪一个：有实测依据，不是拍脑袋 ——
同花顺给三列。实测（可解析率 / 与新浪市价的交集 / 中位数）：

    最新-单位净值    99.3%   344 只   -0.59%   ← 采用
    当前-单位净值    90.6%   308 只   -0.63%   ← 逐行兜底
    前一日-单位净值  98.0%   342 只   +0.35%   ← 不用

「最新」和「当前」在两者都有值的行上给出**完全相同**的折价（折价前 6 名数值
逐位一致），只是「最新」少 39 个空洞，所以取「最新」、逐行回落到「当前」。

「前一日」被排除的理由不是覆盖率，而是**口径错位**：它把 T-1 净值配 T 日市价，
凭空吃进一天的净值漂移 —— p95 从 +1.69% 膨胀到 +5.33%，溢价≥3% 的条数从 13 条
虚增到 35 条。危险的是它的中位数只有 +0.35%，看起来「挺正常」，靠中位数体检
**抓不出来**。所以这一列宁可不用，也不做静默兜底。

—— v4 新增的一道门：折价能不能兑现 ——
同花顺顺带给「赎回状态」和「申购状态」，这是前几版完全没有的信息，而它恰好是
折价套利的**前置条件**：赎回暂停时，折价只是「买得便宜」，不是可兑现的价差。
实测 449 只里 42 只赎回暂停，而且**折价榜前列基本都是这一类**（定开/封闭式
产品在封闭期内天然折价，506008 科创板长城 -9.06% 就是申赎双暂停）。

按幅度排序会让 max_discount=10 的配额被这些不可兑现的品种占满 —— 这不是「排序
不好看」，是把不能做的东西摆在第一位。所以折价侧改成**先按可赎回分组、组内再
按幅度**排序，赎回暂停的照常列出但动作词改写，不再说「折价买入」。溢价侧同理
用「申购状态」（溢价套利靠申购）。

「限制大额」对本项目的资金体量不构成约束，按可用处理，只打提示不降权。

—— 保留 v3 的三道健康门与符号对账 ——
`fund_etf_fund_daily_em` 不再承担 LOF，但没有删：它对同一批**境内 ETF** 给出与
IOPV 完全独立的第二套口径，是符号正负约定的白给对账数据（符号印反比取不到数
危险得多），另外补 59 个 ETF 帧没有的代码。它的角色在 `_SPECS` 里已按实测改名，
避免下一个人再照着「LOF 折价靠这一路」的旧注释去修错地方。
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

import pandas as pd

from .base import Source
from ..models import Kind, Opportunity, SourceResult, Urgency
from ..utils import (fmt_date, load_trade_calendar, parse_date, retry_call,
                     safe_call, to_float, trading_days_between)

log = logging.getLogger("cb_scanner")

# 两张全市场大表之间的间隔（秒）。东财 push2 对连打很敏感，ETF 刚拉完立刻拉下一张
# 容易被直接掐连接（RemoteDisconnected）。
_INTER_CALL_GAP = 1.5

# 跨境标的识别（用于打标签，不用于过滤）
_CROSS_RE = re.compile(
    r"纳指|纳斯达克|标普|道琼斯|美国|中概|恒生|恒指|港股|H股|"
    r"德国|法国|英国|欧洲|日经|日本|韩国|中韩|东南亚|印度|越南|沙特|中东|"
    r"亚太|全球|国际|海外|新兴市场|发达市场|MSCI|原油|石油|黄金|大宗|REIT"
)

# 沪市 513 段是跨境 ETF 专用段位；名称正则漏掉的（如「中韩半导体」）靠它兜底
_CROSS_PREFIX = ("513",)


def _is_cross(code: str, name: str) -> bool:
    return str(code).startswith(_CROSS_PREFIX) or bool(_CROSS_RE.search(name or ""))


# 场内形态：按代码前缀判 ETF / LOF（东财表里没有这个字段）
#
# 「169」是 v4 补的。新浪 LOF 列表可用市价 350 个，按旧前缀表只认出 345 个 ——
# 差的 5 个全是 169 段（169201 浙商鼎盈LOF、169106 东方红创优定开、
# 169105 东方红睿华LOF），是货真价实的深市 LOF。漏掉它们不会算错数，但会让
# 「LOF 段有多少只」这个覆盖门的读数偏低，也会让报告里的「形态」标错成 ETF。
_LOF_PREFIX = ("160", "161", "162", "163", "164", "165", "166", "167", "168",
               "169", "501", "502", "505", "506", "150")


def _shape(code: str) -> str:
    return "LOF" if str(code).startswith(_LOF_PREFIX) else "ETF"


def _shape_label(code: str, name: str) -> str:
    """展示用形态标记：`LOF` / `ETF·跨境`。

    「跨境」原来是一整句行内提示，每天一字不变、十几条各印一遍。压成标记 +
    一条脚注：信息不丢，但不再占行内提示那 3 句的预算 —— 一只深市跨境 LOF
    完全可能同时命中 滑点 / 流动性 / 退市线，再加一句就撞破预算了。
    """
    return _shape(code) + ("·跨境" if _is_cross(code, name) else "")


# ---- 复用两次以上的脚注文本（挂在多个分支上，别让两处文案漂开）----
_FN_OFF_BOOK = (
    "「按收盘盘口推 / 最新价落在盘口之外」= 成交与盘口不是同一时点，收盘后跑的"
    "典型形态（收盘价来自集合竞价、买卖盘是盘中最后一档）。这类条目的滑点与净收益"
    "是推的不是当场能验的，开盘后拿实时盘口重跑一次才作数 —— 08-08 实测两只票"
    "口径不同差出 77 元和 85 元，足以改名次")

_FN_ON_FLOOR = (
    "「场内规模(万)」= 深交所日频**场内份额 × 单位净值**。两个必须知道的限制："
    "① 只覆盖深市 160-169 段，沪市 501/502/505/506 这个接口不提供 —— 某条**没有**"
    "这一列只说明没数，不说明它安全；② 是单日快照，不是退市规则说的连续 60 个交易日")

_FN_DELIST = (
    "「场内规模 < 退市线」依据 2026-08-07 沪深交易所《完善上市开放式基金相关安排"
    "（征求意见稿）》：连续 60 个交易日场内资产净值低于 1000 万元的 LOF 应当终止上市，"
    "小规模这一类**不设过渡期**。是否真的触线以基金公告为准。另：退市对折价持有人是"
    "强制收敛（清盘按净值结算 / 转型有投资者选择期），方向上未必是坏事，但那是一笔"
    "要扛数周净值波动的交易，和本栏隔夜口径的净收益不能混着算")

_FN_GATE_UNKNOWN = (
    "「赎回/申购状态未取到」= 申赎状态只有 LOF 那一路（同花顺）带，ETF 两路的接口"
    "根本不返回。**没取到既不等于开放，也不等于暂停** —— 这类条目的可执行性未经核对，"
    "所以不给它算净收益、也不计入「可兑现」那一档；要做之前自己去基金公司或行情软件"
    "确认一次申赎状态")


def _is_lof(code: str) -> bool:
    return str(code).startswith(_LOF_PREFIX)


def _norm_code(v) -> str:
    """统一成 6 位数字代码；非法返回空串。

    新浪那路的代码可能带市场前缀（'sz160719'），同花顺那路是纯数字，
    不归一化两边 join 不上 —— 而 join 不上的表现是「交集 0」，
    看起来跟接口挂了一模一样，是最难查的一类故障。
    """
    s = str(v).strip().split(".")[0]
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6) if 0 < len(s) <= 6 else ""


# ============================ 数据源规格 ============================
# 顺序有意义 —— 同一代码以先出现的为准，不重复出条。
# 说明列写的是**实测**职责，不是接口文档上的宣传语：v3 就是照着
# 「fund_etf_fund_daily_em 覆盖全部场内基金」这句话把 LOF 折价挂上去的，
# 而它实际一条 LOF 都不给。
_SPECS = (
    ("ETF行情", "fund_etf_spot_em",
     "IOPV 实时估值 → 跨境/ETF 溢价这一侧"),
    ("场内日行情", "fund_etf_fund_daily_em",
     "已公布净值口径的第二套折价率 → 补 59 个 ETF 代码 + 给符号对账当基准；"
     "实测 LOF 段 0 个，**不承担 LOF**"),
)

# ---- LOF 路：市价 ----------------------------------------------------------
# 新浪与东财是完全不同的供应商，东财 push2 整片不通时它没有理由跟着挂。
_LOF_PRICE_SINA = "fund_etf_category_sina"
_LOF_PRICE_SINA_SYMBOL = "LOF基金"

# 兜底：直接打东财 clist。实测 push2delay 这个 host 对 LOF 板块码是通的
# （total=390），而 akshare 原生 fund_lof_spot_em 走的 88.push2 不通 ——
# 所以「东财不通」要精确到 host，不能整片放弃。
_LOF_CLIST_HOSTS = ("push2delay.eastmoney.com", "2.push2.eastmoney.com",
                    "push2.eastmoney.com")
_LOF_CLIST_PAGE = 100
_LOF_CLIST_MAX_PAGES = 6            # 390 只 → 4 页够；留两页余量，但必须有上限
_LOF_CLIST_PARAMS = {               # 板块码与字段原样照抄 akshare
    "pn": "1", "pz": str(_LOF_CLIST_PAGE), "po": "1", "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2", "invt": "2", "wbp2u": "|0|0|0|web", "fid": "f3",
    "fs": "b:MK0404,b:MK0405,b:MK0406,b:MK0407",
    "fields": "f2,f5,f6,f12,f14",     # f6=成交额，兜底时也要能判流动性
}

# ---- LOF 路：净值 ----------------------------------------------------------
_LOF_NAV_THS = "fund_etf_category_ths"
_LOF_NAV_THS_SYMBOL = "LOF"
# 同日口径，按实测覆盖率排序；逐行取第一个有值的
_LOF_NAV_COLS = ("最新-单位净值", "当前-单位净值")
# 「前一日-单位净值」刻意不在兜底链里，理由见模块 docstring：它是 T-1 净值配
# T 日市价，中位数看起来正常（+0.35%）但 p95 从 +1.69% 膨胀到 +5.33%，
# 靠下面那道中位数体检**抓不出来**。宁可少 3 只覆盖，也不混口径。
_LOF_NAV_DATE_COL = "最新-交易日"

# 合成帧的列名（挑过的：要能被下面的 _pick_col 正确认出，且不误撞其它 picker）
_C_CODE, _C_NAME, _C_PX = "代码", "名称", "最新价"
_C_NAV = "参考净值"          # 会被 col_iopv 的 picker 认出 → 走「自算」分支
_C_BASIS = "净值口径"        # 逐行的口径显示名，覆盖默认的「IOPV自算」
_C_DATE = "数据日期"         # 会被 _frame_data_date 认出 → 陈旧检测免费获得
_C_REDEEM, _C_SUB = "赎回状态", "申购状态"
_C_AMT, _C_BID, _C_ASK = "成交额", "买入", "卖出"

# 申赎状态取值（实测分布：赎回 开放402/暂停42/空5；申购另有「限制大额」33）
_GATE_OPEN = "开放"
_GATE_SUSPENDED = "暂停"
_GATE_CAPPED = "限制大额"    # 对本项目的资金体量不构成约束

# 场内日行情的净值列名带日期前缀（如「2026-08-07-单位净值」），
# 没有独立的「数据日期」列，只能从列名里抠。
_COL_DATE_RE = re.compile(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})")


@dataclass
class _Frame:
    """一路数据源的产出。

    比裸 DataFrame 多带三样东西，都是为了让下游别再靠猜：
    `basis_col` 让合成帧逐行声明自己的口径（否则会被标成「IOPV自算」——
    LOF 那路实际用的是已公布净值，标错口径等于把结论的时效性说得比实际好）；
    `gates` 声明这一帧带不带申赎状态；`role` 决定覆盖门与提示语的措辞。
    """
    label: str
    df: pd.DataFrame
    basis_col: Optional[str] = None
    gates: bool = False
    role: str = "ETF"
    diag: dict = field(default_factory=dict)


def _pick_col(df: pd.DataFrame, *keywords: str):
    """返回第一个列名包含任一关键词的列；找不到返回 None。

    接口列名各版本会飘（'基金折价率' / '折价率'、'IOPV实时估值' / 'IOPV'、
    '最新价' / '市价'），这里做模糊匹配，避免一次改版就整栏归零。
    """
    for kw in keywords:
        for col in df.columns:
            if kw in str(col):
                return col
    return None


def _frame_data_date(df: pd.DataFrame):
    """取整帧的数据日期：先找独立日期列，找不到就从列名里解析。"""
    col_dt = _pick_col(df, "数据日期", "日期")
    if col_dt is not None:
        try:
            d = parse_date(df[col_dt].dropna().max())
            if d:
                return d
        except Exception:
            pass
    for col in df.columns:                      # 「2026-08-07-单位净值」
        m = _COL_DATE_RE.search(str(col))
        if m:
            d = parse_date(m.group(1))
            if d:
                return d
    return None


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ============================ LOF 路：取市价 ============================
def _lof_prices_sina(retries: int, today) -> tuple:
    """新浪 LOF 列表 → ({code: {...}}, 错误描述)。

    单行结构：{"name", "px", "amt"(成交额,元), "bid", "ask"}。

    v4.1 起多取三列。核过 akshare 1.18.78 源码，这个接口返回的是完整行情快照
    （代码/名称/最新价/涨跌额/涨跌幅/买入/卖出/昨收/今开/最高/最低/成交量/成交额），
    上一版只挑了三列，等于把免费的流动性和买卖价差扔了 —— 而对 LOF 折价来说，
    「这只票一天成交多少」恰恰决定了纸面折价能不能真买到。
    """
    import akshare as ak
    fn = getattr(ak, _LOF_PRICE_SINA, None)
    if fn is None:
        return {}, f"akshare 无 {_LOF_PRICE_SINA} 接口（建议 pip install -U akshare）"

    df, err = retry_call(
        partial(fn, symbol=_LOF_PRICE_SINA_SYMBOL),
        label=f"{_LOF_PRICE_SINA}({_LOF_PRICE_SINA_SYMBOL})",
        attempts=retries, backoff=(2.0, 5.0),
        cache_key=f"{_LOF_PRICE_SINA}::{_LOF_PRICE_SINA_SYMBOL}::{today}",
        ttl_seconds=900, reject_empty=True,
    )
    if df is None:
        return {}, err or "空表"

    cc, cn, cp = (_pick_col(df, "代码"), _pick_col(df, "名称"),
                  _pick_col(df, "最新价", "现价", "市价"))
    if not (cc and cp):
        return {}, f"关键列缺失（列：{list(df.columns)[:8]}…）"
    ca, cb, cs = (_pick_col(df, "成交额"), _pick_col(df, "买入"),
                  _pick_col(df, "卖出"))

    out = {}
    for _, r in df.iterrows():
        code, px = _norm_code(r.get(cc)), to_float(r.get(cp))
        if code and px and px > 0:
            out[code] = {
                "name": str(r.get(cn) or "").strip() if cn else "",
                "px": px,
                "amt": to_float(r.get(ca)) if ca else None,
                "bid": to_float(r.get(cb)) if cb else None,
                "ask": to_float(r.get(cs)) if cs else None,
            }
    return out, (None if out else "取到表但一个可用市价都没有")


def _lof_prices_clist() -> tuple:
    """兜底：直接打东财 clist（换 host + 翻页）→ ({code: {...}}, 错误描述)。

    只在新浪整条挂掉时才走。刻意不做重试：实测这类失败是 host 级的确定性不通
    （连上约 6 秒被掐 RemoteDisconnected），重试多少次都一样，纯粹白等。

    这一路拿不到买卖盘口（clist 不给），所以 bid/ask 是 None，下游的价差提示
    会自动噤声 —— 宁可少一句提示，也不拿另一个字段凑数。
    """
    try:
        import requests
    except ImportError:
        return {}, "requests 不可用"

    headers = {"User-Agent": "Mozilla/5.0",
               "Referer": "https://quote.eastmoney.com/"}
    errs = []
    for host in _LOF_CLIST_HOSTS:
        out, alive = {}, False
        try:
            for page in range(1, _LOF_CLIST_MAX_PAGES + 1):
                params = dict(_LOF_CLIST_PARAMS, pn=str(page))
                r = requests.get(f"https://{host}/api/qt/clist/get",
                                 params=params, headers=headers, timeout=15)
                data = (r.json() or {}).get("data") or {}
                diff = data.get("diff") or []
                if not diff:
                    break
                alive = True
                for d in diff:
                    code, px = _norm_code(d.get("f12")), to_float(d.get("f2"))
                    if code and px and px > 0:
                        out.setdefault(code, {
                            "name": str(d.get("f14") or ""), "px": px,
                            "amt": to_float(d.get("f6")),   # f6 = 成交额
                            "bid": None, "ask": None,
                        })
                if len(diff) < _LOF_CLIST_PAGE:
                    break
        except Exception as e:
            errs.append(f"{host}: {type(e).__name__}")
            continue
        if alive and out:
            log.info("LOF 市价走 clist 兜底命中：%s，%d 个代码", host, len(out))
            return out, None
        errs.append(f"{host}: 无数据")
    return {}, "；".join(errs)


# ============================ LOF 路：取净值 ============================
def _lof_navs_ths(retries: int, today) -> tuple:
    """同花顺 LOF 列表 → ({code: {...}}, 净值列命中计数, 错误描述)。

    单行结构：{"nav": float, "basis": 用的哪一列, "redeem": .., "sub": .., "date": ..}
    """
    import akshare as ak
    fn = getattr(ak, _LOF_NAV_THS, None)
    if fn is None:
        return {}, {}, f"akshare 无 {_LOF_NAV_THS} 接口（建议 pip install -U akshare）"

    df, err = retry_call(
        partial(fn, symbol=_LOF_NAV_THS_SYMBOL),
        label=f"{_LOF_NAV_THS}({_LOF_NAV_THS_SYMBOL})",
        attempts=retries, backoff=(2.0, 5.0),
        cache_key=f"{_LOF_NAV_THS}::{_LOF_NAV_THS_SYMBOL}::{today}",
        ttl_seconds=900, reject_empty=True,
    )
    if df is None:
        return {}, {}, err or "空表"

    cc = _pick_col(df, "基金代码", "代码")
    if cc is None:
        return {}, {}, f"找不到代码列（列：{list(df.columns)[:8]}…）"

    # 按偏好顺序取**实际存在**的净值列。一列都不在 → 接口改版，明确报错。
    # 刻意不去模糊匹配「任何含『净值』的列」：那样很可能悄悄抓到累计净值或
    # 前一日净值，算出来的折价看着像模像样，实则口径已经错了。
    navcols = [c for c in _LOF_NAV_COLS if c in df.columns]
    if not navcols:
        return {}, {}, (f"净值列全部缺失，期望 {list(_LOF_NAV_COLS)}，"
                        f"实际含『净值』的列：{[c for c in df.columns if '净值' in str(c)]}")

    out, hits = {}, {c: 0 for c in navcols}
    for _, r in df.iterrows():
        code = _norm_code(r.get(cc))
        if not code:
            continue
        nav = basis = None
        for c in navcols:                       # 逐行回落：最新 → 当前
            v = to_float(r.get(c))
            if v and v > 0:
                nav, basis = v, c
                hits[c] += 1
                break
        if nav is None:
            continue
        out[code] = {
            "nav": nav, "basis": basis,
            "redeem": str(r.get(_C_REDEEM, "") or "").strip(),
            "sub": str(r.get(_C_SUB, "") or "").strip(),
            "date": str(r.get(_LOF_NAV_DATE_COL, "") or "").strip(),
        }
    return out, hits, (None if out else "取到表但一个可用净值都没有")


# ============================ 场内规模（退市线） ============================
# 2026-08-07 沪深交易所《关于完善上市开放式基金相关安排的通知（征求意见稿）》：
# 连续 60 个交易日**场内资产净值**低于 1000 万元的 LOF 应当终止上市
# （商品期货 LOF / QDII LOF 另有过渡期，最晚 2027-12-31）。
#
# 这条线正好卡在本工具的选股逻辑上：场内盘子小 → 没人做套利 → 折价长期不收敛
# → 被扫出来。所以有必要把「这只票还能存在多久」摆到折价率旁边。
#
# 关键是**别用日成交额去猜**。成交额和场内资产净值是两回事：一只场内 3000 万的
# LOF 完全可能一天只成交 5 万。拿成交额去标「退市线候选」＝ 凭空造一个判断，
# 和这个项目一路在修的「印一个看着权威其实没依据的数」是同一个毛病。
# 深交所有现成的日频份额数据，用它算：场内资产净值 = 场内份额 × 单位净值。
_SZSE_SHARE_COL = "基金份额"


def _lof_szse_shares(retries: int, today) -> tuple:
    """深交所 LOF 日频份额 → {code: 份额}。只覆盖深市（160-169 段）。

    沪市 LOF（501/502/505/506）这个接口没有，所以是**半边覆盖** —— 缺的那半边
    必须说出来，不能让读者以为「没标记 = 安全」。
    """
    import akshare as ak
    fn = getattr(ak, "fund_scale_daily_szse", None)
    if fn is None:
        # 老版本 akshare 没有这个接口。退市线提示消失，但折价数值一个都不受影响 ——
        # 所以这里只报一句，不能把整条 LOF 路拖下水。
        return {}, "akshare 无 fund_scale_daily_szse 接口（建议 pip install -U akshare）"
    # 份额数据按交易日发布，往前多要几天，取每只票最新的一条
    start = (today - _dt.timedelta(days=10)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    df, err = retry_call(
        partial(fn, start_date=start, end_date=end, symbol="LOF"),
        label="fund_scale_daily_szse(LOF)",
        attempts=max(1, retries - 1), backoff=(2.0,),
        cache_key=f"szse_lof_share::{today}", ttl_seconds=6 * 3600,
    )
    if df is None or df.empty:
        return {}, f"深交所 LOF 份额未取到：{err}"
    if _SZSE_SHARE_COL not in df.columns or "基金代码" not in df.columns:
        return {}, f"深交所 LOF 份额列名改版（列：{list(df.columns)[:6]}…）"
    out = {}
    for _, r in df.iterrows():                 # 表按日期升序，后写的覆盖前写的 = 最新
        code = _norm_code(r.get("基金代码"))
        sh = to_float(r.get(_SZSE_SHARE_COL))
        if code and sh and sh > 0:
            out[code] = sh
    return out, (None if out else "深交所 LOF 份额表取到了但一条都解析不出来")



def _build_lof_frame(prices: dict, navs: dict, price_label: str) -> _Frame:
    """市价 × 净值 → 合成帧。join 不上的直接丢，不做任何猜测性填补。"""
    rows, prems = [], []
    for code, q in prices.items():
        rec = navs.get(code)
        if not rec:
            continue
        nav, px = rec["nav"], q["px"]
        rows.append({
            _C_CODE: code,
            _C_NAME: q.get("name") or "",
            _C_PX: px,
            _C_NAV: nav,
            _C_BASIS: f"{price_label}市价/同花顺{rec['basis']}",
            _C_DATE: rec["date"],
            _C_REDEEM: rec["redeem"],
            _C_SUB: rec["sub"],
            _C_AMT: q.get("amt"),
            _C_BID: q.get("bid"),
            _C_ASK: q.get("ask"),
        })
        prems.append((px / nav - 1) * 100)

    df = pd.DataFrame(rows, columns=[_C_CODE, _C_NAME, _C_PX, _C_NAV,
                                     _C_BASIS, _C_DATE, _C_REDEEM, _C_SUB,
                                     _C_AMT, _C_BID, _C_ASK])
    return _Frame(label="LOF折溢价", df=df, basis_col=_C_BASIS, gates=True,
                  role="LOF",
                  diag={"n": len(rows), "median": _median(prems),
                        "price_n": len(prices), "nav_n": len(navs)})


# ============================ mock ============================
def _mock_spot_df() -> pd.DataFrame:
    """列名对齐 fund_etf_spot_em()。"""
    return pd.DataFrame([
        {"代码": "513100", "名称": "纳指ETF国泰", "最新价": 2.2660, "IOPV实时估值": 1.9882},
        {"代码": "159941", "名称": "纳指ETF广发", "最新价": 1.6870, "IOPV实时估值": 1.4890},
        {"代码": "510300", "名称": "沪深300ETF", "最新价": 3.9000, "IOPV实时估值": 3.8980},
    ])


def _mock_daily_df() -> pd.DataFrame:
    """列名对齐 fund_etf_fund_daily_em()：净值列带日期前缀，折价率正值=折价。

    刻意混进两种脏数据，让 --mock 能真正验到清洗逻辑：
    - 513100 与 spot 帧重复 → 应被去重，不重复出条
    - 161234 的折价率是 '---'（跨境当日净值缺失的典型写法）→ 应被跳过

    注意：真实的这张表里 **LOF 段一条都没有**（实测 1602 行 / LOF 0 个）。
    mock 里保留 161130 / 501050 两条 LOF 是为了继续验「接口折价率」那条代码
    路径本身，不代表线上还指望这一路出 LOF。
    """
    return pd.DataFrame([
        {"基金代码": "161130", "基金简称": "示例标普LOF", "类型": "指数型-海外股票",
         "2026-08-07-单位净值": 1.2590, "2026-08-06-单位净值": 1.2500,
         "市价": 1.2200, "折价率": "3.10%"},
        {"基金代码": "501050", "基金简称": "示例价值LOF", "类型": "指数型-股票",
         "2026-08-07-单位净值": 2.0000, "2026-08-06-单位净值": 1.9900,
         "市价": 2.1200, "折价率": "-6.00%"},
        {"基金代码": "513100", "基金简称": "纳指ETF国泰", "类型": "指数型-海外股票",
         "2026-08-07-单位净值": 1.9882, "2026-08-06-单位净值": 1.9800,
         "市价": 2.2660, "折价率": "-13.97%"},
        {"基金代码": "161234", "基金简称": "示例跨境LOF", "类型": "指数型-海外股票",
         "2026-08-07-单位净值": "---", "2026-08-06-单位净值": 1.1000,
         "市价": 1.3000, "折价率": "---"},
    ])


def _mock_lof_shares() -> dict:
    """mock 份额（份）。160721 刻意压到 1000 万线以下，用来验退市线那条提示。

    注意口径：场内资产净值 = 份额 × **单位净值**，不是份额 × 市价。
    折价的票市价低于净值，用市价算会把规模算小 —— 正好在退市线附近把
    「安全」算成「触线」。这几只 mock 的净值都是 1.0。
    """
    return {
        "160719": 5.0e7,      # × 净值 1.0 → 场内 5,000 万，线上
        "160720": 3.0e7,      # → 3,000 万，线上
        "160721": 6.0e6,      # → 600 万，**线下**
        "160722": 2.0e7,      # → 2,000 万，线上
        "162412": 4.0e7,      # → 4,000 万，线上
        # 501065 / 501050 是沪市段，深交所这张表本来就没有 → 验「未覆盖」文案
    }


def _mock_lof_prices() -> dict:
    """新浪 LOF 市价的形状。刻意放一只同花顺没有的，验 join 失败要被丢掉。"""
    def q(name, px, amt, bid=None, ask=None):
        return {"name": name, "px": px, "amt": amt, "bid": bid, "ask": ask}

    return {
        # 折价 8%，但申赎双暂停 → 排在最后
        "501065": q("示例定开成长", 0.920, 5_000_000),
        # 折价 4%，赎回开放、成交活跃 → 扣费后仍为正，应排第一
        "160719": q("示例开放LOF", 0.960, 8_000_000, 0.959, 0.961),
        # 折价 2%，赎回开放但扣掉赎回费就不剩什么 → 排在 160719 之后、暂停之前
        "160720": q("示例薄折价LOF", 0.980, 6_000_000),
        # 折价 3%，但一天只成交 12 万、买卖价差 1% → 纸面折价买不到。
        # 关键回归：它幅度比 160720 大，绝对收益却更小（仓位只放得下 0.6 万），
        # 排序必须把 160720 放在它前面 —— 这就是「按百分比排会骗人」那条。
        "160721": q("示例冷门LOF", 0.970, 120_000, 0.965, 0.975),
        # 折价 2%，但卖一比最新价高 1.6% → 按卖一买入折价只剩 0.4%，扣费后为负。
        # 对应实盘的 501217：纸面 2.34%、价差 2.02%，扣完是亏的。
        "160722": q("示例宽价差LOF", 0.980, 500_000, 0.975, 0.996),
        # 溢价 8%，申购限制大额（对小资金无约束）
        "162412": q("示例限额LOF", 1.080, 3_000_000),
        # 同花顺没有它 → join 不上，应被丢弃
        "164999": q("示例无净值LOF", 1.000, 1_000_000),
    }


def _mock_lof_navs() -> dict:
    return {
        "501065": {"nav": 1.0, "basis": "最新-单位净值", "redeem": _GATE_SUSPENDED,
                   "sub": _GATE_SUSPENDED, "date": "2026-08-07"},
        "160719": {"nav": 1.0, "basis": "最新-单位净值", "redeem": _GATE_OPEN,
                   "sub": _GATE_OPEN, "date": "2026-08-07"},
        "160720": {"nav": 1.0, "basis": "最新-单位净值", "redeem": _GATE_OPEN,
                   "sub": _GATE_OPEN, "date": "2026-08-07"},
        "160721": {"nav": 1.0, "basis": "最新-单位净值", "redeem": _GATE_OPEN,
                   "sub": _GATE_OPEN, "date": "2026-08-07"},
        "160722": {"nav": 1.0, "basis": "最新-单位净值", "redeem": _GATE_OPEN,
                   "sub": _GATE_OPEN, "date": "2026-08-07"},
        "162412": {"nav": 1.0, "basis": "最新-单位净值", "redeem": _GATE_OPEN,
                   "sub": _GATE_CAPPED, "date": "2026-08-07"},
    }


# ============================ 单行折溢价 ============================
def _row_premium(r, col_px, col_iopv, col_disc, basis_col=None):
    """算单行的溢价率（正=溢价），返回 (prem, 口径, 市价)；算不出返回 (None, None, px)。

    优先自算净值，绕开各家折价率的正负口径；没有净值才退回接口给的折价率。
    `basis_col` 让合成帧逐行声明真实口径 —— LOF 那路用的是**已公布净值**，
    统一标成「IOPV自算」会把结论的时效性说得比实际好。
    """
    px = to_float(r.get(col_px))
    iopv = to_float(r.get(col_iopv)) if col_iopv else None

    if px and iopv and iopv > 0:
        basis = str(r.get(basis_col) or "").strip() if basis_col else ""
        return (px / iopv - 1) * 100, (basis or "IOPV自算"), px
    if col_disc is not None:
        d = to_float(r.get(col_disc))
        if d is None:
            return None, None, px
        # 列名里带「折价」→ 正值代表折价，取反统一成「正=溢价」
        return (-d if "折价" in str(col_disc) else d), f"接口{col_disc}", px
    return None, None, px


# ============================ 折价 → 元 ============================
@dataclass
class _Est:
    """一条折价机会的可执行估算。百分比会骗人，这里换算成元。"""
    real_disc: float          # 按卖一买入真正能吃到的折价(%)
    slip: float               # 最新价 → 卖一 吃掉的折价百分点
    net: float                # 再扣赎回费与佣金后(%)
    size: float               # 可投金额(元)
    profit: float             # 预估收益(元)
    liq_cap: Optional[float]  # 流动性上限(元)；无成交额时 None
    by_liq: bool              # 是被流动性卡住的，还是被本金卡住的


def _estimate(px, nav, ask, amt, *, fee_pct, comm_pct,
              part_pct, capital, max_pos_pct) -> Optional[_Est]:
    """把「折价 3.65%」换算成「你能放 1.1 万、大概拿回 81 元」。

    这一层是 v4.2 加的，起因是 08-08 实盘数据把上一版的排序整个推翻了：
    9 条可赎回折价按幅度排是 161131 第一（3.65%），按元排它掉到第 6（81 元），
    而幅度只有 2.27% 的 506005 因为一天成交 1575 万，能放满仓位，反而排第二。
    **9 个名次全变**。原因是百分比里藏着两个它自己不体现的量：

      - 买入价不是最新价。161131 的最新价 1.001 恰好等于**买一**（最后一笔成交
        砸在买盘上），而卖一是 1.015。你要建仓就得吃卖一，3.65% 的折价里
        有 1.35 个百分点是纸面上的。
      - 能放多少钱由成交额决定。501217 折价 2.34% 看着能做，一天只成交 1.9 万，
        按 5% 成交额算就是 950 元 —— 就算折价是真的，绝对收益也只有个位数元。

    两处刻意悲观，别"顺手优化"回去：

    1) 卖一低于最新价时**不采信**，回落最新价。这种倒挂只可能来自陈旧或交叉的
       快照，采信它等于凭空给自己一个更好的价格。这个口子只准往悲观方向开。
    2) 即便用了卖一，这个数**仍然偏乐观** —— 卖一只是盘口第一档，真按 5% 成交额
       去吃单会打穿好几档。它比按最新价算靠谱，但不是能兑现的承诺。
    """
    if not (nav and nav > 0 and px and px > 0):
        return None
    buy = ask if (ask and ask >= px) else px      # 见上：只准往贵了取
    real = (nav - buy) / nav * 100
    slip = (buy - px) / nav * 100
    net = real - fee_pct - comm_pct

    cash_cap = capital * max_pos_pct / 100
    liq_cap = amt * part_pct / 100 if amt else None
    size = cash_cap if liq_cap is None else min(cash_cap, liq_cap)
    return _Est(real_disc=real, slip=slip, net=net, size=size,
                profit=size * net / 100, liq_cap=liq_cap,
                by_liq=liq_cap is not None and liq_cap < cash_cap)


def _sign_verdict(pairs):
    """交叉校验两套口径的正负约定，返回 (是否疑似反号, 样本数, 同号残差, 反号残差)。

    原理：同一只基金若两套口径方向一致，`mean(|a-b|)` 应该明显小于 `mean(|a+b|)`；
    反过来就说明我们对某一边的符号约定理解反了。比阈值法稳，因为它不依赖
    「多大算显著」这种拍脑袋的数。

    只喂境内标的：跨境的 IOPV 与已公布净值本来就不是同一时点的净值，
    方向天然可以不一致，拿它们校验会一直误报。
    """
    if len(pairs) < 30:                          # 样本太少，不下结论
        return None, len(pairs), None, None
    same = sum(abs(a - b) for a, b in pairs) / len(pairs)
    flip = sum(abs(a + b) for a, b in pairs) / len(pairs)
    return flip < same, len(pairs), same, flip


class FundPremiumSource(Source):
    kind = Kind.FUND_PREM

    # ------------------------------------------------------------------ 取数
    def _load_frames(self, retries: int = 3):
        """返回 [_Frame, ...]；全失败返回空列表。

        失败的那一路会同时记进 `self._load_errs`（给健康面板）和 `self._missing`
        （给栏目级提示）——后者是关键：只有知道缺的是 LOF 还是 ETF、是市价还是
        净值，才能说清「折价 0 条」到底是没机会还是没扫。
        """
        if self.ctx.mock:
            self._shares = _mock_lof_shares()
            return [
                _Frame("ETF行情", _mock_spot_df()),
                _Frame("场内日行情", _mock_daily_df()),
                _build_lof_frame(_mock_lof_prices(), _mock_lof_navs(), "新浪"),
            ]

        import akshare as ak
        frames = []
        # 接口名写死成字符串，不从 `fn.__name__` 取：缓存 key 依赖运行期属性太脆，
        # 一旦接口被装饰器包过（__name__ 变成 wrapper 之类），两路就会共用同一个
        # key，第二路会直接读回第一路的缓存 —— 静默串数据比报错难查得多。
        for idx, (label, api, _why) in enumerate(_SPECS):
            fn = getattr(ak, api, None)
            if fn is None:
                self._load_errs.append(f"{label}: akshare 无 {api} 接口（建议 pip install -U akshare）")
                self._missing.append(label)
                continue

            if idx:                      # 不是第一路 → 先喘口气再打下一张全市场表
                time.sleep(_INTER_CALL_GAP)

            df, err = retry_call(
                fn,
                label=f"{api}({label})",             # 日志里不再是 <lambda>
                attempts=retries,
                backoff=(2.0, 5.0),                  # 指数退避，别贴着限流窗口重打
                cache_key=f"{api}::{self.ctx.today}",
                ttl_seconds=900,
                reject_empty=True,                   # 全市场表为空 = 异常，不是「今天没数据」
            )
            if df is None:
                self._load_errs.append(f"{label}: {err or '空表'}")
                self._missing.append(label)
                continue
            frames.append(_Frame(label, df))

        lof = self._load_lof_frame(retries)
        if lof is not None:
            frames.append(lof)

        # 场内份额：只用于退市线判断，取不到不影响任何数值，所以放在最后、
        # 失败只记一笔，不进 _missing（它不是「折价算不算得出来」的必要条件）。
        time.sleep(_INTER_CALL_GAP)
        self._shares, sherr = _lof_szse_shares(retries, self.ctx.today)
        if sherr:
            self._share_err = sherr
        return frames

    def _load_lof_frame(self, retries: int) -> Optional[_Frame]:
        """LOF 路：市价与净值分别取，缺任何一块都出不了这一帧。

        两块**分开报错**。合成一句「LOF 取数失败」是省事，但下次排查又要从头
        分辨是市价那边挂了还是净值那边挂了 —— 这次的探针整整花了一轮就在
        分辨这件事，不该让下一轮重来一遍。
        """
        time.sleep(_INTER_CALL_GAP)

        prices, perr = _lof_prices_sina(retries, self.ctx.today)
        price_label = "新浪"
        if not prices:
            log.info("LOF 市价：新浪失败（%s），转 clist 兜底", perr)
            prices, perr2 = _lof_prices_clist()
            price_label = "东财clist"
            if not prices:
                self._load_errs.append(f"LOF市价: 新浪({perr})；clist({perr2})")
                self._missing.append("LOF市价")

        # 市价已经挂了也照样打净值那一路：多花约 1 秒，换来的是一次运行就能看清
        # **两侧各自**的死活。上一轮为了分清「是市价挂了还是净值挂了」写了整个探针，
        # 没必要让下一次再来一遍。
        navs, hits, nerr = _lof_navs_ths(retries, self.ctx.today)
        if not navs:
            self._load_errs.append(f"LOF净值: {nerr}")
            self._missing.append("LOF净值")

        if not prices or not navs:
            self._missing.append("LOF折溢价")
            return None

        if hits:
            log.debug("LOF 净值列命中：%s", hits)
        return _build_lof_frame(prices, navs, price_label)

    # ------------------------------------------------------------------ 主流程
    def fetch(self) -> SourceResult:
        res = SourceResult(kind=self.kind)
        c = self.cfg.get("fund_premium", {})
        prem_th = float(c.get("premium_alert_pct", 3.0))
        disc_th = float(c.get("discount_alert_pct", 2.0))
        only_cross = bool(c.get("only_cross_border", False))
        max_prem_n = int(c.get("max_premium", c.get("max_items", 15)) or 0)
        max_disc_n = int(c.get("max_discount", c.get("max_items", 15)) or 0)
        stale_days = int(c.get("stale_days", 3))
        retries = int(c.get("fetch_retries", 3))
        sanity_pct = float(c.get("sanity_median_pct", 3.0))
        only_redeemable = bool(c.get("only_redeemable", False))
        fee_pct = float(c.get("redeem_fee_pct", 1.5))
        comm_pct = float(c.get("commission_pct", 0.03))
        min_turnover = float(c.get("min_turnover_wan", 100))
        spread_th = float(c.get("spread_alert_pct", 0.5))
        part_pct = float(c.get("turnover_participation_pct", 5))
        max_pos_pct = float(c.get("max_position_pct", 30))
        min_profit = float(c.get("min_profit_yuan", 0) or 0)
        # v5.9.2：这两道门原来**静默**过滤。min_profit 那道有 `dropped` 并在栏目级
        # 说出来，这两道没有 —— 同一条纪律，三个开关两种待遇。默认都是 false，
        # 所以这次只是把话补上，一个数都没变。
        n_skip_cross = 0                 # 被 only_cross_border 略去的境内标的
        n_skip_blocked = 0               # 被 only_redeemable 略去的申赎暂停品种
        demote_unknown = bool(c.get("demote_unknown_gate", True))
        delist_line = float(c.get("delist_line_wan", 1000))
        capital = float(self.ctx.capital)
        dropped = 0                      # 被「按元的门槛」滤掉的条数
        self._load_errs = []
        self._missing = []
        self._shares = {}
        self._share_err = None

        # 脚注收集器：**只在真被触发时**才登记，且全报告去重。
        # 用 dict 而不是 set，是为了保持加入顺序 —— 脚注的次序应该跟着正文里
        # 第一次出现的次序走，否则读者顺着标记找下去是乱的。
        _seen_fn: dict = {}

        class _Fn:
            @staticmethod
            def add(text: str) -> None:
                _seen_fn.setdefault(text, None)

        _fn = _Fn()

        cal = None if self.ctx.mock else load_trade_calendar()

        frames = self._load_frames(retries=retries)
        if not frames:
            res.error = "行情接口全部失败：" + "；".join(self._load_errs or ["未知"])
            res.notes.append("本栏 0 条 = 完全没取到数，与「今天没机会」无关")
            return res

        accepted = {}      # code -> (溢价率, 口径)；跨帧去重，先到先得
        xcheck = []        # 两套口径都覆盖到的境内标的，用于校验正负约定
        prem_pool, disc_pool = [], []   # 两侧分开攒：溢价条数多，会把折价挤没
        for fr in frames:
            label, df = fr.label, fr.df
            col_code = _pick_col(df, "代码")
            col_name = _pick_col(df, "名称", "简称")
            col_px = _pick_col(df, "最新价", "现价", "市价")
            col_iopv = _pick_col(df, "IOPV", "实时估值", "参考净值")
            col_disc = _pick_col(df, "折价率", "溢价率")
            if not (col_code and col_px):
                self._load_errs.append(f"{label}: 关键列缺失（{list(df.columns)[:6]}…）")
                self._missing.append(label)
                continue
            if col_iopv is None and col_disc is None:
                # 既没有估值列也没有折价率列 —— 这帧无论多少行都算不出折溢价。
                # v2 就是栽在这里：fund_lof_spot_em 只给行情不给估值，
                # 整帧静默贡献 0 条，而总数被 ETF 帧喂饱，健康面板照报 OK。
                self._load_errs.append(
                    f"{label}: 无估值列也无折价率列，算不出折溢价（列：{list(df.columns)[:8]}…）")
                self._missing.append(label)
                continue

            # 数据日期：整帧取一次，用于陈旧检测（周一早上还挂着上周五的价差最危险）
            data_dt = _frame_data_date(df)
            lag = None
            if data_dt:
                # 交易日差，不是日历天差：周一看周五的数据日历差 3 天、交易日差 1 天，
                # 按日历天算会在每个周一误报「数据陈旧」。
                lag = trading_days_between(data_dt, self.ctx.today, cal)

            # 两个计数分开，不能混：
            # - computable = 这一帧有多少行能算出折溢价 → 帧级健康门用它
            # - 新增代码数 → 累计到 valid，只是「合并后有多少标的」
            # 混用会误判：日行情帧的 ETF 行几乎全和 ETF 帧重复、被去重跳过，
            # 一个完全健康但高度冗余的帧会被判成「0 有效样本」。
            frame_computable = 0
            frame_emitted = 0            # 这一帧真出了几条 —— 帧级提示只在出条时才印
            for _, r in df.iterrows():
                code = _norm_code(r.get(col_code))
                if not code:
                    continue
                name = str(r.get(col_name, "")).strip() if col_name else ""
                prem, basis, px = _row_premium(r, col_px, col_iopv, col_disc,
                                               fr.basis_col)
                if prem is None:
                    continue
                frame_computable += 1

                # 重复代码不重复出条 —— 但两套口径都算得出的样本别浪费，
                # 留下来给下面的符号交叉校验当对账数据。
                if code in accepted:
                    prev_prem, prev_basis = accepted[code]
                    if basis != prev_basis and not _is_cross(code, name):
                        xcheck.append((prev_prem, prem))
                    continue

                accepted[code] = (prem, basis)
                is_cross = _is_cross(code, name)
                if only_cross and not is_cross:
                    # v5.9.3：**只数达到阈值的那些**。
                    # v5.9.2 给这道门补了话，但计数点在阈值判断**之前** ——
                    # 数的是「所有算得出折溢价的境内标的」，不是「本该列出、被略去
                    # 的条数」。mock 下印 8 而实际只少了 7；上了实盘（1600+ ETF 行）
                    # 这个数会是四位数，而真正被略掉的可能只有十来条。
                    # 边上的 n_skip_blocked / dropped 都在阈值之后数 ——
                    # 同一条纪律，三个开关不该是两种口径。
                    if prem >= prem_th or prem <= -disc_th:
                        n_skip_cross += 1
                    continue

                # ---- 申赎闸门：折价靠赎回兑现，溢价靠申购兑现 ----------------
                # 三态，不是两态。ETF 那两路（fr.gates=False）根本不返回申赎状态，
                # 上一版把「取不到」和「开放」合并成同一档，于是报告会说
                # 「溢价侧 37 条…27 条可兑现」——那 27 条全是跨境 ETF，申购状态
                # 一条都没取到。而溢价 24% 的 QDII 恰恰大概率是限额或暂停
                # （不限额溢价撑不到 24%），等于在最显眼的一行断言反面。
                # 「没取到」必须自成一档：它既不是开放，也不是暂停。
                redeem = str(r.get(_C_REDEEM, "") or "").strip() if fr.gates else ""
                subs = str(r.get(_C_SUB, "") or "").strip() if fr.gates else ""

                # 盘口与成交额提前取出：v4.1 里它们只用来打两句提示，v4.2 起
                # 直接参与折价的计算（买入价取卖一、仓位受成交额约束），
                # 所以必须在下面的分支之前就位。
                amt = to_float(r.get(_C_AMT)) if fr.gates else None
                bid = to_float(r.get(_C_BID)) if fr.gates else None
                ask = to_float(r.get(_C_ASK)) if fr.gates else None
                nav = to_float(r.get(col_iopv)) if col_iopv else None

                thin = False          # 「净收益不够本」只对折价侧有意义
                profit = None         # 绝对收益(元)，折价侧的排序键
                est = None            # 折价侧的估算；溢价侧恒为 None
                if prem >= prem_th:
                    known = bool(subs)                    # 溢价靠申购兑现
                    blocked = subs == _GATE_SUSPENDED
                    if blocked:
                        action = f"溢价 {prem:.2f}% → 规避（申购暂停，溢价无法通过申购兑现）"
                    elif not known:
                        action = f"溢价 {prem:.2f}% → 规避（申购状态未取到，能否申购套利待核）"
                    else:
                        # v4.6.1：原来写「规避 / 若申赎开放可申购套利」。走到这个分支
                        # 时申购状态**已经取到了**，metrics 里就明写着「申购: 开放」，
                        # 再说「若申赎开放」是把已知条件退回成假设 —— 和折价侧被改掉的
                        # 「（需可赎回兑现）」是同一个毛病，那次漏了溢价这一侧。
                        # 分档也是按这个状态数的（它进的是「可兑现」那一档），
                        # 动作词跟计数说的必须是同一件事。
                        action = f"溢价 {prem:.2f}% → 规避（申购{subs}，可申购套利）"
                    # 这两句原来挂在**每一条**溢价上（56 字 × 十几条）。它们和这只
                    # 基金今天的数字没有任何关系 —— 换一天跑、换一只票，一字不变。
                    # 归到脚注，全报告印一次。
                    flags = []
                    _fn.add("溢价侧：高溢价随时因恢复申购、风险提示、临时停牌而单日塌缩；"
                            "要套利须先确认一级申赎开放与限额（跨境受 QDII 外汇额度约束）")
                    metrics = {"形态": _shape_label(code, name), "最新价": _num(px),
                               "溢价率(%)": round(prem, 2), "口径": basis}
                elif prem <= -disc_th:
                    known = bool(redeem)                  # 折价靠赎回兑现
                    blocked = redeem == _GATE_SUSPENDED
                    if blocked:
                        action = f"折价 {-prem:.2f}% → 赎回暂停，折价无法通过赎回兑现（仅观察）"
                    elif not known:
                        # 上一版这里写「折价买入（需可赎回兑现）」。括号里那句是**要求**，
                        # 不是**结论** —— 读者会当成"已经确认可赎回"。没取到就说没取到。
                        action = f"折价 {-prem:.2f}% → 赎回状态未取到，能不能兑现待核"
                    else:
                        action = f"折价 {-prem:.2f}% → 折价买入（需可赎回兑现）"
                    # 原来这里挂一句「折价兑现依赖赎回通道、赎回费与到账周期」，
                    # 每条折价都有。可它说的三件事已经各有出处：赎回通道在 metrics
                    # 的「赎回」列、费率在脚注、到账周期从来就没算进任何数。
                    # 留着只是让每条多 19 个字。
                    flags = []
                    metrics = {"形态": _shape_label(code, name), "最新价": _num(px),
                               "折价率(%)": round(-prem, 2), "口径": basis}
                    # ---- 这笔折价到底能拿回多少元 ------------------------------
                    # 路径是「场内买入 → 申请赎回 → 按当日净值结算」。中间有三道
                    # 减法，缺一道都会把折价说得比实际大：
                    #   ① 买入价是**卖一**不是最新价（滑点）
                    #   ② 持有不足 7 日的赎回费，监管下限就是 1.5%
                    #   ③ 能放多少钱受日成交额约束 —— 决定这 % 值几个钱
                    # 光印一个「折价 2.27%」，读的人会把它当收益。
                    #
                    # 赎回暂停的**不算**：那条路根本走不通，印一个「净收益 6.47%」
                    # 等于凭空造出一笔拿不到的收益，比不印更糟。
                    #
                    # 赎回状态**没取到**的也不算。理由和赎回暂停一样：印一个
                    # 「净收益 1.98% / 预估 594 元」等于替读者认定这条路走得通，
                    # 而我们并不知道。少一个数，比多一个没依据的数好。
                    est = (_estimate(px, nav, ask, amt, fee_pct=fee_pct,
                                     comm_pct=comm_pct, part_pct=part_pct,
                                     capital=capital, max_pos_pct=max_pos_pct)
                           if fr.role == "LOF" and known and not blocked else None)
                    if est is not None:
                        metrics["净收益(%)"] = round(est.net, 2)
                        thin = est.net <= 0
                        # 净收益为负时**不印**仓位和金额。上一版在赎回暂停那条印过
                        # 「扣费后 6.47%」，这是同一个毛病换个地方犯：「可投 2.5 万 /
                        # 预估 -282 元」既像在给一笔不该做的交易配仓位，那个 -282
                        # 也是虚构的（你根本不会去填这 2.5 万）。留净收益一个数
                        # 说明它为什么是死的，就够了。
                        if not thin:
                            # 三个数必须自己乘得通：印出来的 可投 × 净收益 = 预估。
                            # 拿未取整的中间值算预估，会印出「3.0 万 × 1.98% = 595」
                            # 这种读者一验就对不上的行（实盘 8 条里 5 条对不上）。
                            # 这层估算的假设误差远大于 1 元，那点精度不值得拿
                            # 「报告里的数能不能自洽」去换。
                            wan = round(est.size / 1e4, 2)
                            metrics["可投(万)"] = wan
                            profit = wan * 1e4 * round(est.net, 2) / 100
                            metrics["预估(元)"] = int(round(profit))
                        else:
                            profit = est.profit
                        if thin:
                            # 动作词必须跟着结论走。「净收益 -0.98%」配「→ 折价买入」，
                            # 和当初「赎回暂停」配「→ 折价买入」是同一个毛病换了个档：
                            # 动作是整条里最显眼的一行，它说买，后面解释再清楚也晚了。
                            action = (f"折价 {-prem:.2f}% → 扣掉滑点与赎回费后仅 "
                                      f"{est.net:+.2f}%，兑现不划算（仅观察）")
                        # 成交价掉在盘口区间之外 = 这笔成交和这个盘口不是同一时点，
                        # 于是按卖一推出来的滑点是"推的"而不是"当场能验的"。
                        # v4.6：它是对**滑点那句**的限定，不是另一件事 —— 独立成句
                        # 会让「滑点 / 流动性 / 盘口 / 退市线」四句同时出现，撞破
                        # ⑤ 的 3 句预算。压成后缀，信息一字不少，句数省一。
                        off_book = bool(bid and ask and ask > bid > 0
                                        and not (bid <= px <= ask))
                        if est.slip > 0.005:
                            # 缩写自 72 字版。留下的是三个数（纸面折价／卖一／真折价），
                            # 删掉的是「差的 X 个百分点在盘口里」—— 那是同一个减法
                            # 的复述，读者看得见 3.00 和 2.50。
                            flags.append(
                                f"折价 {-prem:.2f}% 按最新价 {_num(px)} 算，建仓要吃卖一 "
                                f"{_num(ask)} → 真能拿到 {est.real_disc:.2f}%"
                                + ("（按收盘盘口推）" if off_book else ""))
                            if off_book:
                                _fn.add(_FN_OFF_BOOK)
                        # 滑点那句没触发（卖一 ≤ 最新价，按最新价计价）时，盘口异常
                        # 仍然要说 —— 那时它是唯一的信源。
                        #
                        # v4.6.1：这个 elif 原来接在下面「仓位卡在」那个 if 上，两个
                        # 方向都错。① 最新价高于卖一的倒挂盘口 + 仓位又被成交额卡住
                        # → 走了那个 if，盘口异常一个字不印，读者只看到「买卖价差…
                        # 已经折进净收益」，不知道这个盘口本身是坏的；② 滑点那句已经
                        # 带了「（按收盘盘口推）」后缀、而成交额又充裕 → elif 反而成立，
                        # 同一件事两句 96 字。判据本来就只有一个：滑点那句说没说过。
                        elif off_book:
                            flags.append(
                                f"最新价 {_num(px)} 在买一/卖一 [{_num(bid)}, {_num(ask)}] "
                                "之外，滑点是按收盘盘口推的")
                            _fn.add(_FN_OFF_BOOK)
                        # v4.4 删掉了 thin 分支的那句「扣掉滑点、赎回费…不够覆盖兑现成本」。
                        # 不是不重要，是**动作行已经原样写过一遍**：
                        #   动作：折价 2.00% → 扣掉滑点与赎回费后仅 -1.13%，兑现不划算（仅观察）
                        # 同一个 -1.13% 印两次，第二次只是把读者往下推 49 个字。
                        if est.by_liq and not thin:
                            # `not thin` 这个条件不能省：净收益 ≤ 0 时上面**故意**不印
                            # 「可投/预估」（不给一笔不该做的交易配仓位），这里再去读
                            # metrics['预估(元)'] 就是 KeyError。第一版漏了它，
                            # --mock 直接把整栏打成 ERROR ——「报告里的话」和「报告里的数」
                            # 是同一套约束，删话也要跟着数走。
                            # 这一句还和下面「日成交额低于阈值」那句合并过：两句原本都在说
                            # 同一个上限数字（08-08 的 161131：一句说「卡在 0.6 万」、
                            # 另一句说「约 0.6 万封顶」），合起来 120 字讲一件事。
                            flags.append(
                                f"日成交额 {amt / 1e4:.1f} 万 → 仓位卡在 "
                                f"{est.size / 1e4:.2f} 万（{part_pct:g}% 上限），"
                                f"{est.net:.2f}% 只值 {metrics['预估(元)']} 元"
                                if amt else
                                f"仓位卡在 {est.size / 1e4:.2f} 万（成交额 {part_pct:g}% 上限），"
                                f"{est.net:.2f}% 只值 {metrics['预估(元)']} 元")
                            _fn.add("「仓位卡在 X 万」= 这条的折价率再高也放不进钱："
                                    "单笔建仓按日成交额的固定比例封顶，百分比换不成收益。"
                                    "要少看几条，卡 min_profit_yuan（元），别提 "
                                    "discount_alert_pct（幅度）")
                        if amt is None:
                            flags.append("没给成交额，「可投」只按本金上限算、未折流动性")
                            _fn.add("「没给成交额」= 这一路走的是 clist 兜底（不返回盘口）。"
                                    "该条的「可投」缺了流动性这道约束，是偏乐观的上限，"
                                    "下单前自己看一眼盘口深度")
                else:
                    continue

                # 申赎状态进 metrics 而不是只塞 flags：它是折价能不能做的**前提**，
                # 该和折价率并排出现，不该藏在一长串风险提示里。
                if fr.gates:
                    if redeem:
                        metrics["赎回"] = redeem
                    if subs:
                        metrics["申购"] = subs
                    # 这句只对**折价**成立。上一版不分侧，于是「溢价 + 申购暂停」
                    # 的条目会挂上一句「折价要等开放日才谈得上兑现」—— 它的动作词
                    # 明明写的是溢价规避。溢价侧 blocked 排最后，前 10 条挤不进来
                    # 才一直没露相。
                    if blocked and prem < 0:
                        flags.append("定开/封闭期，折价要等开放日才谈得上兑现")
                        _fn.add("「定开/封闭期」= 这类产品在封闭期内天然折价，"
                                "何时能兑现取决于开放日 —— 折价不是价差，是期限成本。"
                                "留在报告里是因为它解释了「这只票为什么便宜」，"
                                "不想看就把 only_redeemable 设 true")
                    elif prem >= prem_th and subs == _GATE_CAPPED:
                        flags.append("申购限大额（本项目资金体量通常不触发）")

                # ---- 流动性与买卖价差：纸面折价能不能真买到 --------------------
                # 折价是按「最新价」算的，而最新价只是最后一笔成交。一天成交十几万
                # 的冷门 LOF，你要建 1-2 万的仓就得往上吃单，折价会在买入过程中
                # 自己消失。这两项只打标不过滤 —— 多少算够，取决于你打算下多大。
                if amt is not None:
                    metrics["日成交额(万)"] = round(amt / 1e4, 1)
                    # 只在**上面那句没印过**时才补 —— 否则同一个上限数字说两遍。
                    # 08-08 的 161131 就是两句都中：「卡在 0.60 万」+「约 0.6 万封顶」。
                    already = any("仓位卡在" in f for f in flags)
                    if amt < min_turnover * 1e4 and not already:
                        flags.append(
                            f"日成交额仅 {amt / 1e4:.1f} 万（低于 {min_turnover:g} 万），"
                            f"单笔建仓约 {amt * part_pct / 100 / 1e4:.1f} 万封顶")
                if bid and ask and ask > bid > 0 and px:
                    spread = (ask - bid) / px * 100
                    # v4.5：滑点那句已经把「最新价 / 卖一」两个价印出来了，价差这句
                    # 再印一遍「买一/卖一」，新增的信息只有买一和一个能推出来的百分比。
                    # 提示预算只有 3 句，退市线那句要进来，就得有一句出去 —— 出去的
                    # 应该是重复度最高的这句。滑点没触发时它仍然是唯一的价差信息源，
                    # 照印。
                    #
                    # v4.6.1：判据从「滑点那句印过没」扩成「盘口那两个价印过没」。
                    # 倒挂盘口那条走的是「最新价 X 在买一/卖一 [a, b] 之外」，它把
                    # 买一**和**卖一都印了，比这句还全 —— 同一个理由，同样该让位。
                    # 不扩的话，倒挂 + 冷门 + 深市小规模会凑出 4 句，撞破 ⑤。
                    book_shown = any(("建仓要吃卖一" in f) or ("在买一/卖一" in f)
                                     for f in flags)
                    if spread >= spread_th and not book_shown:
                        if est is not None:
                            # 「已经」两个字必须留在行内：verify_report 的不变量④
                            # 靠它抓「已扣过」配「请重算」那类自相矛盾。
                            flags.append(
                                f"买卖价差 {spread:.2f}%（{bid}/{ask}），已经折进净收益")
                            _fn.add("「买卖价差…已经折进净收益」= 买入价按卖一算，"
                                    "别在净收益上再扣一遍。它在这里提示的是**深度**："
                                    "卖一只是第一档，真按「可投」那个仓位去吃单，"
                                    "成交均价还会更高 —— 所以净收益是上限，不是预期")
                        else:
                            flags.append(
                                f"买卖价差 {spread:.2f}%（{bid}/{ask}）—— 实际成交价会偏离"
                                "最新价，折溢价要按你真正成交的价格重算")

                # ---- 退市线：这只票还能存在多久 --------------------------------
                # 2026-08-07 征求意见稿：连续 60 个交易日场内资产净值 < 1000 万的
                # LOF 应当终止上市。它卡的正是本工具选出来的那类票（场内盘子小 →
                # 没人套利 → 折价不收敛），所以摆在折价率旁边，不藏进提示里。
                if fr.role == "LOF" and nav:
                    sh = self._shares.get(code)
                    if sh:
                        onfloor = sh * nav
                        metrics["场内规模(万)"] = int(round(onfloor / 1e4))
                        # 脚注跟着**这一列**走，不跟着"触线"走。上一版挂在触线分支里，
                        # 于是没有一只票触线的日子，这一列会光秃秃地出现，而
                        # 「只覆盖深市、沪市拿不到、不标记≠安全」一个字都不印。
                        # 08-09 实盘就是这样：5 只有这一列、5 只没有，读者无从判断
                        # 那 5 个空白是"安全"还是"没数"。
                        _fn.add(_FN_ON_FLOOR)
                        if onfloor < delist_line * 1e4:
                            flags.append(
                                f"场内规模 {onfloor / 1e4:.0f} 万 < "
                                f"{delist_line:g} 万退市线")
                            _fn.add(_FN_DELIST)

                if blocked and only_redeemable:
                    n_skip_blocked += 1
                    continue

                # ---- 按元的门槛 -------------------------------------------
                # 上一版留了个问题：折价扫出来一堆薄的，是不是该把
                # `discount_alert_pct` 从 2.0 提到 3.0？08-08 的数据说**不该**——
                # 提到 3.0 会砍掉 506005（折价 2.27%、222 元，全场第二），
                # 却把 161131（折价 3.65%、81 元）留下来。百分比阈值筛的是纸面幅度，
                # 而幅度和收益已经被证明不是一回事。要少看几条就卡在元上。
                if min_profit and profit is not None and profit < min_profit:
                    dropped += 1
                    continue

                # 跨境：这句话每天一字不变（27 字 × 十几条），是标准的 footnote 料。
                # v4.6 改成 metrics 里的短标记「ETF·跨境」+ 一条脚注 —— 信息一字不丢，
                # 但不再占行内提示的预算。占预算的后果不是啰嗦：一只深市跨境 LOF
                # 完全可能同时命中 滑点/流动性/退市线，加上这句就是 4 句，撞破 ⑤。
                # 注意：这里说的滞后**不是**数据日期的陈旧程度（那个由帧级 stale
                # 检查负责）。QDII 已公布净值天然落后价格 1-2 个交易日，是结构性的。
                if is_cross:
                    _fn.add("形态里的「·跨境」= 已公布净值天然落后价格 1-2 个交易日"
                            "（结构性的，数据再新也存在），算出的折溢价混入了海外市场"
                            "这段时间的涨跌，**不是可直接交易的价差**。"
                            "要拿到真实折溢价，得用集思录 T-1 估值或基金公司实时 IOPV 校准")
                if data_dt is not None:
                    metrics["数据日期"] = fmt_date(data_dt)

                if not known:
                    _fn.add(_FN_GATE_UNKNOWN)

                opp = Opportunity(kind=self.kind, code=code, name=name, action=action,
                                  urgency=Urgency.WATCH, metrics=metrics, flags=flags)
                # 排序键：三档「可执行度」，档内**按绝对收益(元)**排，元相同再按幅度。
                #   0 = 能兑现且净收益为正   1 = 能兑现但成本吃光   2 = 赎回/申购暂停
                #
                # 两层都是被实盘数据推着改的：
                # 分档 —— 31 条折价里 22 条赎回暂停，且幅度最大的全在这 22 条里，
                #        只按幅度排会让 max_discount 的配额被做不了的品种占满。
                # 档内按元 —— 9 条可赎回按幅度 vs 按元，9 个名次**全部不同**。
                #        3.65% 的 161131 只值 81 元（卖一吃掉一半、一天成交 21 万），
                #        2.27% 的 506005 值 222 元（成交 1575 万，仓位放得满）。
                # 溢价侧没有 profit（走的是申购不是赎回，没算这笔账），
                # 键里恒为 0，自动回落到按幅度排 —— 与上一版行为一致。
                #
                # v4.6 从 3 档变 4 档，中间插进「闸门状态未取到」：
                #   0 可兑现 → 1 状态未取到 → 2 成本吃光 → 3 闸门暂停
                # 未取到排在「成本吃光」之前，因为后者是**算出来的负数**（确定不划算），
                # 前者只是没查到（可能划算）。排在「暂停」之前同理。
                #
                # `tier`（怎么数）和 `sort_rank`（怎么排）**分开**：
                # 计数与措辞的修正是无条件的 —— 把没取到的说成「可兑现」永远是错的。
                # 但要不要因此把它往后排，是个取舍：跨境 ETF 的申购状态全都取不到，
                # 一降档，那张「高溢价规避清单」就会被境内 LOF 顶掉两三只。
                # 所以排序留一个开关，计数不留。
                tier = 3 if blocked else (1 if not known else (2 if thin else 0))
                sort_rank = tier if (demote_unknown or tier != 1) else 0
                frame_emitted += 1
                (prem_pool if prem > 0 else disc_pool).append(
                    (sort_rank, -(profit or 0.0), -abs(prem), tier, opp))

            # ---- 帧级健康门：这一帧扫了一堆行却一行都算不出折溢价 ----------
            # 这是 v3 的关键修复。把门放在总数上（`valid == 0`）拦不住「一帧好、
            # 一帧废」的情况：好的那帧会把总数撑起来，废的那帧静默归零。
            #
            # 只在「有行」时判：空帧是另一种故障（LOF 那路 join 交集为 0），
            # 由 `_check_coverage` 报「市价 N × 净值 M → 交集 0」，比这里的
            # 「扫描 0 行但一行都算不出」精确得多。两边都报只会互相稀释。
            # 非 LOF 的两路有 reject_empty=True 兜着，空表根本走不到这里。
            if frame_computable == 0 and len(df) > 0:
                self._load_errs.append(
                    f"{label}: 扫描 {len(df)} 行但一行都算不出折溢价（列名改版或估值列全空）")
                self._missing.append(label)

            # ---- 数据日期：帧级事实，印一次 -------------------------------
            # 上一版把这两句挂在**每一条**上。可它们对同一帧的所有条目是同一个
            # 数据日期、同一个 lag —— 换一条票一字不变，正是 v4.4 定义的 notes 层
            # （本次运行才成立、但不随条目变化）。挂在条目上既是重复，又白占
            # 行内提示的预算：08-09 那种盘中跑法会给每条折价再加一句漂移提示，
            # 加上滑点/流动性/退市线就是 4 句。
            if frame_emitted and data_dt is not None and lag is not None:
                if lag > stale_days:
                    res.notes.append(
                        f"{label} 数据陈旧：数据日期 {fmt_date(data_dt)} 落后 {lag} 个交易日，"
                        "本帧的价差可能已失效")
                elif fr.role == "LOF" and lag:
                    # LOF 路特有：市价是实时的，净值是**已公布**的。盘中跑的时候
                    # 当日净值还没出，算出来的折价含一个交易日的净值漂移。
                    # 收盘后或周末跑则两边同日，lag=0，这句话不会出现。
                    res.notes.append(
                        f"本栏 LOF 净值取 {fmt_date(data_dt)} 已公布值，"
                        f"所有折价都含 {lag} 个交易日的净值漂移")
                    _fn.add("「净值含 N 个交易日漂移」= 市价是实时的，净值是已公布的；"
                            "当日净值收盘后才更新，所以盘中算出的折价里含这段漂移。"
                            "收盘后或周末跑两边同日，这条不会出现")

        # 「行」统一改成合并去重后的有效标的数，与命中数可比
        valid = len(accepted)

        # ---- 全局健康门：一个有效标的都没有 = 接口全面改版，不是「今天没机会」----
        if valid == 0:
            res.error = ("有效样本 0（列名改版或估值列全空）"
                         + ("；" + "；".join(self._load_errs) if self._load_errs else ""))
            res.notes.append("本栏 0 条 = 一个标的都没算出来，与「今天没机会」无关")
            return res
        res.rows_scanned = valid
        if self._load_errs:               # 部分帧失败：主源没全挂，但结果是残缺的
            res.error = "部分来源缺失（结果不完整）：" + "；".join(self._load_errs)

        if n_skip_cross:
            res.notes.append(
                f"另有 {n_skip_cross} 只**达到折溢价阈值**的境内标的按 "
                f"only_cross_border=true 未列出 —— 关掉它境内 LOF 才会进这一栏")
        if n_skip_blocked:
            res.notes.append(
                f"另有 {n_skip_blocked} 只申赎暂停的按 only_redeemable=true 未列出 —— "
                f"它们的折价解释了「这只票为什么便宜」，关掉开关能看到")

        self._check_coverage(res, frames, accepted, sanity_pct)
        self._check_signs(res, xcheck)

        # 折价在前：那才是小资金可能动手的一侧；溢价只是规避清单
        for side, cap_key, pool, cap in (("折价", "max_discount", disc_pool, max_disc_n),
                                         ("溢价", "max_premium", prem_pool, max_prem_n)):
            pool.sort(key=lambda x: x[:3])
            tiers = [sum(1 for e in pool if e[3] == t) for t in (0, 1, 2, 3)]
            visible = pool[:cap] if cap else pool
            res.opportunities.extend(o for *_, o in visible)
            if cap and len(pool) > cap:
                res.notes.append(
                    f"{side}侧共命中 {len(pool)} 条，受 {cap_key}={cap} 限制只列出前 {cap} 条")
            if any(tiers[1:]):
                gate = "赎回" if side == "折价" else "申购"
                parts = [f"{tiers[0]} 条可兑现"]
                if tiers[1]:
                    # 这一档是 v4.6 拆出来的。它以前和「可兑现」混在一起，于是
                    # 「溢价侧…27 条可兑现」里 27 条的申购状态其实一条都没取到。
                    parts.append(f"{tiers[1]} 条{gate}状态未取到")
                if tiers[2]:
                    parts.append(f"{tiers[2]} 条成本吃光")
                if tiers[3]:
                    parts.append(f"{tiers[3]} 条{gate}暂停")
                res.notes.append(
                    f"{side}侧 {len(pool)} 条按可执行度排序：" + "、".join(parts) +
                    f" —— 幅度最大的往往落在后面几档，按幅度排会把 {cap_key} 的配额占满")
            if side == "折价":
                if dropped:
                    res.notes.append(
                        f"另有 {dropped} 条折价达到 {disc_th:g}% 但预估收益不足 "
                        f"{min_profit:g} 元，已按 min_profit_yuan 略去 —— "
                        "卡的是元不是幅度，所以薄折价里成交额大的仍会留下")
                self._portfolio_note(res, visible, capital, cap, len(pool))

        # 三个字段的定义每天一字不变 —— 它是脚注，不是本次运行的结论。
        # 原来印在栏目抬头，160 字，把下面那条真正会变的「今天折价侧几条」压走了。
        if any("净收益(%)" in o.metrics for o in res.opportunities):
            _fn.add(
                f"「净收益(%)」= 按**卖一**买入能吃到的折价 − 赎回费 {fee_pct:g}% − "
                f"佣金 {comm_pct:g}%；「可投(万)」= min(本金×{max_pos_pct:g}%, "
                f"日成交额×{part_pct:g}%)；「预估(元)」= 两者相乘。"
                f"{fee_pct:g}% 取的是持有不足 7 日的监管下限，持有满 7 日通常降到 0.5% 上下，"
                "实际以各基金合同为准 —— 这一栏是提醒你去查费率表，不是替代它")

        cookie = os.getenv("JISILU_COOKIE") or c.get("jisilu_cookie", "")
        if cookie and not self.ctx.mock:
            self._augment_jisilu(res, cookie, prem_th, disc_th)

        res.footnotes.extend(_seen_fn.keys())
        return res

    # ------------------------------------------------------------------ 组合层面
    def _portfolio_note(self, res, visible, capital, cap, total_n):
        """把本金按次序铺满，算这一栏今天总共值多少钱。

        为什么非要有这一句：单条看「净收益 1.98%」很像一笔不错的买卖，铺满全部
        本金之后才看得出量级 —— 08-08 那天 9 条可赎回折价，10 万块钱铺到第 6 条
        就用完了，合计一千出头，约合本金的 1.2%。这个数不摆出来，人会不自觉地
        把「1.98%」当成组合收益率。

        更要紧的是它换的是什么。赎回按**提交申请当日**的净值结算，而报告里的折价
        是拿上一个**已公布**净值算的 —— 中间隔着一整天的净值涨跌，权益类 LOF
        一天动 1% 很常见，量级和这份收益相当。所以这不是「无风险 1.2%」，是拿
        一天的净值不确定性去换 1.2%。压掉这段不确定性的办法写在提示里。
        """
        # **只铺可见的那些**。上一版按全池贪心，于是 max_discount 一截断，印出来的
        # 「合计预估」就再也无法由报告里看得见的数复算出来 —— 探针实测 12 条折价、
        # max_discount=3 时印 4,680 元，可见条目只能算出 1,562 元，verify_report 的
        # 不变量②直接判负。而它判得对：读者没法验的数，本来就不该印。
        picks = [o for *_, tier, o in visible
                 if tier == 0 and int(o.metrics.get("预估(元)", 0)) > 0]
        if not picks:
            return
        left, total, used, each = capital, 0.0, 0, []
        partial = None
        for o in picks:
            if left <= 0:
                break
            full = float(o.metrics["可投(万)"]) * 1e4
            alloc = min(full, left)
            got = alloc * float(o.metrics["净收益(%)"]) / 100
            total += got
            each.append(got)
            left -= alloc
            used += 1
            if alloc < full:              # 本金在这条上用完了，它只填了一部分
                partial = (o, alloc, full)
        # 说清「前 N 条」里最后一条是不是填满了。实盘 08-08：本金铺到第 6 条只剩
        # 8,500 元、而那条上限 12,900 元，于是合计 1,096 比把 6 条的「预估」直接
        # 相加（1,111）少 15 元 —— 数是对的，话没说全，读者一加就以为哪里错了。
        # 这和刚修掉的「可投×净收益≠预估」是同一类：报告里的数必须能被验算。
        if partial is not None:
            po, alloc, full = partial
            scope = (f"够吃下前 {used - 1} 条，再加 {po.name}（{po.code}）的 "
                     f"{alloc:,.0f} 元 —— 本金到这里用完（该条上限 {full:,.0f} 元）")
            res.footnotes.append(
                "「合计预估」通常比把各条「预估」直接相加要少一点 —— 差额就是最后一条"
                "没填满的那截：本金铺到它那里就用完了，它的上限没用尽。"
                "08-08 实盘：6 条相加 1,111 元，实际合计 1,096 元，差的 15 元即此")
        else:
            scope = f"够吃下前 {used} 条"
        # 本金没铺完 + 后面还有被截断的条目 → 说清楚合计只统计到看得见的这些。
        # 不说的话，读者会以为「本金铺满 = 这就是这一栏的全部收益」。
        beyond = (f"，本金还剩 {left:,.0f} 元没铺完 —— 后面的条目被 {'max_discount'}"
                  f"={cap} 截断，合计只算了看得见的这 {used} 条"
                  if (left > 0 and cap and total_n > cap) else "")
        # 集中度：08-08 实测前 2 条占了 74%，后面 4 条合计 280 元却要占掉 4 万本金
        # 和 4 次「买入 + 赎回 + 等到账」。不点出来，人会默认这一栏得整栏做完。
        conc = ""
        if used >= 4 and total > 0:
            head = sum(each[:2])
            if head / total >= 0.6:
                conc = (f"前 2 条占 {head / total:.0%}（{head:,.0f} 元），"
                        f"后 {used - 2} 条合计 {total - head:,.0f} 元。")
                res.footnotes.append(
                    "「前 2 条占 X%」= 这一栏不必整栏做完。尾部那几条每条都是一次买入、"
                    "一次赎回申请、一笔资金占用到到账，换来的却是零头 —— "
                    "值不值得，按 min_profit_yuan 卡掉自己定")
        # banner 里只留**今天才成立**的数字；后面那段「这笔钱换的是什么」
        # 每天一字不变（110 字），归脚注。注意「合计预估 N 元」必须留在 banner，
        # verify_report 的不变量②要从这句话里把 N 解析出来复算。
        res.notes.append(
            f"组合口径：按上面的次序把 {capital:,.0f} 元铺满，{scope}{beyond}，"
            f"合计预估 {total:,.0f} 元 ≈ 本金的 {total / capital * 100:.2f}%。" + conc)
        res.footnotes.append(
            "「组合合计」换的是**买入当日净值**的不确定性：赎回按提交申请当日净值结算，"
            "而报告里的折价是拿上一个已公布净值算的，中间隔着一天涨跌 —— "
            "权益类 LOF 一天动 1% 很常见，和这份收益同量级。"
            "要压掉这段：盯住标的指数、14:45 之后当日净值基本定型时再买入，"
            "并在当日 15:00 前把赎回提上去，别隔夜。")

    # ------------------------------------------------------------------ 覆盖 / 口径体检
    def _check_coverage(self, res, frames, accepted, sanity_pct):
        """栏目级提示：说清楚缺的是哪一侧，外加 LOF 那路的口径体检。

        最容易吃亏的地方：折价那一路没取到时，报告照样显示一堆 ETF 溢价、
        折价 0 条，看上去像「今天没折价机会」，实际是那一段根本没扫。
        """
        if "LOF市价" in self._missing:
            res.notes.append(
                "LOF 市价两路都没取到（新浪 fund_etf_category_sina + 东财 clist 兜底）。"
                "场内显著折价基本都在 LOF 段（160-169/501/502/505/506/150）—— "
                "本次「折价」结论不可用，0 条只代表未扫描")
        if "LOF净值" in self._missing:
            res.notes.append(
                "LOF 净值未取到（同花顺 fund_etf_category_ths）。有市价没净值算不出"
                "折溢价 —— 本次「折价」结论不可用，0 条只代表未扫描")
        if "ETF行情" in self._missing:
            res.notes.append(
                "ETF 实时行情（fund_etf_spot_em）未取到，跨境溢价一侧本次不可用")
        if "场内日行情" in self._missing:
            res.notes.append(
                "场内日行情（fund_etf_fund_daily_em）未取到。它实测不覆盖 LOF，"
                "折价侧不受影响；缺的是 59 个 ETF 代码和符号正负的对账基准")

        lof_fr = next((f for f in frames if f.role == "LOF"), None)
        if lof_fr is None:
            return

        # 份额那一路挂掉时必须出声。它不影响任何折价数值，所以既不是 error 也不进
        # _missing —— 但**沉默的后果和「都在线上」长得一模一样**：一条退市线提示都
        # 不出，`场内规模` 整列消失。这正是这个项目一路在修的那种 0。
        if self._share_err:
            # v5.9.2：截断从 28 放宽到 68。28 会把 `fund_scale_daily_szse` 切成
            # `fund_scale_daily_s`，读起来像另一个接口名 —— 让人去查一个不存在的东西，
            # 而这一句的全部作用就是告诉你去查哪个接口。68 刚好装得下最长的那条
            # （「akshare 无 … 接口（建议 pip install -U akshare）」，66 字），
            # 加上外层措辞是 121 字，栏目说明 160 字的预算仍有富余。
            err_txt = str(self._share_err)
            err_txt = err_txt if len(err_txt) <= 68 else err_txt[:68] + "…"
            res.notes.append(
                f"退市线本次未判定：深交所场内份额没取到（{err_txt}）"
                " —— 没有条目会标「场内规模 < 退市线」，但这不代表它们都在线上")

        d = lof_fr.diag
        n = d.get("n") or 0
        if n == 0:
            msg = (f"LOF 路市价 {d.get('price_n')} 个 × 净值 {d.get('nav_n')} 个 → 交集 0，"
                   "两边代码对不上（多半是某一路的代码格式变了）")
            res.error = ((res.error + "；") if res.error else "") + msg
            res.notes.append(
                "LOF 市价和净值都取到了，但一只都对不上号 —— 折价侧实际是空的")
            return

        # 口径体检：境内 LOF 常态折溢价很小，整体中位数应当贴着 0（实测 -0.59%）。
        # 偏得远 = 多半把净值列选错了（例如接口改版后「最新-单位净值」变成了别的
        # 口径）。样本少于 30 不下结论，理由同符号对账。
        #
        # 这道门的边界要说清楚：它抓得住「换成了完全不同的口径」，抓不住
        # 「换成了前一日净值」（实测中位数 +0.35%，稳稳在阈值内）。后者只能靠
        # `_LOF_NAV_COLS` 白名单挡在前面 —— 两道防线针对的不是同一种故障。
        med = d.get("median")
        if med is not None and n >= 30 and abs(med) > sanity_pct:
            msg = (f"LOF 折溢价中位数 {med:+.2f}%（{n} 只）偏离 0 超过 {sanity_pct}% —— "
                   "多半是净值列口径不对（如接口改版后取到了非当期净值），"
                   "本次折价数值不要据以操作")
            res.error = ((res.error + "；") if res.error else "") + msg
            res.notes.append(msg)
        elif med is not None:
            log.debug("LOF 口径体检通过：%d 只，中位数 %+.2f%%", n, med)

        lof_n = sum(1 for c in accepted if _is_lof(c))
        if lof_n == 0 and not self._missing:
            res.notes.append(
                "各路都取到数，但有效样本里没有任何 LOF —— 折价侧实际仍是空的，"
                "「折价 0 条」不可当作「今天没折价机会」")

    def _check_signs(self, res, xcheck):
        """符号交叉校验：两套口径对同一批境内标的应当给出同向的结果。

        「折价率列名里带『折价』就取反」只是个约定，没人保证接口一直这么写。
        一旦这个约定错了，输出的不是「没结果」，而是把「折价买入」和「溢价规避」
        精确地印反 —— 比取不到数危险得多。两帧重叠的那批代码是白给的对账数据。
        """
        flipped, n_x, same_err, flip_err = _sign_verdict(xcheck)
        if flipped:
            msg = (f"折价率正负口径校验未通过（{n_x} 个境内标的对账："
                   f"同号残差 {same_err:.2f} > 反号残差 {flip_err:.2f}）—— "
                   "接口的折价率符号约定可能已变，折价/溢价方向存疑，本次结论不要据以操作")
            res.error = (res.error + "；" if res.error else "") + msg
            res.notes.append(msg)
        elif flipped is False:
            log.debug("折价率符号校验通过：%d 个样本，同号残差 %.3f / 反号残差 %.3f",
                      n_x, same_err, flip_err)

    # ------------------------------------------------------------------ 集思录增强
    def _augment_jisilu(self, res, cookie, prem_th, disc_th):
        """集思录 QDII T-1 估值溢价：跨境标的盘中更可用的口径。"""
        import akshare as ak
        seen = {o.code for o in res.opportunities}
        for fn_name in ("qdii_e_index_jsl", "qdii_a_index_jsl"):
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            df, _ = safe_call(fn, cookie=cookie)
            if df is None or getattr(df, "empty", True):
                continue
            col_prem = _pick_col(df, "T-1溢价率", "溢价率")
            col_code = _pick_col(df, "代码")
            col_name = _pick_col(df, "名称")
            if not (col_prem and col_code):
                continue
            for _, r in df.iterrows():
                prem = to_float(r.get(col_prem))
                code = _norm_code(r.get(col_code))
                if prem is None or not code or code in seen:
                    continue
                if -disc_th < prem < prem_th:
                    continue
                seen.add(code)
                res.opportunities.append(Opportunity(
                    kind=self.kind, code=code,
                    name=(str(r.get(col_name, "")) if col_name else ""),
                    action=(f"溢价 {prem:.2f}% → 规避/套利" if prem > 0
                            else f"折价 {-prem:.2f}% → 折价买入"),
                    urgency=Urgency.WATCH,
                    metrics={"口径": "集思录T-1估值", "T-1溢价率(%)": round(prem, 2)},
                    flags=["基于 T-1 估值，隔夜海外波动会改变实际溢价"],
                ))


def _num(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return round(v, 3)
