"""针对历次修复的验证脚本（不联网）。

这是七项离线自检里最重的一项，也是**唯一拦得住行为退化**的东西：
每条断言都对应一次真实踩过的坑，且按规矩**必须先在旧代码上验过是红的**
才允许并进来 —— 否则它钉住的只是当前实现，不是那个不变量。

覆盖：真名日志、空结果不入缓存、重试绕缓存、交易日窗口、交易日差、
栏目级告警渲染、五个源各自的「0 条是哪种 0」、报告级不变量、
以及 v5.9.3 起的「非实盘产出不许覆盖实盘归档」。
"""
import datetime as dt
import logging
import pathlib
import subprocess
import sys
from functools import partial

import pandas as pd

sys.path.insert(0, ".")
from scanner import utils  # noqa: E402
from scanner.models import Kind, Opportunity, SourceResult, Urgency  # noqa: E402
from scanner.report import render_console, render_html  # noqa: E402
from scanner.sources.base import Context  # noqa: E402

ok = lambda label: print(f"  [PASS] {label}")


# ---------------------------------------------------------------- 1. 日志真名
class _Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, r):
        self.msgs.append(r.getMessage())


def test_fn_name():
    grab = _Grab()
    utils.log.addHandler(grab)
    utils.log.setLevel(logging.WARNING)

    def fund_lof_spot_em():
        raise ConnectionError("Remote end closed connection without response")

    utils.safe_call(lambda: fund_lof_spot_em())                       # 裸 lambda：靠字节码兜底
    utils.safe_call(partial(fund_lof_spot_em))                        # partial：穿透 .func
    utils.safe_call(fund_lof_spot_em, _label="fund_lof_spot_em(LOF)")  # 显式 label
    utils.log.removeHandler(grab)

    joined = " | ".join(grab.msgs)
    assert "调用 <lambda> 失败" not in joined, joined
    assert joined.count("fund_lof_spot_em") == 3, joined
    ok("三种写法都能报出真实接口名，日志里不再有光秃秃的 <lambda>")


def test_no_bare_lambda_callsites():
    """约定检查：调用点不该再把网络调用裹进裸 lambda。

    用 AST 而不是正则 —— 文档字符串里为了说明问题会写出这个模式，
    正则会把注释也算成违规。
    """
    import ast
    bad = []
    for f in pathlib.Path("scanner").rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname not in ("safe_call", "retry_call"):
                continue
            args = list(node.args) + [k.value for k in node.keywords]
            if any(isinstance(a, ast.Lambda) for a in args):
                bad.append(f"{f}:{node.lineno}")
    assert not bad, "仍有裸 lambda 调用点：" + ", ".join(bad)
    ok("scanner/ 下已无 safe_call(lambda …) / retry_call(lambda …) 调用点")


# ------------------------------------------------- 2/3. 缓存：空结果 + 重试刷新
def test_cache_empty_and_refresh():
    utils.clear_cache()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        # 第 1 次返回空表（模拟接口异常），第 2 次才正常
        return pd.DataFrame() if calls["n"] == 1 else pd.DataFrame({"代码": ["513100"]})

    df, err = utils.retry_call(flaky, label="flaky", attempts=2, backoff=(0,),
                               cache_key="test::flaky", ttl_seconds=900)
    assert err is None and df is not None and not df.empty, (err, df)
    assert calls["n"] == 2, f"重试没有真正重新发起调用，只调了 {calls['n']} 次"
    ok("空表不入缓存 + 重试 refresh 生效（第 2 次真的重发，不是命中坏缓存）")

    # 非空结果应当已落盘：再调一次不应触发新的网络调用
    before = calls["n"]
    utils.disk_cache("test::flaky", flaky, ttl_seconds=900)
    assert calls["n"] == before, "有效结果没有被缓存"
    ok("有效结果正常落盘，重复调用命中缓存")
    utils.clear_cache()


def test_retry_gives_up():
    def dead():
        raise ConnectionError("Connection aborted.")

    logging.disable(logging.WARNING)
    v, err = utils.retry_call(dead, label="dead", attempts=3, backoff=(0, 0))
    logging.disable(logging.NOTSET)
    assert v is None and "ConnectionError" in err, err
    ok("重试用尽后如实返回错误，不吞掉")


# ------------------------------------------------------------- 4. 交易日窗口
def test_trading_window():
    fri, sat = dt.date(2026, 8, 7), dt.date(2026, 8, 8)
    mon, tue = dt.date(2026, 8, 10), dt.date(2026, 8, 11)

    # 旧口径：周五 + 2 日历天 = 周日 → 周一缴款日漏掉
    assert not (fri <= mon <= fri + dt.timedelta(days=2)), "旧口径本应漏掉周一"

    # 新口径：周五 + 2 交易日 = 周二 → 周一被覆盖
    assert utils.shift_trading_days(fri, 2) == tue
    assert fri <= mon <= utils.shift_trading_days(fri, 2)
    ok("周五跑：+2 交易日 = 周二，下周一的缴款日不再漏掉")

    assert utils.shift_trading_days(sat, 2) == tue
    assert utils.shift_trading_days(sat, 0) == sat
    ok("周六跑：+2 交易日 = 周二；n=0 原样返回")

    # 日历覆盖范围之外必须回落到跳周末，不能一路走到守卫上限
    cal = utils.TradeCalendar({dt.date(2020, 1, 2)})
    assert utils.shift_trading_days(fri, 2, cal) == tue
    ok("交易日历越界时回落跳周末，不会返回几百天后的日期")


def test_trading_days_between():
    fri, mon = dt.date(2026, 8, 7), dt.date(2026, 8, 10)
    assert (mon - fri).days == 3                      # 日历天差 3
    assert utils.trading_days_between(fri, mon) == 1  # 交易日差 1
    assert utils.trading_days_between(fri, dt.date(2026, 8, 8)) == 0
    assert utils.trading_days_between(mon, fri) == 0  # 传反不炸
    assert utils.trading_days_between(None, mon) is None
    ok("交易日差正确：周一看周五的数据不会被误判为陈旧")


# --------------------------------------------------------- 5. 栏目级告警渲染
def test_section_banner():
    ctx = Context(cfg={"capital": 100000, "accounts": 1}, today=dt.date(2026, 8, 8))
    broken = SourceResult(kind=Kind.FUND_PREM, rows_scanned=1570)
    broken.error = "部分来源缺失（结果不完整）：LOF: ConnectionError"
    broken.notes.append("LOF 全市场未取到。场内显著折价基本集中在 LOF 段，"
                        "本次「折价」结论不可用 —— 0 条只代表未扫描")
    out = render_console([broken], ctx)

    assert "[!] 本栏取数异常" in out
    assert "只代表未扫描" in out
    assert "0 条不代表没有机会" in out
    banner_at = out.index("LOF 全市场未取到")
    health_at = out.index("数据源健康")
    assert banner_at < health_at, "告警必须贴着栏目，而不是只在底部健康面板"
    ok("取数异常与口径提示直接印在栏目标题下方，且早于底部健康面板")



# ------------------------------------------------- 6. 帧级健康门 / 多源合并
def _fund_premium_with(frames, cfg=None, lof="mock", capital=100000, shares="mock"):
    """用给定的帧跑一遍 fund_premium，返回 SourceResult。

    `lof` 控制第三路（新浪市价 × 同花顺净值）：
      "mock"         → 用模块自带的 mock 字典，这一路健康
      None           → 这一路整体缺失
      (prices, navs) → 自定义注入

    `shares` 控制深交所场内份额那一路（退市线判断用）：
      "mock" → 模块自带的假份额    {} → 这一路缺失（老 akshare / 接口挂了）

    四个取数函数都被替换掉，**包括 clist 兜底** —— 否则 lof=None 的用例会真的
    去打东财，测试就不再是离线的了（而且在受限网络下要卡满超时）。
    """
    import types
    import scanner.utils as u
    import scanner.sources.fund_premium as fp

    fake = types.ModuleType("akshare")
    fake.fund_etf_spot_em = lambda: frames[0]
    fake.fund_etf_fund_daily_em = lambda: frames[1]
    sys.modules["akshare"] = fake
    u.clear_cache()
    u._TRADE_CAL.update(loaded=True, cal=None)

    if lof == "mock":
        prices, navs = fp._mock_lof_prices(), fp._mock_lof_navs()
    elif lof is None:
        prices, navs = {}, {}
    else:
        prices, navs = lof

    sh = fp._mock_lof_shares() if shares == "mock" else (shares or {})
    saved = (fp._INTER_CALL_GAP, fp._lof_prices_sina,
             fp._lof_prices_clist, fp._lof_navs_ths, fp._lof_szse_shares)
    fp._INTER_CALL_GAP = 0
    fp._lof_prices_sina = lambda retries, today: (prices, None if prices else "stub:新浪不可用")
    fp._lof_prices_clist = lambda: ({}, "stub:不联网")
    fp._lof_navs_ths = lambda retries, today: (navs, {}, None if navs else "stub:同花顺不可用")
    fp._lof_szse_shares = lambda retries, today: (sh, None if sh else "stub:份额不可用")
    try:
        # 本金写死 10 万：v4.2 起「可投/预估」依赖它，靠 Context 默认值(15 万)
        # 会让断言跟着默认值飘。
        ctx = Context(cfg={"capital": capital, "fund_premium": cfg or {}},
                      today=dt.date(2026, 8, 8))
        return fp.FundPremiumSource(ctx).fetch()
    finally:
        (fp._INTER_CALL_GAP, fp._lof_prices_sina,
         fp._lof_prices_clist, fp._lof_navs_ths, fp._lof_szse_shares) = saved
        u.clear_cache()
        sys.modules.pop("akshare", None)


# fund_lof_spot_em 的真实返回列：没有 IOPV，也没有折价率
_LOF_SPOT_COLS = ["代码", "名称", "最新价", "涨跌额", "涨跌幅", "成交量", "成交额",
                  "开盘价", "最高价", "最低价", "昨收", "换手率", "流通市值", "总市值"]


def test_frame_health_gate():
    """一帧好一帧废时，废的那帧必须报错，不能被好帧的样本数掩盖。"""
    spot = pd.DataFrame([{"代码": f"5131{i:02d}", "名称": f"纳指ETF{i}",
                          "最新价": 2.0, "IOPV实时估值": 1.75,
                          "数据日期": "2026-08-07"} for i in range(50)])
    dead = pd.DataFrame([dict(zip(_LOF_SPOT_COLS, [f"1611{i:02d}", f"某LOF{i}", 1.2] + [0] * 11))
                         for i in range(30)])

    res = _fund_premium_with([spot, dead], lof=None)
    assert res.error and "无估值列也无折价率列" in res.error, res.error
    assert any("折价" in n and "0 条只代表未扫描" in n for n in res.notes), res.notes
    assert res.rows_scanned == 50, res.rows_scanned
    ok("帧级健康门：无估值列的那一帧被单独判死，不再被另一帧的样本数掩盖")


def test_merge_and_dedupe():
    """三源合并：重复代码只出一次，脏值跳过，折价排在溢价前。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"premium_alert_pct": 3.0, "discount_alert_pct": 2.0})
    assert res.error is None, res.error

    by_code = {o.code: o for o in res.opportunities}
    assert len(by_code) == len(res.opportunities), "同一代码出了多条"

    # 513100 在两帧都有 → 以 spot 帧的 IOPV 口径为准，只出一条
    assert by_code["513100"].metrics["口径"] == "IOPV自算"
    # 161130 只在日行情帧 → 走接口折价率，且被识别为折价
    assert "折价" in by_code["161130"].action
    # v4.6：跨境从行内提示压成形态标记（示例标普LOF 命中「标普」）
    assert by_code["161130"].metrics["形态"] == "LOF·跨境", by_code["161130"].metrics
    assert by_code["510300" if "510300" in by_code else "160719"].metrics["形态"] == "LOF"
    # 折价率 '---' 的那条应被跳过
    assert "161234" not in by_code
    # LOF 路：同花顺没有 164999 → join 不上，直接丢，不做任何填补
    assert "164999" not in by_code
    # 折价整体排在溢价前面
    kinds = ["折价" if "折价" in o.action else "溢价" for o in res.opportunities]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "折价" else 1), kinds
    ok("三源合并正确：重复代码去重、'---' 脏值跳过、join 不上的丢弃、折价排在最前")


def test_lof_basis_label_is_honest():
    """LOF 路用的是已公布净值，口径不能标成「IOPV自算」。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    lof = [o for o in res.opportunities if o.code == "160719"][0]
    basis = lof.metrics["口径"]
    assert "IOPV" not in basis, basis
    assert "新浪" in basis and "同花顺" in basis and "单位净值" in basis, basis
    ok("LOF 口径如实标注为「新浪市价/同花顺最新-单位净值」，不冒充实时 IOPV")


def test_redeem_gate_ranking():
    """赎回暂停的折价即使幅度更大，也要排在可赎回的后面，且动作词改写。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    by_code = {o.code: o for o in res.opportunities}
    order = [o.code for o in res.opportunities]

    # 501065 折价 8%（赎回暂停） vs 160719 折价 4%（赎回开放）
    assert order.index("160719") < order.index("501065"), order
    assert "折价买入" in by_code["160719"].action, by_code["160719"].action
    assert "折价买入" not in by_code["501065"].action, by_code["501065"].action
    assert "无法通过赎回兑现" in by_code["501065"].action, by_code["501065"].action
    assert by_code["501065"].metrics["赎回"] == "暂停"
    assert any("定开/封闭期" in f for f in by_code["501065"].flags), by_code["501065"].flags
    # v4.4：「折价不是价差，是期限成本」这句每天一字不变 → 归 footnotes，
    # 行内只留短标签。两边都要在，否则就是把话删没了而不是挪走了。
    assert any("期限成本" in f for f in res.footnotes), res.footnotes
    # 「限制大额」不降权，只打提示
    assert by_code["162412"].metrics["申购"] == "限制大额"
    assert any("不触发" in f for f in by_code["162412"].flags)
    assert any("赎回暂停" in n and "配额" in n for n in res.notes), res.notes
    ok("申赎闸门：赎回暂停的折价降权并改写动作词，「限制大额」不降权")


def test_only_redeemable_switch():
    """only_redeemable=true 时，不可兑现的那些直接不出条。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"only_redeemable": True})
    codes = {o.code for o in res.opportunities}
    assert "501065" not in codes, codes
    assert "160719" in codes, codes
    ok("only_redeemable 开关生效：申赎暂停的品种整条不出")


def test_data_date_from_column_name():
    """场内日行情没有独立日期列，数据日期要从列名里解析出来。"""
    from scanner.sources.fund_premium import _frame_data_date, _mock_daily_df

    assert _frame_data_date(_mock_daily_df()) == dt.date(2026, 8, 7)
    assert _frame_data_date(pd.DataFrame([{"代码": "1", "最新价": 1.0}])) is None
    ok("数据日期能从「2026-08-07-单位净值」这类列名里解析出来")


def test_cross_border_marker_not_a_flag():
    """跨境是标记 + 脚注，不再占行内提示的预算；且不能引用数据日期滞后。

    v4.6 改的动机不是审美：这句话每天一字不变、十几条各印一遍，而一只深市跨境
    LOF 完全可能同时命中 滑点 / 流动性 / 退市线 —— 再加这一句就是 4 句，撞破 ⑤。
    """
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    cross = [o for o in res.opportunities if o.code == "513100"][0]
    dom = [o for o in res.opportunities if o.code == "160719"][0]

    # 标记进 metrics，境内的不带
    assert cross.metrics["形态"] == "ETF·跨境", cross.metrics
    assert dom.metrics["形态"] == "LOF", dom.metrics
    # 行内提示里不再有这句（省下的是预算，不是信息）
    assert not any("跨境" in f for f in cross.flags), cross.flags
    # 而结论必须在脚注里原样保住，否则就是把话删没了而不是挪走了
    fns = "".join(res.footnotes)
    assert "净值天然落后价格 1-2 个交易日" in fns, res.footnotes
    assert "集思录 T-1 估值" in fns, res.footnotes
    assert "不是可直接交易的价差" in fns, res.footnotes
    # 「滞后 0」那种反向误导，行内和脚注都不许出现
    assert "滞后 0" not in "；".join(cross.flags) + fns, \
        "又把数据日期滞后当成 QDII 净值滞后了"
    ok("跨境压成形态标记 + 脚注，结论不丢、不再占行内提示预算")


def test_nav_drift_is_a_frame_note_not_a_per_item_flag():
    """净值漂移只在净值日 < 今天时出现，而且是**栏目级说一次**，不是每条挂一句。

    它对同一帧的每一条都是同一个数据日期、同一个 lag —— 换一条票一字不变，
    按 v4.4 的三层判据就该在 notes 层。挂在条目上既重复，又白占行内提示的预算
    （盘中跑时它会和 滑点/流动性/退市线 一起把一条顶到 4 句）。
    """
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    # ctx.today = 2026-08-08(周六)，净值日 08-07(周五) → 交易日差 0 → 不提示
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    assert not any("漂移" in f for o in res.opportunities for f in o.flags)
    assert not any("漂移" in n for n in res.notes), res.notes

    # 把净值日推到 08-06(周四) → 与周六差 1 个交易日 → 应提示
    import scanner.sources.fund_premium as fp
    navs = {k: dict(v, date="2026-08-06") for k, v in fp._mock_lof_navs().items()}
    res2 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              lof=(fp._mock_lof_prices(), navs))
    drift = [n for n in res2.notes if "漂移" in n]
    assert len(drift) == 1, res2.notes          # 一次，不是每条一次
    assert "1 个交易日" in drift[0], drift
    # 行内一句都不许留
    assert not any("漂移" in f for o in res2.opportunities for f in o.flags), \
        [o.flags for o in res2.opportunities]
    assert any("漂移" in f for f in res2.footnotes), res2.footnotes
    ok("净值漂移按交易日差触发，且只在栏目级说一次，不再挂到每一条上")


# ------------------------------------------------- 7. 符号交叉校验
def _sign_frames(flip_sign):
    """构造两帧覆盖同一批境内 ETF 的数据；flip_sign=True 模拟接口反号。"""
    import random
    random.seed(7)
    spot, daily = [], []
    for i in range(120):
        code = f"5100{i:02d}"
        nav, prem = 3.0, random.uniform(-1.5, 1.5)
        px = nav * (1 + prem / 100)
        spot.append({"代码": code, "名称": f"沪深300ETF{i}", "最新价": round(px, 4),
                     "IOPV实时估值": nav, "数据日期": "2026-08-07"})
        disc = prem if flip_sign else -prem      # 正常口径：折价率 = -溢价率
        daily.append({"基金代码": code, "基金简称": f"沪深300ETF{i}", "类型": "指数型-股票",
                      "2026-08-07-单位净值": nav, "市价": round(px, 4),
                      "折价率": f"{disc:.2f}%"})
    daily.append({"基金代码": "161130", "基金简称": "某LOF", "类型": "指数型-股票",
                  "2026-08-07-单位净值": 1.0, "市价": 0.96, "折价率": "4.00%"})
    return pd.DataFrame(spot), pd.DataFrame(daily)


def test_sign_cross_check():
    good = _fund_premium_with(list(_sign_frames(flip_sign=False)))
    assert good.error is None, f"正常口径被误报：{good.error}"
    assert not any("正负口径" in n for n in good.notes), good.notes

    bad = _fund_premium_with(list(_sign_frames(flip_sign=True)))
    assert bad.error and "正负口径校验未通过" in bad.error, bad.error
    assert any("方向存疑" in n for n in bad.notes), bad.notes
    ok("折价率正负口径交叉校验：正常不误报，反号被抓出并写进 error")


# ------------------------------------------------- 8. v4：LOF 路的门
def _q(px, name="某LOF", amt=5_000_000, bid=None, ask=None):
    """构造一条新浪行情记录（v4.1 起是 dict，不再是 (name, px) 元组）。"""
    return {"name": name, "px": px, "amt": amt, "bid": bid, "ask": ask}


def _plain_frames():
    """两帧健康的 ETF 数据，用来当 LOF 各用例的背景板。"""
    spot = pd.DataFrame([{"代码": f"5100{i:02d}", "名称": f"沪深300ETF{i}",
                          "最新价": 3.0, "IOPV实时估值": 3.0,
                          "数据日期": "2026-08-07"} for i in range(40)])
    daily = pd.DataFrame(
        [{"基金代码": f"5100{i:02d}", "基金简称": f"沪深300ETF{i}", "类型": "指数型-股票",
          "2026-08-07-单位净值": 3.0, "市价": 3.0, "折价率": "0.00%"} for i in range(40)])
    return [spot, daily]


def test_lof_missing_side_is_named():
    """缺市价和缺净值必须给出**不同**的提示 —— 合成一句就等于下次重查一遍。"""
    navs = {"160719": {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                       "sub": "开放", "date": "2026-08-07"}}
    no_px = _fund_premium_with(_plain_frames(), lof=({}, navs))

    assert any("LOF 市价两路都没取到" in n for n in no_px.notes), no_px.notes
    assert not any("LOF 净值未取到" in n for n in no_px.notes), no_px.notes

    no_nav = _fund_premium_with(_plain_frames(),
                                lof=({"160719": _q(0.96)}, {}))
    assert any("LOF 净值未取到" in n for n in no_nav.notes), no_nav.notes
    assert not any("LOF 市价两路都没取到" in n for n in no_nav.notes), no_nav.notes
    ok("LOF 缺失提示区分市价 / 净值两侧，不再合成一句「取数失败」")


def test_lof_join_zero():
    """市价、净值都取到但代码对不上 —— 表现同样是 0 条，必须单独报出来。"""
    prices = {"160719": _q(0.96)}
    navs = {"501065": {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                       "sub": "开放", "date": "2026-08-07"}}
    res = _fund_premium_with(_plain_frames(), lof=(prices, navs))
    assert res.error and "交集 0" in res.error, res.error
    assert any("一只都对不上号" in n for n in res.notes), res.notes
    # 帧级门不该跟着叫「扫描 0 行但一行都算不出」—— 两条一起报只会互相稀释
    assert "扫描 0 行" not in res.error, res.error
    ok("LOF join 交集为 0 时单独报错，不与「今天没折价机会」混淆")


def test_lof_sanity_median_gate():
    """净值口径整体错位时，中位数体检要把它拦下来。"""
    prices, navs = {}, {}
    for i in range(60):                       # 样本 ≥30 才下结论
        code = f"1607{i:02d}"
        prices[code] = _q(1.20, f"某LOF{i}")  # 市价统一比净值高 20% → 中位数 +20%
        navs[code] = {"nav": 1.00, "basis": "最新-单位净值", "redeem": "开放",
                      "sub": "开放", "date": "2026-08-07"}
    res = _fund_premium_with(_plain_frames(), lof=(prices, navs))
    assert res.error and "中位数" in res.error, res.error
    assert any("不要据以操作" in n for n in res.notes), res.notes

    # 样本不足 30 时不下结论（mock 只有 3 只，偏得再远也不该报）
    small = _fund_premium_with(_plain_frames())
    assert not (small.error and "中位数" in small.error), small.error
    ok("中位数体检：整体口径错位被拦下，样本不足时不乱下结论")


def test_nav_col_whitelist():
    """只剩「前一日-单位净值」时必须报错，而不是拿它凑合。

    这一条是整个 v4 里最容易被「优化」掉的：拿前一日净值兜底能把覆盖率
    从 344 提到 347，看起来是净赚。但它把 T-1 净值配 T 日市价，溢价≥3%
    的条数会从 13 条虚增到 35 条，而中位数只偏 +0.35% —— 上面那道体检
    根本抓不住。所以白名单必须硬。
    """
    import types
    import scanner.utils as u
    import scanner.sources.fund_premium as fp

    df = pd.DataFrame([{"基金代码": "160719", "前一日-单位净值": 1.0,
                        "赎回状态": "开放", "申购状态": "开放",
                        "最新-交易日": "2026-08-07"}])
    fake = types.ModuleType("akshare")
    fake.fund_etf_category_ths = lambda symbol=None: df
    sys.modules["akshare"] = fake
    u.clear_cache()
    try:
        navs, hits, err = fp._lof_navs_ths(1, dt.date(2026, 8, 8))
    finally:
        u.clear_cache()
        sys.modules.pop("akshare", None)

    assert not navs, navs
    assert err and "净值列全部缺失" in err, err
    assert "前一日-单位净值" in err, "报错要指出实际有哪些净值列，否则下次还得自己去翻"
    ok("净值列白名单：只剩「前一日-单位净值」时明确报错，不静默混口径")


def test_code_normalisation():
    """新浪可能给带市场前缀的代码；不归一化两边就 join 不上。"""
    from scanner.sources.fund_premium import _is_lof, _norm_code, _shape

    assert _norm_code("sz160719") == "160719"
    assert _norm_code("160719.SZ") == "160719"
    assert _norm_code(160719) == "160719"
    assert _norm_code("") == ""
    # v4 补的 169 段：新浪 350 个可用市价里有 5 个落在这里
    assert _is_lof("169105") and _shape("169105") == "LOF"
    ok("代码归一化覆盖带前缀/带后缀/整数三种写法，169 段被认作 LOF")



def test_net_of_fee_and_tiering():
    """净收益 = 按卖一买入的折价 − 赎回费 − 佣金，并按三档可执行度排序。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"redeem_fee_pct": 1.5, "commission_pct": 0.03,
                                  "max_discount": 0})
    by = {o.code: o for o in res.opportunities}
    order = [o.code for o in res.opportunities]

    # 160719 最新价 0.960 / 卖一 0.961 → 按卖一折价 3.9%，3.9-1.53 = 2.37
    assert by["160719"].metrics["净收益(%)"] == 2.37, by["160719"].metrics
    # 160720 无盘口 → 回落最新价，2 - 1.53 = 0.47
    assert by["160720"].metrics["净收益(%)"] == 0.47, by["160720"].metrics
    # 赎回暂停那条即使折价 8% 也排最后
    assert order.index("160719") < order.index("501065"), order
    assert order.index("160720") < order.index("501065"), order
    # 赎回暂停 → 那条路走不通，不能印一个拿不到的收益
    for k in ("净收益(%)", "可投(万)", "预估(元)"):
        assert k not in by["501065"].metrics, by["501065"].metrics
    # 费率调高到吃光折价 → 掉到第 1 档，排在可兑现的之后
    res2 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              cfg={"redeem_fee_pct": 3.0, "max_discount": 0})
    o2 = [o.code for o in res2.opportunities]
    by2 = {o.code: o for o in res2.opportunities}
    assert by2["160720"].metrics["净收益(%)"] < 0
    # v4.4：删掉了 flags 里那句「扣掉滑点、赎回费…不够覆盖兑现成本」——
    # 动作行已经原样写过同一个数字，印两遍只是把读者往下推 49 个字。
    # 结论没丢，只是改由动作行单独承载，这里就验动作行。
    assert "兑现不划算" in by2["160720"].action, by2["160720"].action
    assert f'{by2["160720"].metrics["净收益(%)"]:+.2f}%' in by2["160720"].action
    assert not any("不够覆盖兑现成本" in f for f in by2["160720"].flags), \
        "thin 句又长回来了：同一个数字不该在动作行和提示里各印一遍"
    assert o2.index("160719") < o2.index("160720") < o2.index("501065"), o2
    assert any("成本吃光" in n for n in res2.notes), res2.notes
    # 口径说明必须出现，且写明 1.5% 是下限、要自己查费率表
    assert any("净收益(%)" in n and "以各基金合同为准" in n
               for n in res.footnotes), res.footnotes
    ok("净收益：按卖一计价、数值正确、按可执行度分三档、费率假设写进报告")


def test_slippage_folded_into_number():
    """买入价取卖一不取最新价，且只准往贵了取。"""
    import scanner.sources.fund_premium as fp
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0})
    by = {o.code: o for o in res.opportunities}

    # 160722：折价 2%，卖一 0.996 → 真实折价 0.4%，扣费后 -1.13 → 第 1 档
    wide = by["160722"]
    assert wide.metrics["折价率(%)"] == 2.0, wide.metrics
    assert wide.metrics["净收益(%)"] == -1.13, wide.metrics
    assert any("真能拿到 0.40%" in f for f in wide.flags), wide.flags
    # 净收益为负 → 不给它配仓位、不印一个虚构的亏损额（同「赎回暂停不印扣费后」）
    for k in ("可投(万)", "预估(元)"):
        assert k not in wide.metrics, wide.metrics
    # 纸面折价更小的 160720（2%，无价差）必须排在它前面
    order = [o.code for o in res.opportunities]
    assert order.index("160720") < order.index("160722"), order

    # 卖一低于最新价（陈旧/交叉快照）→ 不采信，回落最新价，不许凭空变好看
    prices = dict(fp._mock_lof_prices())
    prices["160720"] = dict(prices["160720"], bid=0.97, ask=0.90)
    res2 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              lof=(prices, fp._mock_lof_navs()),
                              cfg={"max_discount": 0})
    b = {o.code: o for o in res2.opportunities}["160720"]
    assert b.metrics["净收益(%)"] == 0.47, b.metrics
    ok("滑点折进数值：按卖一计价、倒挂盘口不采信、纸面幅度不再决定次序")


def test_sizing_and_absolute_profit():
    """可投金额 = min(本金上限, 成交额上限)，绝对收益决定档内次序。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0, "max_position_pct": 30,
                                  "turnover_participation_pct": 5})
    by = {o.code: o for o in res.opportunities}
    order = [o.code for o in res.opportunities]

    # 160719：成交 800 万 → 流动性上限 40 万，被本金上限 3 万卡住
    assert by["160719"].metrics["可投(万)"] == 3.0, by["160719"].metrics
    assert by["160719"].metrics["预估(元)"] == 711, by["160719"].metrics
    # 160721：成交 12 万 → 流动性上限 0.6 万，被成交额卡住
    assert by["160721"].metrics["可投(万)"] == 0.6, by["160721"].metrics
    assert by["160721"].metrics["预估(元)"] == 58, by["160721"].metrics
    # v4.4：这句和「日成交额低于阈值」那句合并了 —— 两句原本都在说同一个上限
    # 数字（08-08 的 161131：一句「卡在 0.6 万」、一句「约 0.6 万封顶」）。
    liq = [f for f in by["160721"].flags if "仓位卡在" in f]
    assert len(liq) == 1, by["160721"].flags
    assert "0.60 万" in liq[0] and "58 元" in liq[0], liq[0]
    assert not any("万封顶" in f for f in by["160721"].flags), \
        "同一个上限数字又被印了两遍"

    # 核心回归：160720 折价 2% < 160721 折价 3%，但 141 元 > 58 元 → 必须排在前面。
    # 按幅度排会把次序颠倒，这正是 08-08 实盘推翻上一版排序的那件事。
    assert by["160720"].metrics["预估(元)"] == 141, by["160720"].metrics
    assert by["160720"].metrics["折价率(%)"] < by["160721"].metrics["折价率(%)"]
    assert order.index("160720") < order.index("160721"), order

    # 组合口径：3+3+0.6 = 6.6 万，711+141+58.2 ≈ 910 元
    note = next((n for n in res.notes if "组合口径" in n), None)
    assert note and "前 3 条" in note and "910 元" in note, note
    # v4.4：banner 只留今天才成立的数字，「换的是什么 + 怎么压掉」下沉 footnotes。
    # 「合计预估 N 元」必须留在 banner —— 不变量②要从这句里解析 N 出来复算。
    assert "14:45" not in note and "15:00" not in note, note
    assert any("14:45" in f and "15:00" in f for f in res.footnotes), res.footnotes
    ok("仓位与绝对收益：两道上限都生效、按元排推翻按幅度排、组合合计与操作口径写明")


def _greedy_total(res, capital):
    """按报告里**印出来的**可投/净收益贪心铺满，返回 (合计, 用到几条, 最后一条是否只填一半)。"""
    picks = [o for o in res.opportunities if "预估(元)" in o.metrics
             and o.metrics["预估(元)"] > 0]
    left, total, used, part = capital, 0.0, 0, False
    for o in picks:
        if left <= 0:
            break
        full = float(o.metrics["可投(万)"]) * 1e4
        alloc = min(full, left)
        total += alloc * float(o.metrics["净收益(%)"]) / 100
        left -= alloc
        used += 1
        part = alloc < full
    return total, used, part


def test_printed_numbers_multiply_out():
    """报告里印出来的三个数必须自己乘得通：可投 × 净收益 = 预估。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0})
    checked = 0
    for o in res.opportunities:
        if "预估(元)" not in o.metrics:
            continue
        wan = float(o.metrics["可投(万)"])
        net = float(o.metrics["净收益(%)"])
        assert o.metrics["预估(元)"] == int(round(wan * 1e4 * net / 100)), \
            (o.code, o.metrics)
        checked += 1
    assert checked >= 3, checked

    # 组合合计也必须能由印出来的数复算出来。
    # 注意不能写成「等于各条预估之和」—— 本金用完时最后一条只填一部分，
    # 那个等式不成立（实盘 08-08 差 15 元）。这里按贪心复算。
    total, used, part = _greedy_total(res, 100000)
    assert not part, "本金没用完，这个用例测不到部分填充"
    note = next(n for n in res.notes if "组合口径" in n)
    assert f"{total:,.0f} 元" in note and f"前 {used} 条" in note, note
    ok("报告内自洽：可投 × 净收益 = 预估，组合合计可由印出来的数复算")


def test_partial_last_position_is_spelled_out():
    """本金在最后一条上用完时，必须说明它只填了一部分，否则直接相加对不上。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    # 单只上限抬到 60% → 两条各 6 万 > 10 万本金，第二条只能填 4 万
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0, "max_position_pct": 60})
    total, used, part = _greedy_total(res, 100000)
    assert part, "用例没造出部分填充的情形"
    note = next(n for n in res.notes if "组合口径" in n)
    # v4.4：banner 只留数字（哪条、填了多少、上限多少），
    # 「为什么合计比直接相加少」那句解释归 footnotes。
    assert "本金到这里用完" in note and "该条上限" in note, note
    assert any("直接相加" in f for f in res.footnotes), res.footnotes
    assert "40,000 元" in note, note          # 第二条实际只填进去的钱
    assert f"{total:,.0f} 元" in note, note
    # 各条预估直接相加 ≠ 合计，正是这句话要解释的差额
    naive = sum(o.metrics["预估(元)"] for o in res.opportunities
                if "预估(元)" in o.metrics and o.metrics["预估(元)"] > 0)
    assert naive > total, (naive, total)
    ok("最后一条只填一半时说明白，合计与「各条预估直接相加」的差额有交代")


def test_trade_outside_quote_is_flagged():
    """最新价掉在买一/卖一之外 = 成交与盘口不同时点，滑点是推的，要说明。"""
    import scanner.sources.fund_premium as fp
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    # 实盘 08-08 的 165516 / 501098 形态：最新价**低于买一**（收盘集合竞价价
    # 配盘中最后一档报价）。上一版按阈值的价差提示对它们完全噤声。
    prices = dict(fp._mock_lof_prices())
    prices["160720"] = dict(prices["160720"], bid=0.984, ask=0.986)
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             lof=(prices, fp._mock_lof_navs()),
                             cfg={"max_discount": 0})
    o = {x.code: x for x in res.opportunities}["160720"]
    # v4.6.1：这里卖一 > 最新价，滑点那句本来就会印，「（按收盘盘口推）」是挂在
    # 它后面的限定 —— 事实说到了，但只说一遍。独立成句的那条只在滑点句没触发时
    # 才是唯一信源（倒挂快照那种），由 test_off_book_flag_is_bound_to_… 钉住。
    assert any("建仓要吃卖一" in f and "按收盘盘口推" in f for f in o.flags), o.flags
    assert not any("在买一/卖一" in f for f in o.flags), \
        f"滑点句已带后缀，再独立成句就是同一件事说两遍：{o.flags}"
    assert any("不是同一时点" in f for f in res.footnotes), res.footnotes
    # 数值仍按卖一算（悲观口径不因为快照可疑就放松）
    assert o.metrics["净收益(%)"] == round((1 - 0.986) * 100 - 1.53, 2), o.metrics
    # 价差本身只有 0.20%，低于 spread_alert_pct → 老的价差提示不会响
    assert not any("买卖价差" in f for f in o.flags), o.flags

    # 正常盘口（最新价在区间内）不该误报
    ok2 = {x.code: x for x in _fund_premium_with(
        [_mock_spot_df(), _mock_daily_df()], cfg={"max_discount": 0}
    ).opportunities}["160719"]
    assert not any("落在买一/卖一" in f for f in ok2.flags), ok2.flags
    ok("成交价落在盘口之外时明说滑点是推的；价差低于阈值也不再静默")


def test_action_word_follows_the_verdict():
    """净收益为负的行不许再说「折价买入」。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0})
    dead = {o.code: o for o in res.opportunities}["160722"]   # 净收益 -1.13%
    assert "折价买入" not in dead.action, dead.action
    assert "兑现不划算" in dead.action and "-1.13%" in dead.action, dead.action
    live = {o.code: o for o in res.opportunities}["160719"]
    assert "折价买入" in live.action, live.action
    ok("动作词跟着结论走：净收益为负的不再说「买入」，为正的照旧")


def test_spread_flag_does_not_ask_to_double_count():
    """价差已折进净收益，折价侧不能再让人「按真正买到的价格重算」。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0})
    by = {o.code: o for o in res.opportunities}
    # v4.5：滑点那句已经印了「最新价 / 卖一」，价差句再印「买一/卖一」是重复。
    # 提示预算只有 3 句，退市线那句要进来就得有一句出去 —— 出去的是重复度最高的。
    cold = by["160721"]                      # 折价侧，滑点触发 → 价差句不再重复出现
    assert any("建仓要吃卖一" in f for f in cold.flags), cold.flags
    assert not any("买卖价差" in f for f in cold.flags), cold.flags
    # 滑点没触发（卖一==最新价）时，价差句仍是唯一的价差信息源，照印
    import scanner.sources.fund_premium as fp2
    prices = {k: dict(v) for k, v in fp2._mock_lof_prices().items()}
    prices["160721"] = dict(prices["160721"], bid=0.955, ask=0.97)   # ask == px
    res3 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              lof=(prices, fp2._mock_lof_navs()),
                              cfg={"max_discount": 0})
    c3 = {o.code: o for o in res3.opportunities}["160721"]
    sp = [f for f in c3.flags if "买卖价差" in f]
    assert sp, c3.flags
    assert "已经" in sp[0] and "折进净收益" in sp[0], sp[0]
    assert "重算" not in sp[0], sp[0]
    assert any("别在净收益上再扣一遍" in f for f in res3.footnotes), res3.footnotes
    # 溢价侧没做这层折算，老措辞保留
    prem = [o for o in res.opportunities if "溢价" in o.action and
            any("买卖价差" in f for f in o.flags)]
    for o in prem:
        f = next(x for x in o.flags if "买卖价差" in x)
        assert "重算" in f and "已经" not in f, f
    ok("价差提示按口径分侧：折价侧说深度不说重算，溢价侧保留原措辞")


def test_min_profit_yuan_filters_on_money():
    """门槛卡在元上：薄折价但成交额大的留下，厚折价但放不进钱的滤掉。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0, "min_profit_yuan": 100})
    codes = {o.code for o in res.opportunities}
    # 160720 折价 2%（幅度最小的一档）→ 141 元，留下
    assert "160720" in codes, codes
    # 160721 折价 3%（幅度更大）→ 只值 58 元，滤掉。按幅度卡会得到相反的结果。
    assert "160721" not in codes, codes
    assert any("min_profit_yuan" in n and "卡的是元不是幅度" in n
               for n in res.notes), res.notes
    # 默认 0 = 不过滤，行为与上一版一致
    res0 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              cfg={"max_discount": 0})
    assert "160721" in {o.code for o in res0.opportunities}
    assert not any("min_profit_yuan" in n for n in res0.notes), res0.notes
    ok("按元的门槛：留下薄折价里能放钱的、滤掉厚折价里放不进钱的，默认不生效")


def test_no_liquidity_data_is_declared():
    """clist 兜底没有成交额 → 可投只按本金算，必须明说没折过流动性。"""
    import scanner.sources.fund_premium as fp
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    prices = {k: dict(v, amt=None, bid=None, ask=None)
              for k, v in fp._mock_lof_prices().items()}
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             lof=(prices, fp._mock_lof_navs()),
                             cfg={"max_discount": 0})
    o = {x.code: x for x in res.opportunities}["160719"]
    assert o.metrics["可投(万)"] == 3.0, o.metrics       # 只剩本金上限
    assert "日成交额(万)" not in o.metrics, o.metrics
    assert any("未折流动性" in f for f in o.flags), o.flags
    assert any("clist 兜底" in f for f in res.footnotes), res.footnotes
    ok("缺成交额时如实声明「可投」未按流动性折算，不假装算过")


def test_liquidity_and_spread_flags():
    """冷门 LOF 的纸面折价买不到 —— 成交额与买卖价差要单独提示。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"min_turnover_wan": 100, "spread_alert_pct": 0.5,
                                  "max_discount": 0})
    by = {o.code: o for o in res.opportunities}

    cold = by["160721"]          # 日成交 12 万、价差 1%
    assert cold.metrics["日成交额(万)"] == 12.0, cold.metrics
    # v4.4：这条被「仓位卡在」那句吸收了（同一个 12 万、同一个 0.6 万上限），
    # 成交额本身仍在 metrics 里；「日成交额仅…」只在没被吸收时才补印。
    assert any("日成交额 12.0 万" in f and "仓位卡在" in f for f in cold.flags), cold.flags
    assert not any("日成交额仅" in f for f in cold.flags), cold.flags
    # v4.5：160721 滑点已触发 → 价差句被去重（见 test_spread_flag_...）。
    # 这里改验：价差信息没丢，它在滑点那句里以「最新价/卖一」的形式在
    assert any("0.97" in f and "0.975" in f for f in cold.flags), cold.flags

    liquid = by["160719"]        # 日成交 800 万、价差 0.21%
    assert liquid.metrics["日成交额(万)"] == 800.0
    assert not any("日成交额仅" in f for f in liquid.flags), liquid.flags
    assert not any("买卖价差" in f for f in liquid.flags), liquid.flags

    # clist 兜底拿不到盘口 → 不该硬凑出一个价差提示
    import scanner.sources.fund_premium as fp
    prices = {k: dict(v, bid=None, ask=None) for k, v in fp._mock_lof_prices().items()}
    res2 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              lof=(prices, fp._mock_lof_navs()),
                              cfg={"max_discount": 0})
    assert not any("买卖价差" in f for o in res2.opportunities for f in o.flags)
    ok("流动性与价差：冷门标的被打标、活跃标的不误报、缺盘口时噤声")


def test_premium_side_has_no_fee_column():
    """溢价侧走的是申购不是赎回，不该套用赎回费口径。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    prem = [o for o in res.opportunities if "溢价" in o.action]
    assert prem, "溢价侧一条都没有，用例失去意义"
    assert all("净收益(%)" not in o.metrics for o in prem), \
        [o.metrics for o in prem if "净收益(%)" in o.metrics]
    ok("扣费后一栏只出现在折价侧，不把赎回费错安到溢价上")


# ------------------------------------------------- 报告级自检（v4.3 新增）
# 上面的测试全是函数级（检查 SourceResult 数据结构），
# 下面这组是报告级（检查 render_console 渲染后的文本）。
# 这就是三轮修复里缺的那道门——它解析已印出来的文字，
# 所以口径变更、渲染 bug、或数据结构与文本之间的不一致都能抓住。

def test_report_verify_mock_default():
    """默认 mock 数据渲染后，四条不变量全部通过。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df
    from verify_report import verify_console

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0})
    ctx = Context(cfg={"capital": 100000, "accounts": 1}, today=dt.date(2026, 8, 8))
    text = render_console([res], ctx)
    errs = verify_console(text)
    assert not errs, "\n".join(errs)
    ok("报告级自检：默认 mock 渲染后四条不变量全部通过")


def test_report_verify_partial_fill():
    """本金在最后一条上用完时，组合合计仍能通过贪心复算。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df
    from verify_report import verify_console

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0, "max_position_pct": 60})
    ctx = Context(cfg={"capital": 100000, "accounts": 1}, today=dt.date(2026, 8, 8))
    text = render_console([res], ctx)
    errs = verify_console(text)
    assert not errs, "\n".join(errs)
    ok("报告级自检：部分填充场景贪心复算与印出的合计一致")


def test_report_verify_replay():
    """用实盘 08-08 的 9 条数据渲染，四条不变量全部通过。"""
    import types
    import scanner.sources.fund_premium as fp
    from verify_report import verify_console

    LOG = [
        ("161131", "易方达科润LOF",   1.001, 3.65,   21.0, 1.001, 1.015),
        ("160220", "国泰民益LOF",     4.499, 3.51,  230.3, 4.474, 4.499),
        ("501220", "行业轮动FOF",     0.960, 3.17,   11.7, None,  None),
        ("160215", "国泰价值LOF",     3.552, 2.87,   25.7, 3.552, 3.588),
        ("501098", "科创建信LOF",     1.727, 2.60,   23.1, 1.735, 1.740),
        ("169201", "浙商鼎盈LOF",     1.842, 2.52,   34.4, 1.842, 1.856),
        ("165516", "中信保诚周期LOF", 7.732, 2.52,   30.4, 7.734, 7.772),
        ("501217", "行业配置FOF",     1.041, 2.34,    1.9, 1.039, 1.060),
        ("506005", "科创板博时",      1.539, 2.27, 1575.6, None,  None),
    ]
    prices, navs = {}, {}
    for code, name, px, disc, wan, bid, ask in LOG:
        prices[code] = {"name": name, "px": px, "amt": wan * 1e4, "bid": bid, "ask": ask}
        navs[code] = {"nav": px / (1 - disc / 100), "basis": "最新-单位净值",
                      "redeem": "开放", "sub": "开放", "date": "2026-08-07"}
    navs["501217"]["redeem"] = "开放"  # 实盘中赎回开放但净收益为负

    empty = pd.DataFrame(columns=["代码", "名称", "最新价", "IOPV实时估值"])
    res = _fund_premium_with([empty, empty],
                             lof=(prices, navs),
                             cfg={"max_discount": 0, "sanity_median_pct": 99})
    ctx = Context(cfg={"capital": 100000, "accounts": 1}, today=dt.date(2026, 8, 8))
    text = render_console([res], ctx)
    errs = verify_console(text)
    assert not errs, "\n".join(errs)
    # 回归：501217 净收益为负，③号检查应已覆盖动作词方向
    action_lines = [l for l in text.split("\n") if "501217" in l and "动作" in l]
    if action_lines:
        assert "折价买入" not in action_lines[0], action_lines[0]
    ok("报告级自检：08-08 实盘 9 条数据渲染后四条不变量全部通过")


def test_hint_budget_bites():
    """⑤ 提示预算：v4.3 那条 5 句 326 字必须被拦下，缝合成一句也拦。"""
    from verify_report import verify_console

    base = ("================================================================\n"
            " 低容量套利扫描  2026-08-08   本金 100,000 × 1 户\n"
            "================================================================\n\n"
            "----------------------------------------------------------------\n"
            "▎LOF/QDII折溢价  （1 条）\n\n"
            "  [ ]观察  示例开放LOF（160719）\n"
            "      动作：折价 4.00% → 折价买入（需可赎回兑现）\n"
            "      形态: LOF | 净收益(%): 2.37 | 可投(万): 3.0 | 预估(元): 711\n"
            "      [!] {F}\n")

    # 现状：3 句、125 字 —— 通过
    now = ("折价 4.00% 按最新价 0.96 算，建仓要吃卖一 0.961 → 真能拿到 3.90%；"
           "日成交额 12.0 万 → 仓位卡在 0.60 万（5% 上限），2.37% 只值 711 元；"
           "买卖价差 1.03%（0.965/0.975），已经折进净收益")
    assert not verify_console(base.replace("{F}", now)), verify_console(base.replace("{F}", now))

    # 退回 v4.3 的原文 —— 必须报「句数」和「单句长度」两类
    old = ("折价兑现依赖赎回通道、赎回费与到账周期；"
           "折价 3.00% 是按最新价 0.97 算的，而建仓要吃卖一 0.975 —— 真正能拿到的折价是 "
           "2.50%，差的 0.50 个百分点在盘口里；"
           "仓位被成交额卡在 0.60 万（日成交额的 5%），0.97% 只值 58 元 —— "
           "百分比再高，放不进钱就换不成收益；"
           "日成交额仅 12.0 万元（低于 100 万），按 5% 成交额上限估算单笔建仓约 0.6 万封顶，"
           "再多就会把折价自己买没；"
           "买卖价差 1.03%（0.965/0.975）—— 这段**已经**折进上面的净收益里，别再扣一遍")
    errs = verify_console(base.replace("{F}", old))
    assert any("5 句 > 3 句" in e for e in errs), errs
    assert any("单句" in e for e in errs), errs

    # 绕法：用顿号把三件事缝成一句 —— 单句上限堵住
    stitched = ("折价 4.00% 按最新价 0.96 算、建仓要吃卖一 0.961、真能拿到 3.90%，"
                "而日成交额只有 12 万所以仓位卡在 0.6 万，"
                "另外买卖价差 1.03% 已经折进净收益里了")
    assert any("单句" in e for e in verify_console(base.replace("{F}", stitched)))
    ok("提示预算：5 句 326 字的老写法被拦下，缝成一句的绕法也被拦下")


def test_banner_not_attributed_to_previous_item():
    """栏目 banner 不能被算到上一栏最后一条的行内提示上。"""
    from verify_report import _parse_items

    text = ("▎配债一手党  （1 条）\n\n"
            "  [~]临近  某转债／某股份（123777）\n"
            "      动作：登记日买入正股（08-10）\n"
            "      [!] 抢权风险：正股3月涨 45% > 30%\n\n"
            "----------------------------------------------------------------\n"
            "▎LOF/QDII折溢价  （0 条）\n"
            "  [!] 折价侧 6 条按可执行度排序：4 条可兑现、1 条成本吃光、1 条赎回暂停\n"
            "  无\n")
    items = _parse_items(text)
    assert len(items) == 1, items
    assert items[0]["code"] == "123777"
    assert "抢权风险" in items[0]["flags_text"], items[0]
    assert "可执行度" not in items[0]["flags_text"], \
        "下一栏的 banner 又被记到上一栏最后一条上了"
    ok("栏目 banner 不再被误记为上一条的行内提示（④⑤ 读的就是这个字段）")


def test_footnotes_carry_what_flags_dropped():
    """行内删掉的长解释必须在 footnotes 里，且全报告去重。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0, "max_premium": 0})
    fns = res.footnotes
    assert len(fns) == len(set(fns)), "脚注没去重"
    # 每条被下沉的解释都要在
    for key in ("期限成本", "集思录 T-1 估值", "以各基金合同为准",
                "14:45", "min_profit_yuan", "退市线",
                # v4.6 下沉的三条
                "场内规模(万)", "既不等于开放，也不等于暂停"):
        assert any(key in f for f in fns), (key, fns)
    # 价差脚注是**条件性**的：默认 mock 里每条折价都触发了滑点，价差句被去重，
    # 那句脚注也就不该登记 —— 脚注只在正文真用到它时才出现。
    assert not any("别在净收益上再扣一遍" in f for f in fns), \
        "没有条目引用它，却把这条脚注印出来了"
    # 跨境有多条，但脚注只登记一次
    cross = [o for o in res.opportunities if "跨境" in str(o.metrics.get("形态", ""))]
    assert len(cross) >= 2, [o.code for o in cross]
    assert sum(1 for f in fns if f.startswith("形态里的「·跨境」")) == 1, fns
    # 「场内规模」这一列一出现脚注就得在 —— 上一版把它挂在「触线」分支里，
    # 没有一只票触线的日子，这一列会光秃秃地出现而口径一个字不印。
    assert any("场内规模(万)" in o.metrics for o in res.opportunities)
    assert any("沪市 501/502/505/506 这个接口不提供" in f for f in fns), fns
    ok("脚注承接了行内删掉的解释，多条共用同一句时只登记一次")


def test_allotment_zero_explains_itself():
    """配债 0 条时要分清「真没标的」「登记日取不到」「列位移」三种。"""
    import types
    from scanner.sources.base import Context
    from scanner.sources.cb_allotment import CBAllotmentSource
    from scanner.utils import clear_cache

    t = dt.date(2026, 8, 7)
    cols = ["债券代码", "债券简称", "申购日期", "申购代码", "申购上限", "正股代码",
            "正股简称", "正股价", "转股价", "转股价值", "债现价", "转股溢价率",
            "原股东配售-股权登记日", "原股东配售-每股配售额", "发行规模",
            "中签号发布日", "中签率", "上市时间", "信用评级"]

    def make(reg_offset=-1, reg_null=False):
        rows = []
        for i in range(200):
            d = t - dt.timedelta(days=30 + i)
            rows.append({"债券代码": f"1230{i:02d}", "债券简称": f"历史{i}转债",
                         "申购日期": d, "申购代码": "", "申购上限": 100.0,
                         "正股代码": "300001", "正股简称": "历史股份", "正股价": 10.0,
                         "转股价": 12.0, "转股价值": 95.0, "债现价": 110.0,
                         "转股溢价率": 15.0,
                         "原股东配售-股权登记日": (None if reg_null else
                                                  d + dt.timedelta(days=reg_offset)),
                         "原股东配售-每股配售额": 1.5, "发行规模": 5.0,
                         "中签号发布日": d, "中签率": 0.001, "上市时间": d,
                         "信用评级": "AA"})
        return pd.DataFrame(rows)[cols]

    def run(df):
        fake = types.ModuleType("akshare")
        fake.bond_zh_cov = lambda d=df: d
        fake.stock_zh_a_hist = lambda **kw: pd.DataFrame({"收盘": [10.0, 11.0]})
        sys.modules["akshare"] = fake
        clear_cache()
        ctx = Context(cfg={"lookahead_days": 10}, today=t, mock=False)
        return CBAllotmentSource(ctx).fetch()

    # ① 真空窗：不报 error，但要说清最晚的登记日是哪天
    a = run(make())
    assert not a.opportunities and not a.error, (a.error, a.opportunities)
    assert any("接口正常" in n and "已经过去" in n for n in a.notes), a.notes

    # ② 登记日整列取不到 → 报 error，明说不是「近期没有登记日」
    b = run(make(reg_null=True))
    assert b.error and "没有一行有可解析的股权登记日" in b.error, b.error
    assert any("不是「近期没有配债登记日」" in n for n in b.notes), b.notes

    # ③ 列位移（登记日接到「上市时间」那类字段上，比申购日晚约 20 天）
    c = run(make(reg_offset=+20))
    assert c.error and "列位可能移位" in c.error, c.error
    assert any("不可信" in n and "diag_allotment" in n for n in c.notes), c.notes

    # 有条目时不印取证说明 —— 报告不该为了自证而变长
    d = run(make(reg_offset=+31))     # 登记日落进 [今天, 今天+10]
    assert d.opportunities, "构造失败：这批应该出条"
    assert not any("本栏 0 条" in n for n in d.notes), d.notes
    ok("配债 0 条自证：真空窗 / 登记日取不到 / 列位移 三者在报告里不再长得一样")


def test_every_script_compiles():
    """每个 .py 都要能编译 —— 诊断脚本没人 import，语法错会一路活到你联网跑的那天。

    diag_allotment.py 就是这么来的：它是配债栏出 0 条时唯一的取证工具，
    而整套自测里没有任何一处会碰到它。等到周一盘中才发现它自己崩了，
    等于白跑一次。
    """
    import py_compile

    root = pathlib.Path(__file__).resolve().parent
    files = sorted(p for p in root.rglob("*.py")
                   if "__pycache__" not in str(p))
    assert len(files) >= 15, f"只扫到 {len(files)} 个文件，路径不对？"
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True, cfile=str(f) + "c")
        except py_compile.PyCompileError as e:
            raise AssertionError(f"{f.relative_to(root)} 编译不过：{e}") from None
        finally:
            pathlib.Path(str(f) + "c").unlink(missing_ok=True)
    # 诊断脚本必须在名单里，否则这条测试是空转的
    names = {f.name for f in files}
    for must in ("diag_allotment.py", "diag_sources.py", "diag_lof_coverage.py",
                 "replay_20260808.py", "verify_report.py"):
        assert must in names, (must, sorted(names))
    ok(f"全部 {len(files)} 个脚本编译通过（含四个不会被 import 的诊断/回放脚本）")


def test_delist_line_uses_real_shares_not_turnover():
    """退市线判断必须用场内份额×净值，不能拿日成交额猜；沪市段要说明未覆盖。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 0, "max_premium": 0})
    by = {o.code: o for o in res.opportunities}

    # 160721：份额 600 万份 × 单位净值 1.0 = 600 万 < 1000 万 → 出提示。
    # 口径是份额 × **净值**不是 × 市价：折价票市价低于净值，用市价算会把规模
    # 算小，正好在线附近把「安全」误报成「触线」。
    assert by["160721"].metrics["场内规模(万)"] == 600, by["160721"].metrics
    assert any("退市线" in f for f in by["160721"].flags), by["160721"].flags
    # 160719：4,800 万，远在线上 → 有数没提示
    assert by["160719"].metrics["场内规模(万)"] == 5000, by["160719"].metrics
    assert not any("退市线" in f for f in by["160719"].flags), by["160719"].flags

    # 核心：160721 日成交额 12 万 < 160722 的 50 万，但被标退市线的是 160721
    # 而 160722 场内 1,960 万安全 —— 说明判据是份额不是成交额
    assert by["160722"].metrics["日成交额(万)"] > by["160721"].metrics["日成交额(万)"]
    assert not any("退市线" in f for f in by["160722"].flags), by["160722"].flags

    # 沪市段（501065）深交所这张表没有 → 既不给数也不给提示，不能默认「安全」
    assert "场内规模(万)" not in by["501065"].metrics, by["501065"].metrics
    assert not any("退市线" in f for f in by["501065"].flags), by["501065"].flags

    # 份额整路缺失（老版 akshare / 接口挂）→ 折价数值一个都不受影响
    res2 = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                              cfg={"max_discount": 0}, shares={})
    b2 = {o.code: o for o in res2.opportunities}
    assert not any("场内规模(万)" in o.metrics for o in res2.opportunities)
    assert b2["160721"].metrics["预估(元)"] == by["160721"].metrics["预估(元)"]
    ok("退市线用场内份额×净值判定，与成交额脱钩；沪市未覆盖、份额缺失都不影响数值")


def test_fund_announcements_use_the_fund_column():
    """基金退出线索必须查巨潮「基金」栏目，动作词也不能沿用要约收购那套。"""
    import types
    from scanner.sources.base import Context
    from scanner.sources.event_arb import EventArbSource
    import scanner.sources.event_arb as ea

    calls = []

    def fake_search(symbol="", market="", keyword="", start_date="", end_date=""):
        calls.append((market, keyword))
        return ea._mock_df(keyword)

    fake = types.ModuleType("akshare")
    fake.stock_zh_a_disclosure_report_cninfo = fake_search
    sys.modules["akshare"] = fake
    saved = ea._INTER_CALL_GAP
    ea._INTER_CALL_GAP = 0
    try:
        ctx = Context(cfg={"event_arb": {
            "keywords": ["要约收购"],
            "fund_keywords": ["终止上市", "投资者选择期"],
        }}, today=dt.date(2026, 8, 8))
        res = EventArbSource(ctx).fetch()
    finally:
        ea._INTER_CALL_GAP = saved
        sys.modules.pop("akshare", None)

    # 这是这次最容易踩空的点：基金公告不在「沪深京」库里，
    # 用默认栏目搜「终止上市」一条都搜不到，且和「今天没有」长得一样
    assert ("沪深京", "要约收购") in calls, calls
    assert ("基金", "终止上市") in calls, calls
    assert ("基金", "投资者选择期") in calls, calls
    assert not any(m == "沪深京" and k == "终止上市" for m, k in calls), calls

    by = {o.code: o for o in res.opportunities}
    assert "对价 vs 现价" in by["600123"].action, by["600123"].action
    for code in ("161130", "501050"):
        assert "退出方式" in by[code].action, by[code].action
        assert "对价" not in by[code].action, by[code].action
        assert any("折价收敛" in f for f in by[code].flags), by[code].flags
    ok("基金退出线索走「基金」栏目，动作词与要约收购那套分开")


# ============================ v4.6 六处修复 ============================
def test_unknown_gate_is_its_own_tier():
    """「申赎状态没取到」不能算进「可兑现」。

    起因是 08-09 实盘那句「溢价侧 37 条…27 条可兑现」—— 那 27 条全是跨境 ETF，
    申购状态一条都没取到。而溢价 24% 的 QDII 恰恰大概率是限额或暂停，
    等于在全页最显眼的一行上断言了反面。
    """
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    by = {o.code: o for o in res.opportunities}

    # 513100 走 ETF 帧 → 没有申购状态
    etf = by["513100"]
    assert "申购状态未取到" in etf.action, etf.action
    assert "若申赎开放可申购套利" not in etf.action, etf.action
    # 161130 走日行情帧 → 折价侧同理，不许再写「折价买入」
    disc = by["161130"]
    assert "赎回状态未取到" in disc.action, disc.action
    assert "折价买入" not in disc.action, disc.action
    # 状态未取到的不给算净收益：印一个数等于替读者认定这条路走得通
    assert "净收益(%)" not in disc.metrics, disc.metrics
    assert "预估(元)" not in disc.metrics, disc.metrics

    # 计数分档：两侧都要把「未取到」单列出来
    prem_note = [n for n in res.notes if n.startswith("溢价侧") and "可执行度" in n]
    disc_note = [n for n in res.notes if n.startswith("折价侧") and "可执行度" in n]
    assert prem_note and "申购状态未取到" in prem_note[0], res.notes
    assert disc_note and "赎回状态未取到" in disc_note[0], res.notes
    # 「未取到」不许被算进「可兑现」那个数
    assert "1 条可兑现、3 条申购状态未取到" in prem_note[0], prem_note[0]
    # 脚注要说清「没取到既不等于开放也不等于暂停」
    assert any("既不等于开放，也不等于暂停" in f for f in res.footnotes), res.footnotes
    ok("申赎状态未取到自成一档，不再被并进「可兑现」，也不给它算净收益")


def test_demote_switch_moves_order_but_never_the_count():
    """开关只管排序。无论怎么设，都不许把「没取到」数进「可兑现」。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    # 一只申购确认开放的境内 LOF（溢价 5%）+ 两只申购状态取不到的跨境 ETF（溢价更高）
    prices = {"160801": {"name": "境内溢价LOF", "px": 1.05, "amt": 3e6,
                         "bid": None, "ask": None}}
    navs = {"160801": {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                       "sub": "开放", "date": "2026-08-07"}}
    spot = pd.DataFrame([
        {"代码": "513100", "名称": "纳指ETF国泰", "最新价": 1.20, "IOPV实时估值": 1.0},
        {"代码": "159941", "名称": "纳指ETF广发", "最新价": 1.15, "IOPV实时估值": 1.0},
    ])
    daily = pd.DataFrame(columns=["基金代码", "基金简称", "市价", "折价率"])

    def order_and_note(demote):
        res = _fund_premium_with([spot, daily], lof=(prices, navs), shares={},
                                 cfg={"demote_unknown_gate": demote})
        prem = [o.code for o in res.opportunities if "溢价率(%)" in o.metrics]
        note = [n for n in res.notes if n.startswith("溢价侧") and "可执行度" in n][0]
        return prem, note

    on, note_on = order_and_note(True)
    off, note_off = order_and_note(False)

    # 开着：确认开放的排最前，哪怕它幅度最小
    assert on[0] == "160801", on
    # 关掉：回到纯按幅度，跨境 ETF 顶回前面
    assert off[0] == "513100" and off.index("160801") > 0, off
    # 但两种设置下，计数一字不差 —— 那是对错，不是偏好
    assert "1 条可兑现、2 条申购状态未取到" in note_on, note_on
    assert "1 条可兑现、2 条申购状态未取到" in note_off, note_off
    ok("demote 开关只改次序，不改分档计数（没取到永远不算可兑现）")


def test_premium_blocked_has_no_discount_wording():
    """溢价 + 申购暂停，不该挂一句「折价要等开放日才谈得上兑现」。"""
    import scanner.sources.fund_premium as fp
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    prices = {"160801": {"name": "溢价申购暂停LOF", "px": 1.08, "amt": 3e6,
                         "bid": None, "ask": None}}
    navs = {"160801": {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                       "sub": "暂停", "date": "2026-08-07"}}
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             lof=(prices, navs), shares={})
    o = [x for x in res.opportunities if x.code == "160801"][0]
    assert "申购暂停" in o.action, o.action
    assert not any("折价" in f for f in o.flags), o.flags
    ok("溢价条目不再被安上折价口吻的封闭期提示")


def test_on_floor_scale_never_vanishes_silently():
    """场内规模那一列消失时必须出声 —— 沉默和「都在线上」长得一模一样。"""
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    # 份额取到：列在、脚注在（哪怕一只都没触线）
    got = _fund_premium_with([_mock_spot_df(), _mock_daily_df()])
    assert any("场内规模(万)" in o.metrics for o in got.opportunities)
    assert any("沪市 501/502/505/506 这个接口不提供" in f for f in got.footnotes), \
        "这一列印出来了，却没说它只覆盖深市"
    # 整数展示，不是 2486.0
    vals = [o.metrics["场内规模(万)"] for o in got.opportunities
            if "场内规模(万)" in o.metrics]
    assert all(isinstance(v, int) for v in vals), vals

    # 份额没取到：列消失，但必须有一句栏目级说明
    lost = _fund_premium_with([_mock_spot_df(), _mock_daily_df()], shares={})
    assert not any("场内规模(万)" in o.metrics for o in lost.opportunities)
    hit = [n for n in lost.notes if "退市线本次未判定" in n]
    assert hit, lost.notes
    assert "不代表它们都在线上" in hit[0], hit[0]
    ok("场内规模取不到时明说未判定，不再与「都在线上」混为一谈")


def test_hint_budget_holds_on_worst_case():
    """一条同时命中 滑点/流动性/盘口之外/退市线/跨境 时仍在 3 句预算内。

    这不是理论组合：08-09 实盘的 165516 已经 3 句 140 字、160215 是
    滑点+流动性+退市线，只差一个「最新价落在盘口外」。而这个组合恰恰出现在
    深市小盘跨境 LOF 上 —— 正是新退市规则针对的那一类。
    """
    from verify_report import verify_console
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    prices = {"160901": {"name": "示例纳指LOF", "px": 0.970, "amt": 120_000,
                         "bid": 0.975, "ask": 0.980}}      # px 低于买一 → 盘口之外
    navs = {"160901": {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                       "sub": "开放", "date": "2026-08-07"}}
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             lof=(prices, navs), shares={"160901": 6.0e6})
    o = [x for x in res.opportunities if x.code == "160901"][0]

    assert len(o.flags) <= 3, o.flags
    assert len("；".join(o.flags)) <= 150, len("；".join(o.flags))
    # 四个信号一个都不能丢：盘口那条压成了滑点句的后缀
    joined = "；".join(o.flags)
    assert "建仓要吃卖一" in joined and "（按收盘盘口推）" in joined, joined
    assert "仓位卡在" in joined and "退市线" in joined, joined
    assert o.metrics["形态"] == "LOF·跨境", o.metrics
    assert any("按收盘盘口推" in f for f in res.footnotes), res.footnotes

    errs = verify_console(render_console([res], Context(cfg={"capital": 100000})))
    assert not errs, errs
    ok("最坏组合下行内提示仍在 3 句预算内，四个信号一个不丢")


def test_portfolio_total_only_counts_visible_rows():
    """max_discount 截断时，印出的「合计预估」必须能由看得见的数复算出来。"""
    from verify_report import verify_console
    from scanner.sources.fund_premium import _mock_daily_df, _mock_spot_df

    prices, navs = {}, {}
    for i in range(12):                       # 每条可投都很小 → 3 条铺不满本金
        c = f"1607{i:02d}"
        prices[c] = {"name": f"薄折价{i:02d}", "px": 0.96 - i * 0.001, "amt": 3e5,
                     "bid": None, "ask": None}
        navs[c] = {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                   "sub": "开放", "date": "2026-08-07"}
    res = _fund_premium_with([_mock_spot_df(), _mock_daily_df()],
                             cfg={"max_discount": 3}, lof=(prices, navs), shares={})

    note = [n for n in res.notes if n.startswith("组合口径")][0]
    assert "本金还剩" in note and "截断" in note, note
    errs = verify_console(render_console([res], Context(cfg={"capital": 100000})))
    assert not errs, errs                      # ② 以前必然判负，现在应当自洽
    ok("组合合计只统计可见条目，截断时明说本金没铺完")


def test_event_arb_truncation_claim_is_true():
    """「只列出最近的 N 条」这句话要么成立，要么别说。"""
    import types
    from scanner.sources.base import Context
    from scanner.sources.event_arb import EventArbSource
    import scanner.sources.event_arb as ea

    rows = [{"代码": f"60{i:04d}", "简称": f"票{i}",
             "公告标题": "关于要约收购的公告",
             "公告时间": f"2026-08-0{i % 8 + 1} 10:00:00",
             "公告链接": f"http://x/{i}"} for i in range(6)]

    def fake(symbol="", market="", keyword="", start_date="", end_date=""):
        return pd.DataFrame(rows if keyword == "要约收购" else [])

    fk = types.ModuleType("akshare")
    fk.stock_zh_a_disclosure_report_cninfo = fake
    sys.modules["akshare"] = fk
    saved = ea._INTER_CALL_GAP
    ea._INTER_CALL_GAP = 0
    try:
        def run(max_items):
            ctx = Context(cfg={"event_arb": {"keywords": ["要约收购"],
                                             "fund_keywords": [],
                                             "window_days": 30,
                                             "max_items": max_items}},
                          today=dt.date(2026, 8, 9))
            return EventArbSource(ctx).fetch()

        exact = run(6)                         # 恰好 6 条 → 一条都没砍
        assert len(exact.opportunities) == 6
        assert not any("只列出最近" in n for n in exact.notes), exact.notes

        cut = run(3)                           # 砍了 → 必须说，且留下的确实是最新的
        assert len(cut.opportunities) == 3
        assert any("只列出最近的 3 条" in n for n in cut.notes), cut.notes
        # 6 条公告日期是 08-01…08-06，砍到 3 条必须留下最新的三天
        kept = {o.action_date for o in cut.opportunities}
        assert kept == {dt.date(2026, 8, 4), dt.date(2026, 8, 5),
                        dt.date(2026, 8, 6)}, sorted(kept)
    finally:
        ea._INTER_CALL_GAP = saved
        sys.modules.pop("akshare", None)
    ok("事件套利：恰好等于上限时不谎报截断，真截断时留下的确实是最新的")


def test_config_normalises_both_keyword_lists():
    """fund_keywords 写成字符串不能被 list() 拆成单字。"""
    from scanner.config import _sanitize

    cfg = _sanitize({"event_arb": {"keywords": "要约收购", "fund_keywords": "终止上市"}})
    assert cfg["event_arb"]["keywords"] == ["要约收购"]
    assert cfg["event_arb"]["fund_keywords"] == ["终止上市"], \
        "漏归一化 → list('终止上市') 会拆成四个单字关键词去打巨潮"
    ok("两个关键词列表都做字符串归一，不会退化成单字检索")


# =====================================================================
# v4.6.1
# =====================================================================
def _one_lof(px, amt, bid=None, ask=None, nav=1.0, code="160719",
             redeem="开放", sub="开放"):
    """造一只 LOF 跑一遍，返回它的 Opportunity。"""
    prices = {code: {"name": "示例LOF", "px": px, "amt": amt,
                     "bid": bid, "ask": ask}}
    navs = {code: {"nav": nav, "basis": "最新-单位净值", "redeem": redeem,
                   "sub": sub, "date": "2026-08-07"}}
    res = _fund_premium_with([pd.DataFrame(), pd.DataFrame()],
                             lof=(prices, navs), shares={})
    return {o.code: o for o in res.opportunities}[code]


def test_off_book_flag_is_bound_to_the_slippage_sentence():
    """盘口异常那句的判据是「滑点那句说没说过」，不是「仓位卡没卡住」。

    原来的 `elif` 接在「仓位卡在 X 万」那个 if 上，两个方向都错：

      ① 最新价**高于卖一**（倒挂快照）+ 仓位又被成交额卡住 → 走了那个 if，
         盘口异常一个字不印。读者只看到「买卖价差…已经折进净收益」，
         而这个盘口本身就是坏的 —— 这正是 08-09 那份没露相的组合：
         触发 off_book 的三只恰好都同时被成交额卡住。
      ② 滑点那句已经带了「（按收盘盘口推）」后缀 + 成交额充裕 → elif 反而成立，
         同一件事印两句 96 字。

    两个方向各钉一条。
    """
    # ① 最新价 0.960 > 卖一 0.955，成交额 12 万 → 仓位被卡
    inverted = _one_lof(px=0.960, amt=120_000, bid=0.950, ask=0.955)
    assert any("在买一/卖一" in f for f in inverted.flags), \
        f"倒挂盘口没说出来：{inverted.flags}"
    assert any("仓位卡在" in f for f in inverted.flags), inverted.flags
    # 盘口那句把买一卖一都印了，价差那句该让位（否则凑得出 4 句撞破 ⑤）
    assert not any("买卖价差" in f for f in inverted.flags), \
        f"盘口那句已印了两个价，价差句是重复：{inverted.flags}"

    # ② 最新价 0.940 < 买一 0.950，成交额 1000 万 → 仓位不被卡
    off_low = _one_lof(px=0.940, amt=10_000_000, bid=0.950, ask=0.960)
    slip = [f for f in off_low.flags if "建仓要吃卖一" in f]
    assert slip and "按收盘盘口推" in slip[0], off_low.flags
    assert not any("在买一/卖一" in f for f in off_low.flags), \
        f"滑点那句已带后缀，独立成句就是说两遍：{off_low.flags}"

    # 盘口正常时两句都不该出现
    normal = _one_lof(px=0.960, amt=10_000_000, bid=0.959, ask=0.961)
    assert not any("在买一/卖一" in f for f in normal.flags), normal.flags
    ok("盘口异常那句跟着滑点句走：倒挂时不再静默，滑点已说时不再重复")


def test_worst_case_with_inverted_quote_still_fits_the_budget():
    """倒挂 + 冷门 + 深市小规模：三个信号一个不丢，仍在 3 句预算内。

    这是 v4.6.1 新凑出来的最坏组合。修 elif 之前它只有 3 句是因为
    盘口那句被吞了；修完若不把价差句让掉，就会变成 4 句。
    """
    prices = {"160721": {"name": "示例倒挂冷门LOF", "px": 0.970, "amt": 120_000,
                         "bid": 0.960, "ask": 0.965}}
    navs = {"160721": {"nav": 1.0, "basis": "最新-单位净值", "redeem": "开放",
                       "sub": "开放", "date": "2026-08-07"}}
    res = _fund_premium_with([pd.DataFrame(), pd.DataFrame()],
                             lof=(prices, navs), shares={"160721": 6.0e6})
    o = res.opportunities[0]
    joined = "；".join(o.flags)
    assert len(o.flags) <= 3, f"{len(o.flags)} 句：{o.flags}"
    assert len(joined) <= 150, f"{len(joined)} 字：{joined}"
    for sig in ("在买一/卖一", "仓位卡在", "退市线"):
        assert sig in joined, f"信号「{sig}」丢了：{joined}"
    from verify_report import verify_console
    ctx = Context(cfg={"capital": 100000}, today=dt.date(2026, 8, 9))
    assert not verify_console(render_console([res], ctx))
    ok("倒挂盘口的最坏组合：盘口/流动性/退市线三句齐全且未撞破预算")


def test_emphasis_marks_never_reach_console_or_html():
    """`**重点**` 是 markdown 语法，不该原样打进终端和网页。

    起因：config 默认的 formats 是 ["console", "html"]，markdown 那一路根本没开，
    而提示与脚注是照 markdown 写的 —— 08-09 那份 HTML 里 7 处、console 里 4 处
    裸露的 `**…**`。三种渲染各有各的正确做法，所以在渲染层分开处理，
    而不是把源字符串里的记号删掉（删了 markdown 那一路就白写了）。
    """
    from scanner.report import render_markdown

    res = SourceResult(
        kind=Kind.FUND_PREM,
        opportunities=[Opportunity(kind=Kind.FUND_PREM, code="160719",
                                   name="示例LOF", action="折价 3.00% → **买入**",
                                   urgency=Urgency.WATCH,
                                   metrics={"口径": "**自算**"},
                                   flags=["提示里也有**重点**"])],
        notes=["栏目说明里的**重点**"], footnotes=["脚注里的**重点**"])
    ctx = Context(cfg={"capital": 100000}, today=dt.date(2026, 8, 9))

    con = render_console([res], ctx)
    assert "**" not in con, [l for l in con.split("\n") if "**" in l]
    assert "买入" in con and "重点" in con and "自算" in con, "去记号把字也删了"

    htm = render_html([res], ctx)
    assert "**" not in htm, "HTML 里仍有裸记号"
    assert htm.count("<b>重点</b>") == 3 and "<b>买入</b>" in htm, htm[:200]
    assert "<b>自算</b>" in htm

    # markdown 那一路原样保留 —— 它本来就是这个语法
    assert "**重点**" in render_markdown([res], ctx)
    ok("强调记号按格式分流：console 去掉、html 转 <b>、markdown 原样")


def test_event_arb_renders_newest_first():
    """事件套利按公告时间**倒序**渲染，与它的截断口径一致。

    v4.6 把截断改成了「按公告时间倒序留最新的 N 条」，渲染层却还在升序排，
    于是 08-09 那份里最新的一条（159717 基金合同终止，08-08）被摆在最下面，
    最旧的 08-05 反而打头。留下的是对的，摆出来的次序是反的。

    同时钉住另一侧：有硬性时点的栏目（打新/配债）必须仍是**升序** ——
    那里的日期是将到的时点，越早越急。
    """
    ev = [Opportunity(kind=Kind.EVENT, code="600000", name=n,
                      action="读公告", action_date=dt.date.fromisoformat(d),
                      urgency=Urgency.WATCH, date_desc=True)
          for d, n in (("2026-08-05", "最旧"), ("2026-08-08", "最新"),
                       ("2026-08-07", "居中"))]
    # 公告时间解析不出来的（date_desc + 无日期）排最后，不许顶到最前面
    ev.append(Opportunity(kind=Kind.EVENT, code="600001", name="无日期",
                          action="读公告", urgency=Urgency.WATCH, date_desc=True))
    assert [o.name for o in sorted(ev, key=lambda x: x.sort_key())] == \
        ["最新", "居中", "最旧", "无日期"]

    ipo = [Opportunity(kind=Kind.CB_IPO, code="123001", name=n, action="申购",
                       action_date=dt.date.fromisoformat(d), urgency=Urgency.SOON)
           for d, n in (("2026-08-12", "后天"), ("2026-08-10", "明天"))]
    ipo.append(Opportunity(kind=Kind.CB_IPO, code="123002", name="无日期",
                           action="申购", urgency=Urgency.SOON))
    assert [o.name for o in sorted(ipo, key=lambda x: x.sort_key())] == \
        ["明天", "后天", "无日期"], "有硬性时点的栏目必须仍是越早越前"

    src = _event_source_with_dates(["2026-08-05", "2026-08-08", "2026-08-07"])
    ctx = Context(cfg={"capital": 100000}, today=dt.date(2026, 8, 9))
    txt = render_console([src], ctx)
    order = [l for l in txt.split("\n") if "[ ]观察" in l]
    assert "示例2026-08-08" in order[0], order
    ok("事件套利渲染成倒序，与「按公告时间倒序截断」对齐；限时栏目仍是升序")


def _event_source_with_dates(dates):
    """用给定的公告日期跑一遍 event_arb（走 mock 分支之外的真实构造路径）。"""
    import scanner.sources.event_arb as ea
    rows = [{"代码": f"60000{i}", "简称": f"示例{d}",
             "公告标题": "关于要约收购的提示性公告",
             "公告时间": f"{d} 00:00:00",
             "公告链接": f"http://x/?t={d}"} for i, d in enumerate(dates)]
    saved = ea._mock_df
    ea._mock_df = lambda kw: (pd.DataFrame(rows) if kw == "要约收购"
                              else pd.DataFrame())
    try:
        ctx = Context(cfg={"event_arb": {"window_days": 30,
                                         "keywords": ["要约收购"],
                                         "fund_keywords": []}},
                      today=dt.date(2026, 8, 9), mock=True)
        return ea.EventArbSource(ctx).fetch()
    finally:
        ea._mock_df = saved


def test_premium_action_states_the_status_it_already_fetched():
    """申购状态已经取到时，动作词不许再写「若申赎开放」。

    走到这个分支说明状态查到了，metrics 里就明写着「申购: 开放」，分档也是按它
    数进「可兑现」那一档的 —— 再说「若申赎开放」是把已知条件退回成假设。
    这和折价侧被改掉的「（需可赎回兑现）」是同一个毛病，上一轮漏了溢价这一侧。
    """
    from scanner.sources.fund_premium import _GATE_CAPPED

    o = _one_lof(px=1.08, amt=3_000_000, nav=1.0, code="162411")
    assert "溢价" in o.action and o.metrics["申购"] == "开放"
    assert "若申赎开放" not in o.action, o.action
    assert "申购开放" in o.action, o.action

    cap = _one_lof(px=1.08, amt=3_000_000, nav=1.0, code="162412",
                   sub=_GATE_CAPPED)
    assert f"申购{_GATE_CAPPED}" in cap.action, \
        f"限制大额不能被说成开放：{cap.action}"

    # 没取到 / 暂停 两档的措辞不受影响
    unk = _one_lof(px=1.08, amt=3_000_000, nav=1.0, code="162413", sub="")
    assert "申购状态未取到" in unk.action, unk.action
    sus = _one_lof(px=1.08, amt=3_000_000, nav=1.0, code="162414", sub="暂停")
    assert "申购暂停" in sus.action, sus.action
    ok("溢价动作词照实说申购状态，不再把已知条件退回成假设")


def test_selfcheck_runs_on_every_report():
    """报告级自检不能只在 --mock 下跑 —— 它要抓的组合只有实盘数据凑得出来。"""
    import ast
    import pathlib

    src = pathlib.Path("run.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "verify_console"]
    assert calls, "run.py 里没有调用 verify_console"
    # 那次调用不能被包在 `if args.mock:` 里
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond = ast.dump(node.test)
        if "mock" not in cond:
            continue
        inner = [n for n in ast.walk(node)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "verify_console"]
        assert not inner, "verify_console 又被关回 --mock 分支里了"
    ok("报告级自检每次运行都跑，实盘不通过时降级退出码而不是不给报告")


# =============================== v5.0 ===============================
# 三件「把手里已有的字段用完」的事（零新数据源），外加一条取证。

_CB_COLS = ["债券代码", "债券简称", "申购日期", "申购代码", "申购上限", "正股代码",
            "正股简称", "正股价", "转股价", "转股价值", "债现价", "转股溢价率",
            "原股东配售-股权登记日", "原股东配售-每股配售额", "发行规模",
            "中签号发布日", "中签率", "上市时间", "信用评级"]


def _cb_row(**kw):
    """一行 bond_zh_cov 的默认值，测试里只覆盖关心的那几列。"""
    base = {"债券代码": "123001", "债券简称": "测试转债", "申购日期": None,
            "申购代码": "", "申购上限": 100.0, "正股代码": "300001",
            "正股简称": "测试股份", "正股价": 10.0, "转股价": 12.0,
            "转股价值": 95.0, "债现价": 100.0, "转股溢价率": 15.0,
            "原股东配售-股权登记日": None, "原股东配售-每股配售额": 1.0,
            "发行规模": 5.0, "中签号发布日": None, "中签率": 0.001,
            "上市时间": None, "信用评级": "AA"}
    base.update(kw)
    return base


def _run_source(cls, rows, today, cfg=None):
    """拿一张假 bond_zh_cov 跑某个数据源（不联网）。"""
    import types
    from scanner.sources.base import Context
    from scanner.utils import clear_cache

    df = pd.DataFrame(rows)[_CB_COLS]
    fake = types.ModuleType("akshare")
    fake.bond_zh_cov = lambda d=df: d
    fake.stock_zh_a_hist = lambda **kw: pd.DataFrame({"收盘": [10.0, 10.5]})
    sys.modules["akshare"] = fake
    clear_cache()
    base = {"lookahead_days": 10}
    base.update(cfg or {})
    return cls(Context(cfg=base, today=today, mock=False)).fetch()


def test_listing_reminder_is_a_date_not_a_price():
    """上市提醒：按交易日开窗、不被申购侧的门挡住、且一个价格字都不印。

    补它的理由是流程上的空白：申购、缴款两句说完就断片，而**唯一需要人做判断的
    那一天恰恰是上市日**（卖还是留）。补它的边界同样重要 —— 市面上那种
    「合理价值 137」是模型输出、没有出处，印进来这份报告就不再是「规则即收益」。
    """
    from scanner.sources.cb_ipo import CBIpoSource

    fri = dt.date(2026, 8, 7)          # 周五
    rows = [
        _cb_row(债券代码="113101", 债券简称="今日上市", 申购日期=fri - dt.timedelta(days=20),
                上市时间=fri),
        # 周一：日历天 +2 只到周日，会漏掉它；交易日 +2 到周二，稳稳盖住
        _cb_row(债券代码="123102", 债券简称="周一上市", 申购日期=fri - dt.timedelta(days=20),
                上市时间=fri + dt.timedelta(days=3)),
        _cb_row(债券代码="113103", 债券简称="下周五上市", 申购日期=fri - dt.timedelta(days=20),
                上市时间=fri + dt.timedelta(days=7)),
        _cb_row(债券代码="123104", 债券简称="早已上市", 申购日期=fri - dt.timedelta(days=60),
                上市时间=fri - dt.timedelta(days=40)),
        # 评级低、转股价值低：申购侧的两道门都会挡住它，但它今天上市
        _cb_row(债券代码="123105", 债券简称="低评级上市", 申购日期=fri - dt.timedelta(days=20),
                上市时间=fri, 信用评级="A+", 转股价值=80.0),
    ]
    res = _run_source(CBIpoSource, rows, fri,
                      cfg={"cb_ipo": {"min_rating": "AA+", "min_convert_value": 95}})

    listed = [o for o in res.opportunities if "上市" in o.action]
    got = sorted(o.code for o in listed)
    assert got == ["113101", "123102", "123105"], got
    assert [o.urgency for o in listed if o.code == "113101"] == [Urgency.TODAY]
    assert [o.urgency for o in listed if o.code == "123102"] == [Urgency.SOON], \
        "周一上市这条被交易日窗口漏掉了 —— 说明窗口又退回按日历天算"

    # 申购侧的门不该挡住上市提醒：债已经在手里了
    assert any(o.code == "123105" for o in listed), \
        "低评级/低转股价值的债今天上市却不提醒 —— 那两道门是「要不要申购」的判据"
    assert not [o for o in res.opportunities
                if o.code == "123105" and "申购" in o.action], "申购侧的门反而失效了"

    # 一个价格字都不许有
    banned = ["合理价值", "预期涨幅", "预计涨幅", "目标价", "估值", "建议卖",
              "参考价", "首日涨幅", "值多少", "预计价格"]
    for o in listed:
        blob = f"{o.action} {o.note} {o.metrics} {''.join(o.flags)}"
        hit = [w for w in banned if w in blob]
        assert not hit, f"上市提醒印了定价词汇 {hit}：{blob}"
    assert any("只报**时点**，不报价格" in f for f in res.footnotes), res.footnotes
    ok("上市提醒按交易日开窗、不受申购侧门槛影响，且只报时点不报价格")


def test_allotment_unit_follows_the_exchange():
    """最小配售单位分沪深：v4.6.1 及以前统一按 1000 元，深市被放大了 10 倍。

    沪市不足 1 手（10 张 / 1,000 元面值）才进位，「一手党」这个名字就是从这来的；
    深市走中国结算深圳分公司的配股指引，最小单位是 1 张（100 元面值）。
    """
    from scanner.sources.cb_allotment import CBAllotmentSource

    t = dt.date(2026, 8, 7)
    reg = t + dt.timedelta(days=2)
    # 列名里有连字符，做不了关键字实参 —— 统一用 ** 展开字典传
    rows = [
        _cb_row(债券代码="113201", 债券简称="沪市转债", 正股代码="600201", 正股价=9.50,
                申购日期=reg + dt.timedelta(days=1),
                **{"原股东配售-股权登记日": reg, "原股东配售-每股配售额": 0.8}),
        _cb_row(债券代码="123201", 债券简称="深市转债", 正股代码="300201", 正股价=9.50,
                申购日期=reg + dt.timedelta(days=1),
                **{"原股东配售-股权登记日": reg, "原股东配售-每股配售额": 0.8}),
        _cb_row(债券代码="990999", 债券简称="怪代码转债", 正股代码="600999", 正股价=9.50,
                申购日期=reg + dt.timedelta(days=1),
                **{"原股东配售-股权登记日": reg, "原股东配售-每股配售额": 0.8}),
    ]
    res = _run_source(CBAllotmentSource, rows, t)
    by_code = {o.code: o for o in res.opportunities}
    assert set(by_code) == {"113201", "123201", "990999"}, list(by_code)

    sh, sz = by_code["113201"], by_code["123201"]
    assert "配1手需持股" in sh.metrics and "配1张需持股" not in sh.metrics, sh.metrics
    assert "配1张需持股" in sz.metrics and "配1手需持股" not in sz.metrics, sz.metrics

    # ceil(1000/0.8)=1250 股 vs ceil(100/0.8)=125 股，正好差 10 倍
    assert sh.metrics["配1手需持股"] == 1250, sh.metrics
    assert sz.metrics["配1张需持股"] == 125, sz.metrics
    assert sh.metrics["占用市值(元)"] == 11875, sh.metrics
    assert sz.metrics["占用市值(元)"] == 1188, sz.metrics      # 125 × 9.5 = 1187.5
    assert "面值1000元（1手）" in sh.note and "面值100元（1张）" in sz.note

    # 认不出前缀时按沪市高估，但必须说出来 —— 不能静悄悄给一个买不到的数
    odd = by_code["990999"]
    assert any("认不出沪深" in f for f in odd.flags), odd.flags

    # 进位这件事只能写在口径里：它是竞争性的，事前不可知
    fn = " ".join(res.footnotes)
    assert "进位" in fn and "竞争性" in fn and "保证配满" in fn, res.footnotes
    ok("最小配售单位分沪深：深市不再按 1 手估，列名与注解跟着单位走")


def test_equity_weight_and_breakeven_are_pure_arithmetic():
    """含权量与打平溢价：两个恒等式，读者拿计算器就能验。

    含权量 = 每股配售额 ÷ 正股价；打平溢价 = 100 ÷ 含权量(%)。
    后者的推导：正股跌 1% 亏掉「占用市值 × 1%」，而占用市值 = 面值 ÷ 含权量，
    所以要靠转债这一侧赚回 1/含权量 的溢价才打平 —— 与面值无关，沪深通用。
    """
    from scanner.sources.cb_allotment import CBAllotmentSource

    t = dt.date(2026, 8, 7)
    reg = t + dt.timedelta(days=2)
    rows = [
        # 含权量 12.5% → 打平 8%
        _cb_row(债券代码="123301", 债券简称="高含权", 正股代码="300301", 正股价=10.0,
                申购日期=reg + dt.timedelta(days=1),
                **{"原股东配售-股权登记日": reg, "原股东配售-每股配售额": 1.25}),
        # 含权量 8.42% → 打平 12%
        _cb_row(债券代码="123302", 债券简称="低含权", 正股代码="300302", 正股价=9.50,
                申购日期=reg + dt.timedelta(days=1),
                **{"原股东配售-股权登记日": reg, "原股东配售-每股配售额": 0.8}),
    ]
    res = _run_source(CBAllotmentSource, rows, t)
    by_code = {o.code: o for o in res.opportunities}

    hi, lo = by_code["123301"], by_code["123302"]
    assert hi.metrics["含权量(%)"] == 12.5, hi.metrics
    assert lo.metrics["含权量(%)"] == 8.42, lo.metrics
    assert any("需上市溢价8%才打平" in f for f in hi.flags), hi.flags
    assert any("需上市溢价12%才打平" in f for f in lo.flags), lo.flags

    # 占用市值 = 面值 ÷ 含权量，和 需持股 × 正股价 是同一个数（取整误差内）
    for o in (hi, lo):
        w = o.metrics["含权量(%)"] / 100
        assert abs(100 / w - o.metrics["占用市值(元)"]) <= 2, (o.code, o.metrics)

    # 这句是警告标签，不是卖点：口径里必须点明触发时点的问题
    fn = " ".join(res.footnotes)
    assert "发行公告之后" in fn and "高开" in fn, res.footnotes
    assert "核准" in fn and "看不到" in fn, "埋伏窗口在核准之后这件事没说清"

    # 行内提示仍在预算内（⑤）
    from verify_report import verify_console
    from scanner.sources.base import Context
    ctx = Context(cfg={"capital": 100000, "accounts": 1}, today=t)
    assert not verify_console(render_console([res], ctx))
    ok("含权量与打平溢价是可复算的恒等式，且作为警告标签写进了口径")


def test_listing_column_shift_is_caught():
    """上市时间那一列错位时不许装作「本周没有新债上市」。

    和配债的登记日是同一类静默陷阱，而且更隐蔽：错位之后申购/缴款照常出条，
    栏目看着一切正常，只有上市提醒天天 0 条。语义不变量现成的 ——
    **上市日必在申购日之后**。
    """
    from scanner.sources.cb_ipo import CBIpoSource

    t = dt.date(2026, 8, 7)

    def bulk(n=200, list_offset=+20, list_null=False):
        rows = []
        for i in range(n):
            d = t - dt.timedelta(days=40 + i)
            rows.append(_cb_row(
                债券代码=f"1230{i:02d}", 债券简称=f"历史{i}转债", 申购日期=d,
                中签号发布日=d + dt.timedelta(days=6),
                上市时间=(None if list_null else d + dt.timedelta(days=list_offset))))
        return rows

    # ① 列位移：上市日跑到申购日**之前**（接到了登记日那类字段上）
    a = _run_source(CBIpoSource, bulk(list_offset=-1), t)
    assert a.error and "列位可能移位" in a.error, a.error
    assert any("不可信" in n for n in a.notes), a.notes

    # ② 整列取不到
    b = _run_source(CBIpoSource, bulk(list_null=True), t)
    assert b.error and "没有一行有可解析的上市时间" in b.error, b.error
    assert any("不是「近期没有新债上市」" in n for n in b.notes), b.notes

    # ③ 正常空窗：不报 error，说清接口正常
    c = _run_source(CBIpoSource, bulk(), t)
    assert not c.error and not c.opportunities, (c.error, c.opportunities)
    assert any("接口正常" in n for n in c.notes), c.notes

    # ④ 本栏有别的条目时不再多印一句 —— 报告不该为了自证而变长
    d = _run_source(CBIpoSource, bulk() + [_cb_row(债券代码="113999", 申购日期=t)], t)
    assert d.opportunities and not any("本栏 0 条" in n for n in d.notes), d.notes

    # ⑤ 小表不误报：--mock 那 4 行里只有 1 行有上市日
    from scanner.sources.base import Context
    e = CBIpoSource(Context(cfg={"lookahead_days": 10}, today=dt.date.today(),
                            mock=True)).fetch()
    assert not e.error, e.error
    assert any("上市" in o.action for o in e.opportunities), "mock 跑不出上市提醒"
    ok("上市时间列位移/整列空各报一句，正常空窗与小表都不误报")


# ---- 阶段 2 探针的两个纯函数（diag_redeem.py，不接进 scanner/）-------------
# 探针本身要联网才跑得动，但这两个函数是纯的 —— 能离线验的部分就得验，
# 不然等到联网那天才发现解析器自己是坏的，等于白探一次。

def test_redeem_title_classifier_separates_the_opposite_signal():
    """「不提前赎回」和「提前赎回」含义完全相反，绝不能并进同一栏。

    这不是措辞洁癖：前者是"这次不赎、继续持有"，后者是"限期离场，过期按 100 元
    收走"。按 `赎回` 粗筛会把两者一起捞进来，而混错的代价是照反方向操作。
    """
    import diag_redeem as D

    cases = {
        '关于提前赎回"春23转债"的公告': "提前赎回",
        '关于强制赎回"应流转债"的提示性公告': "提前赎回",
        '关于不提前赎回"XX转债"的公告': "不提前赎回",
        "关于决定不行使可转债提前赎回权利的公告": "不提前赎回",
        '关于"齐翔转2"赎回结果暨股份变动公告': "赎回结果/实施",
        '关于"宙邦转债"即将停止交易和转股的提示性公告': "停止交易提示",
        "关于召开2026年第二次临时股东大会的通知": "无关",
    }
    for title, want in cases.items():
        got = D.classify_title(title)
        assert got == want, f"{title!r} 判成了「{got}」，应该是「{want}」"

    # 顺序陷阱：「不提前赎回」包含「提前赎回」的全部字符，判反了方向就反了
    assert D.classify_title("关于不提前赎回的公告") != "提前赎回"
    ok("强赎标题分类把「不提前赎回」这个反向信号单独摘出来，不并进强赎")


def test_redeem_date_extractor_flags_what_it_is_unsure_about():
    """公告正文里的关键日期：找到、找不到、可能张冠李戴，三种都要如实回报。

    最危险的是第三种。「最后交易日、最后转股日分别为A、B」这种并列写法里，
    从"最后转股日"往后找先撞上的是 A —— 那是**前一个标签**的值，
    而且解析器不会报错，就这么静悄悄印一个错日期出去。
    """
    import diag_redeem as D

    # ① 常见分句：四个字段都拿到，且都不存疑
    got = D.extract_key_dates(
        "本次可转债的最后交易日为2026年8月18日，最后转股日为2026年8月20日，"
        "赎回登记日为2026年8月20日，赎回价格为100.53元/张。")
    assert got["最后交易日"]["date"] == dt.date(2026, 8, 18), got["最后交易日"]
    assert got["最后转股日"]["date"] == dt.date(2026, 8, 20), got["最后转股日"]
    assert got["赎回价格"]["value"] == 100.53, got["赎回价格"]
    assert not any(v.get("ambiguous") for v in got.values()), got

    # ② 全角数字 + 空格 + 插入语（公告里很常见）
    got = D.extract_key_dates("最后交易日为 ２０２６ 年 ８ 月 １８ 日（星期二）")
    assert got["最后交易日"]["date"] == dt.date(2026, 8, 18), got

    # ③ 横杠/斜杠格式，且用的是别名措辞
    got = D.extract_key_dates("停止交易日：2026-08-18；停止转股日：2026/8/20")
    assert got["最后交易日"]["date"] == dt.date(2026, 8, 18), got
    assert got["最后转股日"]["date"] == dt.date(2026, 8, 20), got

    # ④ 标签在、日期不在 → 必须回一个 date=None + 原文，而不是整条丢掉
    got = D.extract_key_dates("最后交易日详见公司后续公告，届时另行通知。")
    assert "最后交易日" in got and got["最后交易日"]["date"] is None, got
    assert "详见" in got["最后交易日"]["raw"], got

    # ⑤ 并列写法 → 两个都得标存疑（后一个拿到的就是错的那个日期）
    got = D.extract_key_dates(
        "本次可转债的最后交易日、最后转股日分别为2026年8月18日、2026年8月20日。")
    assert got["最后交易日"]["ambiguous"], got["最后交易日"]
    assert got["最后转股日"]["ambiguous"], (
        "并列句里后一个标签拿到的是前一个的日期，却没标存疑 —— "
        "这正是会静悄悄印错日期的那条路")
    ok("强赎日期解析：找不到与可能张冠李戴都如实回报，不静悄悄给一个错日期")


# ================================ v5.1：cb_redeem 源 ================================
# 六条：v5.1-rc 的四条 + docs/probes/probe2.txt 回来之后新增的两条（③档不假装覆盖、
# 倒计时字段里的 HTML 不进报告）。它们全是**离线**的 —— 这一栏的接口
# （bond_cb_redeem_jsl）沙箱连不上，但要钉的六件事都不在网络那一侧。

_REDEEM_COLS = ["代码", "名称", "现价", "正股代码", "正股名称", "规模", "剩余规模",
                "转股起始日", "最后交易日", "到期日", "转股价", "强赎触发比",
                "强赎触发价", "正股价", "强赎价", "强赎天计数", "强赎条款", "强赎状态"]


def _rd_row(**kw):
    """一行 bond_cb_redeem_jsl 的默认值，列名照 docs/probes/probe.txt 抄。"""
    base = {"代码": "113001", "名称": "测试转债", "现价": 128.0,
            "正股代码": "600001", "正股名称": "测试股份", "规模": 10.0,
            "剩余规模": 9.5, "转股起始日": None, "最后交易日": None,
            "到期日": None, "转股价": 12.0, "强赎触发比": "130",
            "强赎触发价": 15.6, "正股价": 16.2, "强赎价": 100.162,
            "强赎天计数": None, "强赎条款": "15/30, 130%", "强赎状态": ""}
    base.update(kw)
    return base


def _run_redeem(rows, today, cfg=None):
    """拿一张假 bond_cb_redeem_jsl 跑 cb_redeem 源（不联网）。"""
    import types
    from scanner.sources.cb_redeem import CBRedeemSource
    from scanner.utils import clear_cache

    df = pd.DataFrame(rows)[_REDEEM_COLS]
    fake = types.ModuleType("akshare")
    fake.bond_cb_redeem_jsl = lambda d=df: d
    sys.modules["akshare"] = fake
    clear_cache()
    base = {"lookahead_days": 10}
    base.update(cfg or {})
    return CBRedeemSource(Context(cfg=base, today=today, mock=False)).fetch()


def test_redeem_window_opens_on_trading_days():
    """退出窗口按**交易日**开，不是日历天。

    和缴款/上市那两个窗口同一个坑，但这一栏的代价最大：
    周五跑，若按日历天 +2 只盖到周日，下周一就要停止交易的那只债直接落在窗外 ——
    而漏掉它意味着**这只债你卖不掉了**，不是少赚一点。
    """
    fri = dt.date(2026, 8, 7)          # 周五
    mon = dt.date(2026, 8, 10)         # 下周一
    rows = [
        _rd_row(代码="113101", 名称="今日转债", 最后交易日=fri,
                到期日=fri + dt.timedelta(days=6)),
        _rd_row(代码="123102", 名称="周一转债", 最后交易日=mon,
                到期日=mon + dt.timedelta(days=6)),
        _rd_row(代码="113103", 名称="远期转债", 最后交易日=fri + dt.timedelta(days=45),
                到期日=fri + dt.timedelta(days=51)),
    ]
    res = _run_redeem(rows, fri, cfg={"cb_redeem": {"exit_window_trading_days": 2}})

    got = sorted(o.code for o in res.opportunities)
    assert got == ["113101", "123102"], got
    assert [o.urgency for o in res.opportunities if o.code == "113101"] == [Urgency.TODAY]
    assert [o.urgency for o in res.opportunities if o.code == "123102"] == [Urgency.SOON], \
        "周一停止交易的那只被窗口漏掉了 —— 说明窗口又退回按日历天算"

    # 剩余交易日是按交易日数的：周五到周一是 1 个交易日，不是 3 天
    mon_item = next(o for o in res.opportunities if o.code == "123102")
    assert mon_item.metrics["剩余交易日"] == 1, mon_item.metrics
    ok("退出窗口按交易日开：周五跑能盖住下周一停止交易的债，剩余天数也按交易日算")


def test_redeem_listed_without_date_never_vanishes():
    """在表里、但最后交易日为空的，必须自成一档出条 —— 不许静默消失。

    这不是假想：08-09 那份日报的已公告赎回栏里有春23 和应流，
    而 docs/probes/probe.txt 显示这两只在 bond_cb_redeem_jsl 里**有行、日期是空的**。
    只按「最后交易日在窗口内」出条的话，它们会安静地不见 ——
    报告看着干净，而你手里那只债就这么没人提了（纪律 5 的头号敌人）。
    """
    t = dt.date(2026, 8, 10)
    rows = [
        _rd_row(代码="113678", 名称="春23转债", 最后交易日=None,
                到期日=dt.date(2029, 3, 17)),
        _rd_row(代码="113685", 名称="应流转债", 最后交易日=None,
                到期日=dt.date(2031, 9, 19)),
        _rd_row(代码="113001", 名称="正常转债", 最后交易日=t,
                到期日=t + dt.timedelta(days=6)),
    ]
    res = _run_redeem(rows, t)

    codes = sorted(o.code for o in res.opportunities)
    assert codes == ["113001", "113678", "113685"], (
        f"名单里日期为空的被丢掉了：{codes} —— 这正是「静默归零」")

    for code in ("113678", "113685"):
        it = next(o for o in res.opportunities if o.code == code)
        # 不给紧急度：它没有时点，不该混进「今日/临近」去争注意力
        assert it.urgency == Urgency.WATCH, (code, it.urgency)
        assert it.action_date is None, (code, it.action_date)
        # 不给动作词：这一条是陈述，不是指令
        for verb in ("卖出", "买入", "转股", "今日", "务必", "立即"):
            assert verb not in it.action, f"{code} 的动作词命令了「{verb}」：{it.action}"
        # 「没取到」必须说成「没取到」，不能说成「没有」
        blob = it.action + "".join(it.flags)
        assert "未取到" in blob, blob
        assert "不是「没有」" in blob, blob

    # 空是**少数派**时（这一列大体有值，个别行缺）→ 缺失才是异常，照旧逐条印，
    # 超配额只截断展示，但**不许少说** —— 印出来的那句必须能对上真实条数。
    # （空占多数是另一件事，probe2 实测就是那种形状，见下一条用例。）
    many = ([_rd_row(代码=f"14{i:04d}", 名称=f"无日期{i}", 最后交易日=None,
                     到期日=dt.date(2029, 1, 1)) for i in range(30)]
            + [_rd_row(代码=f"15{i:04d}", 名称=f"远期{i}",
                       最后交易日=t + dt.timedelta(days=90),
                       到期日=t + dt.timedelta(days=96)) for i in range(70)])
    res2 = _run_redeem(many, t, cfg={"cb_redeem": {"max_unknown": 20}})
    shown = [o for o in res2.opportunities if o.action_date is None]
    assert len(shown) == 20, len(shown)
    claim = " ".join(res2.notes)
    assert "共 30 只" in claim and "另有 10 只" in claim, claim
    ok("名单内但日期未取到的自成一档：不静默消失、不给紧急度、不给动作词")


def test_redeem_countdown_is_never_parsed_as_a_date():
    """`强赎天计数` 必须当不透明字符串，一次 parse_date 都不许调。

    实测：`parse_date('12/15 | 30')` → `0001-12-15`（按空格截断成 '12/15'，
    pandas 兜底读成 12 月 15 日，年份缺省 0001），而且**只在首个数字 ≤12 时**
    中招 —— 恰好是倒计时刚开始的那批。一旦它被当成日期喂进窗口判断，
    这只债会带着一个 0001 年的「最后交易日」出现在报告里。
    """
    from scanner.utils import parse_date

    # 先钉住陷阱本身还在（哪天 parse_date 改了，这条会提醒你重看这段推理）
    bogus = parse_date("12/15 | 30")
    assert bogus is not None and bogus.year < 1900, bogus
    assert parse_date("20/15 | 30") is None, "首个数字 >12 时应当解析不出来"

    t = dt.date(2026, 8, 10)
    rows = [
        _rd_row(代码="123096", 名称="宙邦转债", 最后交易日=None,
                到期日=dt.date(2028, 9, 26), 强赎天计数="12/15 | 30"),
        _rd_row(代码="127001", 名称="另一转债", 最后交易日=None,
                到期日=dt.date(2029, 1, 1), 强赎天计数="20/15 | 30"),
    ]
    res = _run_redeem(rows, t)

    assert len(res.opportunities) == 2, res.opportunities
    for o in res.opportunities:
        assert o.action_date is None, (
            f"{o.code} 拿到了一个 action_date={o.action_date} —— "
            "倒计时字段被解析成日期了")
        # 原样透传：值一个字节都不许变，也不许出现被解析后的年份
        blob = f"{o.action} {o.note} {o.metrics} {''.join(o.flags)}"
        assert "0001" not in blob, blob
    zb = next(o for o in res.opportunities if o.code == "123096")
    assert "12/15 | 30" in zb.note, zb.note
    # 它不能待在 metrics 里：那一行用 ` | ` 分隔，值里正好有一个 ` | `
    assert not any("12/15" in str(v) for v in zb.metrics.values()), zb.metrics
    ok("倒计时字段原样透传：不被解析成日期，也不塞进以 | 分隔的 metrics 行")


def test_redeem_unknown_tier_does_not_pretend_coverage():
    """空占多数时，第 ③ 档不许逐条印 —— 但覆盖率必须在栏目级说全。

    这一条是 docs/probes/probe2.txt 直接改出来的。v5.1-rc 的做法是「超 max_unknown 就按
    代码排序截 20 条」，当时不知道这一档会有多大（Q4 未答）。probe2 答了：
    **319 行里带最后交易日的只有 6 行，其余 313 行为空（98%）**。

    于是原做法的两个后果同时出现：
      · 印出来的 20 条是代码最小那批噪音，天天占版面；
      · 这一档本来要救的春23(113667)/应流(113697) 恰好被截在配额外面。
    也就是说它既灌了噪音，又没救到人 —— 还让读者以为这一档有覆盖。

    「空」在这张表里不是「日期丢了」，是「这只债还没有最后交易日」。
    没有个体信息量的东西不该逐条占位；但**不逐条印不等于不说**：
    几行有日期、几行为空、空不等于「没在赎回」，这三件事一句都不能少。
    """
    t = dt.date(2026, 8, 10)
    # probe2 的 6 行真实数据（最后交易日 / 到期日照抄），其余按实测比例铺开
    real = [("128128", "齐翔转2", dt.date(2026, 8, 14), dt.date(2026, 8, 20)),
            ("128127", "文科转债", dt.date(2026, 8, 14), dt.date(2026, 8, 20)),
            ("113039", "嘉泽转债", dt.date(2026, 8, 18), dt.date(2026, 8, 24)),
            ("128129", "青农转债", dt.date(2026, 8, 19), dt.date(2026, 8, 25)),
            ("123064", "万孚转债", dt.date(2026, 8, 26), dt.date(2026, 9, 1)),
            ("123065", "宝莱转债", dt.date(2026, 8, 31), dt.date(2026, 9, 4))]
    rows = [_rd_row(代码=c, 名称=n, 最后交易日=lt, 到期日=md, 强赎状态="已公告强赎")
            for c, n, lt, md in real]
    rows += [_rd_row(代码="113667", 名称="春23转债", 最后交易日=None,
                     到期日=dt.date(2029, 3, 17), 强赎天计数="15/15 | 30",
                     强赎状态="公告要强赎"),
             _rd_row(代码="113697", 名称="应流转债", 最后交易日=None,
                     到期日=dt.date(2031, 9, 19), 强赎天计数="17/15 | 30",
                     强赎状态="公告要强赎")]
    rows += [_rd_row(代码=f"11{i:04d}", 名称=f"存续{i}转债", 最后交易日=None,
                     到期日=dt.date(2029, 1, 1), 强赎天计数=f"{i % 16}/15 | 30")
             for i in range(311)]
    assert len(rows) == 319, len(rows)

    res = _run_redeem(rows, t)

    # ① 一条「未取到」都不该逐行印
    blanks = [o for o in res.opportunities if o.action_date is None]
    assert blanks == [], f"空占 98% 时还逐条印了 {len(blanks)} 条 —— 那是噪音不是覆盖"
    # ② 该出的还得出：窗口内那两只（08-14 停止交易）一个都不许少
    assert sorted(o.code for o in res.opportunities) == ["128127", "128128"], \
        [o.code for o in res.opportunities]

    # ③ 少印可以，少说不可以：三个数 + 那句「空不等于没在赎回」都要在
    claim = " ".join(res.notes)
    for piece in ("319", "6 行", "313", "不等于"):
        assert piece in claim, f"栏目级说明缺了「{piece}」：{claim}"
    assert "没有" not in claim.replace("给不出", ""), \
        f"把「没取到」说成了「没有」：{claim}"
    # ④ 栏目说明也吃预算（160 字），别哪天把这句写成一段
    from verify_report import MAX_BANNER_CHARS
    for n in res.notes:
        assert len(n) <= MAX_BANNER_CHARS, (len(n), n)

    # ⑤ 关掉这条口子（gate=1.0）应当回到 v5.1-rc 的截断行为 —— 留着可回退
    res2 = _run_redeem(rows, t, cfg={"cb_redeem": {"unknown_ratio_gate": 1.0}})
    assert len([o for o in res2.opportunities if o.action_date is None]) == 20
    ok("空占多数时第 ③ 档不逐条印，但覆盖率照说；gate 可关回旧行为")


def test_redeem_countdown_html_never_reaches_the_report():
    """`强赎天计数` 里的 HTML 标签不许进报告。

    probe2 实测这一列**会带标签** —— 临近到期那批的取值是：
        临近到期 <span style="color:red;font-weight:bold;">!</span><br>2026-08-14 最后交易
    裸标签进 markdown 会显示成一串尖括号，进 html 会**真的生效**（红色加粗）：
    集思录的排版跑到我的报告里来了，而报告的字体权重是用来标紧急度的。
    标签不是这个值的内容，删标签不算改值 —— 文本一个字都不动。

    （这一列只在第 ③ 档透传，而带 HTML 的那批都是有日期的行，所以今天两者
    碰不上。留这条用例是因为「最后交易日整列改版」时它们就会碰上 ——
    那正是最需要这一列的时候。）
    """
    t = dt.date(2026, 8, 10)
    raw = ('临近到期 <span style="color:red;font-weight:bold;">!</span>'
           '<br>2026-08-14 最后交易')
    rows = [_rd_row(代码="128128", 名称="齐翔转2", 最后交易日=None,
                    到期日=dt.date(2026, 8, 20), 强赎天计数=raw)]
    res = _run_redeem(rows, t)

    it = res.opportunities[0]
    for tag in ("<span", "<br", "style=", "&lt;"):
        assert tag not in it.note, f"标签漏进报告了：{it.note}"
    # 文本照旧：删的只有标签
    assert "临近到期" in it.note and "2026-08-14 最后交易" in it.note, it.note
    assert "本工具不解析它" in it.note, it.note
    ok("倒计时字段里的 HTML 标签删掉、文本不动，标签不会进报告")


def test_redeem_action_words_pass_the_report_invariants():
    """三档的动作词一起过五条不变量，且**不给任何一只债贴强赎/到期的标签**。

    这一栏最容易犯的错是「说得比知道的多」：表里的最后交易日既可能是强赎的，
    也可能是自然到期摘牌的，而两者处置方向相反（限期离场 vs 拿本息）——
    在 `diag_redeem2.py` 的退出码回来之前，替某一只债选一个方向就是猜。

    注意区分两件相反的事：
      · 给某只债贴「强赎」标签 = 下判断 → 禁止；
      · 栏目级那句「本栏不区分强赎与到期摘牌」= 声明限制 → 必须说。
    只做前一件的沉默不是谨慎，是把风险留给读者。
    """
    from verify_report import verify_console

    t = dt.date(2026, 8, 10)
    rows = [
        _rd_row(代码="113001", 名称="今日转债", 最后交易日=t,
                到期日=t + dt.timedelta(days=6), 现价=128.5),
        _rd_row(代码="123002", 名称="临近转债", 最后交易日=t + dt.timedelta(days=3),
                到期日=dt.date(2031, 3, 18), 现价=131.2),
        _rd_row(代码="113003", 名称="已过转债", 最后交易日=t - dt.timedelta(days=2),
                到期日=t + dt.timedelta(days=4), 现价=100.4),
        _rd_row(代码="113004", 名称="无日期转债", 最后交易日=None,
                到期日=dt.date(2029, 1, 25), 现价=119.8, 强赎天计数="12/15 | 30"),
    ]
    res = _run_redeem(rows, t)
    assert len(res.opportunities) == 4, [o.code for o in res.opportunities]

    # ① 逐条：动作词里不许出现针对这一只债的分类
    for o in res.opportunities:
        for word in ("强赎", "到期摘牌", "赎回价", "拿本息"):
            assert word not in o.action, f"{o.code} 的动作词下了判断「{word}」：{o.action}"
        assert "最后交易日" in o.action, o.action

    # ② 栏目级的限制声明必须在，而且要说全「方向相反」
    blob = "".join(res.footnotes)
    assert "不区分强赎与到期摘牌" in blob, res.footnotes
    assert "相反" in blob, "只说了不区分，没说两者方向相反 —— 读者不会知道这有多要紧"

    # ③ 渲染后过五条不变量（③动作词方向、④矛盾指令、⑤预算都在里面）
    ctx = Context(cfg={"capital": 100000, "accounts": 1}, today=t, mock=False)
    errs = verify_console(render_console([res], ctx))
    assert not errs, "\n".join(errs)

    # ④ 提示预算：这一栏每条只挂一句，别哪天又涨回去
    for o in res.opportunities:
        assert len(o.flags) <= 1, (o.code, o.flags)
        assert len("；".join(o.flags)) <= 60, (o.code, o.flags)
    ok("退出提醒三档：动作词不下强赎/到期判断，栏目级限制说全，五条不变量全过")


# ======================= v5.7：文档侧的腐烂检查 =======================
# 为什么这两条要进 check.sh，而不是只写成一句纪律：
#
# §10 附4 的结尾自己写过一句「测试拦得住行为退化，拦不住文档腐烂 ——
# 所以每轮的对账不能省」。v5.6 又写过一句「能变成代码的就别只写成文字」。
# 然后 v5.7 这轮对账数出来：`docs/probes/probe8.txt` 早就躺在包里，而 `STATE.md` 和
# `HANDOFF.md` 里它**只出现过一次，就是那条要往它上面写的重定向命令**。
# 照着文档跑一遍，等于把一份存档抹掉。
#
# 这正是 §6.6 ① 已经吃过一次的亏（当时是探针自己印死编号，修成了
# `archive_hint()` 只印规则不印编号）。**代码那一侧守住了，文档这一侧没有。**
# 同一个形状连着栽两次，就不该再靠「下轮记得对账」兜。

def _project_root():
    import pathlib
    return pathlib.Path(__file__).resolve().parent


def _dev_doc(name):
    """开发文档（STATE/HANDOFF/KICKOFF）的位置。

    开源那一版把它们从根目录挪进了 `docs/dev/` —— 根目录只留面向使用者的
    README / DISCLAIMER / SETUP_AUTORUN。**两处都认**：老布局的包摊在根目录也照跑，
    否则这几条断言会因为一次纯目录调整全红，而它们本来是要盯内容的。
    """
    root = _project_root()
    for cand in (root / "docs" / "dev" / name, root / name):
        if cand.exists():
            return cand
    return root / "docs" / "dev" / name


def _probes_dir():
    """探针存档目录，同样两处都认（`docs/probes/` 优先）。"""
    root = _project_root()
    d = root / "docs" / "probes"
    return d if d.is_dir() else root


def _probe_archives():
    """现存的探针存档，按编号排好。`docs/probes/probe.txt` 算 1 号。"""
    import re
    out = []
    for p in _probes_dir().glob("probe*.txt"):
        m = re.fullmatch(r"probe(\d*)\.txt", p.name)
        if m:
            out.append((int(m.group(1) or 1), p.name))
    return [name for _, name in sorted(out)]


def test_every_probe_archive_is_registered_in_the_handoff():
    """每一份存档都要在 `HANDOFF.md` 里被点到名 —— 没登记的证据等于不存在。

    这条抓的不是「写错了一个数」，是**一份跑出来的输出没人知道它在**：
    没登记 → 下一轮对账数不出来 → 它的编号被当成空位派出去 → 覆盖。
    v5.7 的 `docs/probes/probe8.txt` 走完的正是这条链子的前三节。
    """
    doc = _dev_doc("HANDOFF.md").read_text(encoding="utf-8")
    archives = _probe_archives()
    assert archives, "一份 probe*.txt 都没有，这个检查失去意义 —— 是不是打包漏了？"

    missing = []
    for name in archives:
        stem = name[:-len(".txt")]
        # 「登记」= 除了那条重定向命令之外，还有别的地方提到它
        hits = doc.count(stem)
        as_target = doc.count("> " + name) + doc.count(">" + name)
        if hits - as_target <= 0:
            missing.append(name)
    assert not missing, (
        f"这些存档在 HANDOFF.md 里没有登记，只在重定向命令里出现过：{missing}\n"
        f"→ 去 §3 代码地图补一行，写清楚它是哪个脚本、哪一次跑的、为什么别删。")
    ok(f"{len(archives)} 份探针存档在 HANDOFF 代码地图里逐一登记，没有谁只以「被写入目标」的身份出现")


def test_state_never_hardcodes_an_archive_number():
    """`STATE.md` 里不许写死存档编号 —— 编号会腐烂，规则不会。

    只管 `STATE.md`，**不一刀切管 `HANDOFF.md`**：那一份兼记历史，
    「当初那条命令就是 `> docs/probes/probe2.txt`」是史料，写死的编号在那里是对的。
    而 `STATE.md` 自己的抬头就写着「只写现在在哪 / 下一步做什么」——
    它里面的每一个编号都是指向将来的，**指向将来的编号一定会过期**。

    正确写法照 `diag_cbplan.archive_hint()`：
    「另存为一个没用过的编号（probeN.txt，N 取现有最大号 +1）」。
    """
    import re
    state = _dev_doc("STATE.md").read_text(encoding="utf-8")
    bad = re.findall(r">\s*(probe\d*\.txt)", state)
    assert not bad, (
        f"STATE.md 把存档编号写死成了重定向目标：{sorted(set(bad))}\n"
        f"→ 现存 {_probe_archives()}；照 archive_hint() 改成只说规则不说编号。")
    ok("STATE.md 的下一步命令只说存档规则不写死编号，照 archive_hint() 那句")


def test_event_arb_declares_it_when_the_search_hits_the_page_cap():
    """检索被服务端截断时必须出声 —— 「静默地少给」是纪律 5 的头号敌人。

    背景：probe6 量到巨潮检索在 **3000 条 = 100 整页**封顶，越过封顶
    akshare 回来的是重复行。`event_arb` 结构上够不到那个封顶（传了 7 天窗、
    关键词又窄），**但那是推理不是保证** —— 窗口配大、关键词配宽都能推上去。

    这条钉两件事，方向相反，缺一不可：
      · 到了封顶 → 必须说，而且**不许贴方向**（「漏了多少」谁也不知道）；
      · 没到封顶 → **一个字都不许多说**，否则天天报警等于没有报警。
    """
    from scanner.sources.event_arb import _CNINFO_PAGE_CAP

    assert _CNINFO_PAGE_CAP == 3000, "封顶值改了？probe6 实测是 3000，改它要有新实测"

    src = (_project_root() / "scanner" / "sources" / "event_arb.py").read_text(
        encoding="utf-8")
    # ① 守卫必须挂在**每个关键词自己的返回量**上，不是挂在去重后的合计上：
    #    合计过了 3000 不代表任何一路被截，单路到 3000 才是被截。
    assert "len(df) >= _CNINFO_PAGE_CAP" in src, "守卫没挂在单个关键词的返回量上"
    # ② 提示语不许贴方向 —— 这一条是 v5.6 用 probe6 证伪过一次的（原来印「下界」）
    for word in ("下界", "上界", "至少", "最多"):
        assert word not in src.split("capped_kws:")[-1][:600], \
            f"封顶提示又贴方向了（「{word}」）—— 这个数不是计数，是 30 × 翻的页数"
    # ③ 它只能往 notes 里加话，不能改条目：少印是不允许的
    assert "capped_kws" not in src.split("rows.sort")[-1], \
        "封顶守卫伸到了条目侧 —— 它只许多说一句，不许少印一条"
    ok("检索到整页封顶时栏目级出声、不贴方向，且只加说明不动条目")


def test_documented_exit_codes_match_the_criteria_in_code():
    """`decide()` 判的东西，和三份文档里写的退出码含义**必须是同一件事**。

    这一条抓的是 v5.8 换问题时最容易留下的那种腐烂：
    判据换成了 B（获批名单 + 截断限制），而文档里那句
    「0 = 名单拿得到且口径复现 ≥3/4」还挂着 —— 下一轮谁照文档读，
    就会拿一个早已不存在的判据去对代码。

    §6.8 ⑤ 那张表的教训：**「已修」的判据是「它再犯的时候有没有东西会变红」。**
    换判据这件事横跨代码 + 三份文档，靠人对账已经栽过一次（§6.8 ④）。
    """
    root = _project_root()
    src = (root / "diag_cbplan.py").read_text(encoding="utf-8")

    # ① 代码这一侧：decide() 里不许再出现 A 方案的判据
    seg = src.split("def " + "decide")[1].split("\ndef ")[0]
    for gone in ("_MIN_HIT", "_TOL_PCT", "_CALIB", "spot_ok"):
        assert gone not in seg, \
            f"decide() 里还留着 A 方案的判据 {gone} —— 换问题就是换判据，别留暗门"
    assert "_MIN_APPR" in seg, "decide() 没在用 B 方案的门槛 _MIN_APPR"

    # ② 文档这一侧：三份文档里不许再写 A 方案那句退出码含义。
    #    历史叙述放行（照 §6.8 ④ 第 15 条的老办法，认「原来」「已搁置」这类标记）——
    #    「当初那句判据是什么」是史料，**照着它去对代码**才是害人的那一种。
    stale = "口径复现"
    for name in ("STATE.md", "HANDOFF.md"):
        for ln in _dev_doc(name).read_text(encoding="utf-8").splitlines():
            if stale in ln and "0 =" in ln:
                if any(m in ln for m in ("原来", "已搁置", "旧", "~~", "咬到")):
                    continue
                raise AssertionError(
                    f"{name} 还在按 A 方案写退出码含义：{ln.strip()[:70]}")

    # ③ B 方案的命根子：退出码 0 那一支必须说「下限不是全集」。
    #    只给名单不给含权量，读者容易把「上了名单」读成「值得埋伏」（§6.5 B 的风险）。
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cbplan_probe", str(root / "diag_cbplan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    code, why = mod.decide(True, {"searched": True, "n_cb": 22, "n_all": 133,
                                  "capped": ["可转换公司债券"], "uncountable": []})
    assert code == 0, why
    blob = "".join(why)
    assert "下限，不是全集" in blob and "纪律 8" in blob, why
    ok("退出码含义在代码和文档里是同一件事；退 0 那一支必带「下限不是全集」")


# ================================================================ v5.9.1
def test_item_note_reaches_every_format():
    """条目级 `note` 必须三种格式都印得出来。

    起因：`render_html` 从头到尾**没引用过 `o.note`**。console(149) 和
    markdown(211) 都印，只有 HTML 这一路没有 —— 而 config 默认的 formats 是
    ["console", "html"]，HTML 恰恰是每天真正被读的那一份。
    08-11 那份 md 里有 7 条条目级「注」，同一次运行的 html 里一条都没有：
    打新的「上市日是这条线上唯一要你判断的一天」、配债的面值口径、
    cb_approved 的「获批日期取最早那条」全都在这一层丢掉了。

    没被拦住是因为 `verify_report` 只解析 console 文本，5 条不变量一条都不读
    HTML，而 report 的两条老渲染断言恰好都没给 Opportunity 设过 note。
    """
    from scanner.report import render_markdown

    marker = "该正股另有 1 只更早的转债 —— 不是这一次的"
    res = SourceResult(
        kind=Kind.CB_APPROVED,
        opportunities=[Opportunity(kind=Kind.CB_APPROVED, code="601111",
                                   name="示例股份", action="获批公告 07-22",
                                   urgency=Urgency.WATCH,
                                   metrics={"获批日期": "2026-07-22"},
                                   link="http://x/1", note=marker)])
    ctx = Context(cfg={"capital": 100000}, today=dt.date(2026, 8, 12))

    for name, fn in (("console", render_console), ("markdown", render_markdown),
                     ("html", render_html)):
        txt = fn([res], ctx)
        assert marker in txt, f"{name} 这一路把条目级 note 丢了"
    ok("条目级「注」三种格式都印得出来（HTML 曾整个漏掉这一层）")


def _approved_run(rows, cov_rows, cfg, today=dt.date(2026, 8, 12)):
    """用假检索表跑一次 cb_approved（走 mock 路径，不碰网络）。"""
    from scanner.sources.cb_approved import CBApprovedSource
    import scanner.sources.cb_approved as ca
    saved = (ca._mock_df, ca._mock_cov_df)
    df = pd.DataFrame(rows)
    ca._mock_df = lambda kw: (df if kw == "同意注册" else pd.DataFrame())
    ca._mock_cov_df = lambda: pd.DataFrame(cov_rows)
    try:
        base = {"window_days": 180, "keywords": ["同意注册"],
                "max_items": 30, "stale_days": 90, "hide_issued": False}
        base.update(cfg)
        return CBApprovedSource(Context(cfg={"cb_approved": base},
                                        today=today, mock=True)).fetch()
    finally:
        ca._mock_df, ca._mock_cov_df = saved


def test_approved_truncation_never_eats_the_stale_tier():
    """截断不许把「获批满 N 天仍未发行」那一档砍掉，措辞也得跟印出来的一致。

    v5.9-rc 的排序是获批日期倒序、留最新的 max_items 条 —— 砍掉的永远是最老的，
    而 stale 按定义就是最老的那一批。两个方向对着干，stale 结构性地必被砍光，
    栏目级却还写着「已逐条标出，**一条都没过滤**」。

    实盘 08-12 就撞上了：命中 50 只、印 30 只，说「1 只获批已满 90 天」，
    而印出来的 30 条距获批只到 83 天 —— 那一只在被砍的 20 只里。
    """
    T = dt.date(2026, 8, 12)

    def row(code, back):
        d = T - dt.timedelta(days=back)
        return {"代码": code, "简称": "票" + code,
                "公告标题": "关于<em>同意注册</em>向不特定对象发行可转换公司债券的批复",
                "公告时间": d.strftime("%Y-%m-%d %H:%M:%S"),
                "公告链接": "http://x/" + code}

    # 33 只已发行（0~32 天）+ 2 只未发行且已过 stale_days（100/120 天）
    rows = [row("6000%02d" % i, i) for i in range(33)] + \
           [row("6009%02d" % k, b) for k, b in enumerate((100, 120))]
    cov = [{"债券代码": "1130%02d" % i, "债券简称": "债%02d" % i,
            "正股代码": "6000%02d" % i,
            "申购日期": T - dt.timedelta(days=i)} for i in range(33)]

    r = _approved_run(rows, cov, {"max_items": 30})
    printed = {o.code for o in r.opportunities}
    assert {"600900", "600901"} <= printed, "stale 那一档被 max_items 砍掉了"
    assert len(r.opportunities) == 30, len(r.opportunities)
    assert any("已发行的 5 只未列出" in n for n in r.notes), r.notes
    assert any("一条都没砍" in n for n in r.notes), r.notes
    assert any("2 只获批已满 90 天" in n and "这一档不参与截断" in n
               for n in r.notes), r.notes
    assert not any("没印出来" in n for n in r.notes), "说的和印的对不上"

    # 恰好等于上限时不许谎报截断（同 event_arb 那条的判据）
    exact = _approved_run(rows, cov, {"max_items": 35})
    assert len(exact.opportunities) == 35
    assert not any("未列出" in n for n in exact.notes), exact.notes

    # 未走完的那一档本身就超过 max_items 时：宁可超长，也要说出来，不静默砍
    over = _approved_run(rows, cov, {"max_items": 1})
    assert {"600900", "600901"} <= {o.code for o in over.opportunities}
    assert any("超过 max_items=1" in n for n in over.notes), over.notes
    ok("获批栏：截断只砍「已发行」那一档，stale 一条不砍，超长时照说")


def test_approved_marks_private_placement_and_keeps_it_out_of_the_stale_count():
    """定向可转债要标出来，而且**不许**计进「获批满 N 天」那个数。

    「向特定对象发行」/「发行可转债购买资产」不面向公众：没有网上申购、
    没有原股东配售、bond_zh_cov 里本来就不会有 —— 它的「未查到发行记录」是
    结构性的。混进 stale 会让那个数越攒越大，而那个数本来是要你回头看一眼的信号。
    实盘 08-12 那 30 条里有 2 条是这一类（920826 / 688230）。

    判据是标题里的**原文字样**，不是推断；没写明的照印，不替它猜。
    """
    T = dt.date(2026, 8, 12)
    at = (T - dt.timedelta(days=150)).strftime("%Y-%m-%d %H:%M:%S")

    def row(code, title):
        return {"代码": code, "简称": "票" + code, "公告标题": title,
                "公告时间": at, "公告链接": "http://x/" + code}

    rows = [
        row("600001", "关于向不特定对象发行可转换公司债券<em>同意注册</em>的批复"),
        # 标题形状抄实盘那两条
        row("920826", "关于向特定对象发行可转换公司债券获得<em>同意注册</em>的提示性公告"),
        row("688230", "关于发行可转换公司债券及支付现金购买资产<em>同意注册</em>的公告"),
        row("600004", "关于收到可转换公司债券<em>同意注册</em>批复的公告"),
    ]
    r = _approved_run(rows, [], {})
    kinds = {o.code: o.metrics.get("发行方式") for o in r.opportunities}
    assert kinds == {"600001": "公募", "920826": "定向",
                     "688230": "定向", "600004": "标题未写明"}, kinds

    def stale_flagged(code):
        o = next(x for x in r.opportunities if x.code == code)
        return any("仍未查到发行记录" in f for f in o.flags)

    assert stale_flagged("600001") and stale_flagged("600004")
    assert not stale_flagged("920826") and not stale_flagged("688230"), \
        "定向的被计进了「获批满 N 天」"
    assert any("获批已满 90 天" in n and "2 只" in n for n in r.notes), r.notes
    assert any("2 只标题写的是" in n for n in r.notes), r.notes
    priv = next(x for x in r.opportunities if x.code == "920826")
    assert any("向特定对象" in f for f in priv.flags), priv.flags
    ok("获批栏：定向可转债标得出来，且不计进「获批满 N 天」那个数")



# ================= v5.9.2：四处静默少给（都先在旧代码上验过是红的）=================
def test_ipo_gate_never_eats_the_payment_reminder():
    """min_rating / min_convert_value 只关**申购**这一条，别把整行跳掉。

    旧代码是 `if within(申购日…): if not _rating_ok(…): continue`，而 `continue`
    跳的是 DataFrame 的下一行 —— 同一只债的缴款提醒和上市提醒一起没了。
    而 cb_ipo.py 里明写着「不套 min_rating / min_convert_value：那两道门是
    要不要申购的判据，已经在手里的债不该因为评级低就不提醒你它今天上市」。

    在旧代码上：出条数从 2 掉到 0（缴款提醒静默消失）。缴款是这条线唯一的硬性风险。
    """
    from scanner.sources import cb_ipo
    T = dt.date(2026, 8, 12)
    row = {"债券代码": "123001", "债券简称": "低评级转债", "申购日期": T,
           "申购代码": "123001", "申购上限": 100.0, "正股代码": "300001",
           "正股简称": "某股份", "正股价": 10.0, "转股价值": 95.0, "债现价": 100.0,
           "转股溢价率": 20.0, "原股东配售-股权登记日": T - dt.timedelta(days=1),
           "原股东配售-每股配售额": 1.0, "发行规模": 5.0, "中签号发布日": T,
           "中签率": 0.01, "上市时间": T, "信用评级": "A"}
    saved = cb_ipo._mock_df
    cb_ipo._mock_df = lambda: pd.DataFrame([row])
    try:
        def run(cfg):
            r = cb_ipo.CBIpoSource(Context(cfg={"cb_ipo": cfg}, today=T, mock=True)).fetch()
            return [o.action for o in r.opportunities]

        base = run({})
        assert sum("申购" in a for a in base) == 1, base
        assert any("缴款" in a for a in base), base
        assert any("上市" in a for a in base), base

        # 评级门：只该少掉申购那一条
        gated = run({"min_rating": "AA"})
        assert not any("顶格申购" in a for a in gated), f"评级门没关住申购：{gated}"
        assert any("缴款" in a for a in gated), f"评级门吃掉了缴款提醒：{gated}"
        assert any("上市" in a for a in gated), f"评级门吃掉了上市提醒：{gated}"

        # 转股价值门同理（旧代码里是同一个 continue）
        cv = run({"min_convert_value": 100})
        assert not any("顶格申购" in a for a in cv), cv
        assert any("缴款" in a for a in cv), f"转股价值门吃掉了缴款提醒：{cv}"
    finally:
        cb_ipo._mock_df = saved
    ok("打新的两道门只关申购那一条，缴款/上市提醒照出（旧代码整行跳掉）")


def test_trading_window_never_shrinks_past_the_calendar_edge():
    """交易日历盖不住的那一段，窗口不许比真实的短，而且得说出来。

    回落逻辑「跳周末」把节假日**当成了交易日** → 更快数满 n 个 → 终点更早 →
    窗口更短。方向恰好和 utils 里那句「窗口偏长，宁可多提醒一天」相反
    （那句话在 v5.9.2 改对了）。

    而且它不需要「日历拿不到」才触发：新浪日历只排到当年年底，包里那份最晚
    2026-12-31。拿真实边界实测，旧代码 12-30 跑缴款窗口 +2 交易日算出
    2027-01-01（元旦当天），实际应为 01-04。
    """
    days = {d for d in (dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(365))
            if d.weekday() < 5}
    cal = utils.TradeCalendar(days)                 # 覆盖到 2026-12-31，之后靠猜

    # 覆盖范围之内：一天都不多开，和 shift_trading_days 完全一致
    inside = dt.date(2026, 8, 7)                    # 周五
    end, sure = utils.trading_window_end(inside, 2, cal)
    assert sure is True and end == utils.shift_trading_days(inside, 2, cal) == dt.date(2026, 8, 11)

    # 覆盖范围之外：必须自报「没担保」，且终点不早于真实答案
    edge = dt.date(2026, 12, 30)
    end, sure = utils.trading_window_end(edge, 2, cal)
    assert sure is False, "越界了却说担保得了"
    assert utils.shift_trading_days(edge, 2, cal) == dt.date(2027, 1, 1), "前提变了"
    assert end >= dt.date(2027, 1, 4), f"窗口偏短，缴款日 01-04 落在窗外：{end}"

    # covers() 要能分出「查到的」和「猜的」
    assert cal.covers(dt.date(2026, 12, 31)) and not cal.covers(dt.date(2027, 1, 4))

    # 边际可以关掉（--mock 用它，自检要可复现），关掉后和旧行为一字不差
    end0, sure0 = utils.trading_window_end(edge, 2, cal, unknown_margin=0)
    assert sure0 is False and end0 == utils.shift_trading_days(edge, 2, cal)

    # shift_trading_days 的语义不许被边际污染：它回答的是「第 n 个交易日是哪天」
    assert utils.shift_trading_days(inside, 2) == dt.date(2026, 8, 11)
    ok("日历越界时窗口多开边际、自报没担保，而 shift_trading_days 语义不变")


def test_approved_missing_code_never_merges_two_companies():
    """代码取不到时，不同公司不许被并成一条。

    旧代码 `code = _norm_code(…) or "未取到"` 让所有空代码行共用同一个 key：
    三家公司 → 出 1 条，只留第一家的简称、取所有家里最早的那个日期，
    而注里那句「窗口内另有 N 条同类公告」会被读成「同一家公司发了 N 条」——
    一句本来用于自证的话变成了误导。
    """
    T = dt.date(2026, 8, 12)

    def row(name, back, code=""):
        return {"代码": code, "简称": name,
                "公告标题": "关于向不特定对象发行可转换公司债券<em>同意注册</em>的批复",
                "公告时间": (T - dt.timedelta(days=back)).strftime("%Y-%m-%d %H:%M:%S"),
                "公告链接": f"http://x/{name}/{back}"}   # 链接要各不相同，否则先被去重

    r = _approved_run([row("甲", 10), row("乙", 100), row("丙", 5)], [], {})
    assert len(r.opportunities) == 3, f"空代码把公司并成了 {len(r.opportunities)} 条"
    assert {o.name for o in r.opportunities} == {"甲", "乙", "丙"}
    assert all(o.code == "未取到" for o in r.opportunities), "代码位置该照实印「未取到」"
    assert not any("另有" in (o.note or "") for o in r.opportunities), \
        "不同公司之间不该出现「窗口内另有 N 条同类公告」"

    # v5.9.3 改了这一小节。原来写的是「三条都没发行、乙已过 stale_days →
    # 断言 notes 里有『1 只获批已满 90 天』」—— 那是**把当时的错误行为钉住了**：
    # 这三条一个正股代码都没有，本工具压根没法回配总表，"未查到发行记录" 是
    # 我们自己编的（`cov_idx.get("未取到")` 必然落空），据此再攒天数、标
    # 「距获批 100 天仍未查到发行记录」，读起来像这只债出了事。
    # 断言的**主张**（空代码不许并条）没错，错的是它顺手认可的这个副作用。
    # 现在：没代码的一条都不进 stale，另用一只**有代码**的 stale 债把
    # 「说的和印的是同一件事」那层牙齿补回来。
    assert not any("获批已满" in n for n in r.notes), \
        f"没法核对的不该攒进「获批满 N 天」：{r.notes}"
    assert all(o.metrics["发行状态"].startswith("未核对") for o in r.opportunities), \
        [o.metrics["发行状态"] for o in r.opportunities]

    r2 = _approved_run([row("甲", 10), row("乙", 100), row("丁", 120, "600222")], [], {})
    assert any("1 只获批已满 90 天" in n for n in r2.notes), r2.notes
    assert not any("没印出来" in n for n in r2.notes), r2.notes
    stale_flagged = [o.code for o in r2.opportunities
                     if any("仍未查到发行记录" in f for f in o.flags)]
    assert stale_flagged == ["600222"], f"标 stale 的应当只有有代码那只：{stale_flagged}"

    # 有代码的照旧按正股归并，这一条不许被上面的改法带坏
    same = _approved_run([row("甲", 10, "600111"), row("甲", 20, "600111")], [], {})
    assert len(same.opportunities) == 1
    assert "另有 1 条同类公告" in same.opportunities[0].note
    ok("获批栏：代码取不到时按公告归并，两家公司不会被并成一条")


def test_event_arb_zero_says_which_kind_of_zero():
    """事件套利 0 条时要说清是哪种 0 —— 它曾是六个源里唯一沉默的那个。

    报告只印一个「无」，而「7 天真空窗」和「关键词全部空返回」长得一模一样。
    分寸照其余五个源：只在整栏一条都没有时才多说话，有条目时闭嘴。
    """
    from scanner.sources import event_arb as E
    T = dt.date(2026, 8, 12)
    saved = E._mock_df

    def run(cfg, mock_fn):
        E._mock_df = mock_fn
        try:
            return E.EventArbSource(Context(cfg={"event_arb": cfg}, today=T,
                                            mock=True)).fetch()
        finally:
            E._mock_df = saved

    cfg = {"keywords": ["要约收购"], "fund_keywords": [], "window_days": 7}

    # ② 检索通了、窗口内确实没有
    r = run(cfg, lambda kw: pd.DataFrame())
    assert r.notes and any("接口正常" in n for n in r.notes), r.notes

    # ⓞ 两组关键词都没配
    r0 = run({"keywords": [], "fund_keywords": []}, lambda kw: pd.DataFrame())
    assert any("关键词都没配" in n for n in r0.notes), r0.notes

    # ③ 有命中行但全落在窗口外
    old = (T - dt.timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    r3 = run(cfg, lambda kw: pd.DataFrame([{
        "代码": "600123", "简称": "旧要约", "公告标题": "关于要约收购报告书的公告",
        "公告时间": old, "公告链接": "http://x/1"}]))
    assert not r3.opportunities
    assert any("窗口之外" in n for n in r3.notes), r3.notes

    # 有条目时不许多说话
    now = (T - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    rh = run(cfg, lambda kw: pd.DataFrame([{
        "代码": "600123", "简称": "新要约", "公告标题": "关于要约收购报告书的公告",
        "公告时间": now, "公告链接": "http://x/2"}]))
    assert rh.opportunities and not any("本栏 0 条" in n for n in rh.notes), rh.notes
    ok("事件套利 0 条能分出四种：没配关键词/全失败/真空窗/全落窗外")


# ==================================================================
# v5.9.3：源码复核第二轮翻出的三处静默 + 四处口径 + 一处凭据被覆盖
# 八条全部先在 v5.9.2 上验过是红的（见 STATE.md 那张表）。
# ==================================================================



def _ipo_run(rows, cfg, today=dt.date(2026, 8, 12)):
    """用假 bond_zh_cov 跑一次 cb_ipo（走 mock 路径，不碰网络）。"""
    import scanner.sources.cb_ipo as ci
    saved = ci._mock_df
    ci._mock_df = lambda: pd.DataFrame(rows)
    try:
        return ci.CBIpoSource(Context(cfg={"cb_ipo": cfg, "lookahead_days": 10},
                                      today=today, mock=True)).fetch()
    finally:
        ci._mock_df = saved


def _ipo_row(code, name, apply_back, rating, cv=100.0, lot=None, listd=None):
    T = dt.date(2026, 8, 12)
    return {"债券代码": code, "债券简称": name,
            "申购日期": T - dt.timedelta(days=apply_back),
            "申购代码": code, "申购上限": 100.0, "正股代码": "300" + code[-3:],
            "正股简称": name + "股份", "正股价": 10.0, "转股价值": cv,
            "债现价": 100.0, "转股溢价率": 20.0,
            "原股东配售-股权登记日": T - dt.timedelta(days=apply_back + 1),
            "原股东配售-每股配售额": 1.0, "发行规模": 5.0,
            "中签号发布日": lot, "中签率": None, "上市时间": listd,
            "信用评级": rating}


# ---------------------------------------------------------------- 1
def test_ipo_gates_report_what_they_ate():
    """两道门略去几条要报数，且 0 条时不许再说「确实没有」。

    v5.9.2 修的是「门写成 continue、跳掉整行」，方向对，但门本身一条都不数。
    于是 min_rating 一开，窗口内的低评级债静默消失，而 _explain_listing_zero
    的第 ③ 支照旧说「窗口内确实没有申购/缴款/上市（接口正常）」—— 明明有。
    同一轮里 fund_premium 的三个开关都补了计数并照说，打新这两个漏了。
    """
    rows = [_ipo_row("123001", "甲转债", 0, "A+"),
            _ipo_row("123002", "乙转债", -2, "A")]

    base = _ipo_run(rows, {"min_rating": ""})
    assert len(base.opportunities) == 2, base.opportunities

    r = _ipo_run(rows, {"min_rating": "AA"})
    assert not r.opportunities, "这两只本来就该被评级门挡在申购之外"
    assert any("2 只" in n and "min_rating" in n for n in r.notes), \
        f"门吃掉 2 条却一个字不说：{r.notes}"
    assert not any("确实没有" in n for n in r.notes), \
        f"窗口内明明有两只，报告不该说「确实没有」：{r.notes}"

    # 转股价值那道门同理，且两道门的措辞要分得开
    r2 = _ipo_run([_ipo_row("123003", "丙转债", 0, "AAA", cv=70.0)],
                  {"min_convert_value": 90})
    assert any("min_convert_value" in n for n in r2.notes), r2.notes

    # 门只关申购这一条：缴款提醒照出（v5.9.2 那条不许被带坏）
    T = dt.date(2026, 8, 12)
    r3 = _ipo_run([_ipo_row("123004", "丁转债", 0, "A", lot=T)],
                  {"min_rating": "AA"})
    assert len(r3.opportunities) == 1 and "缴款" in r3.opportunities[0].action, \
        [o.action for o in r3.opportunities]
    ok("打新两道门略去几条照说，0 条时不再谎称「窗口内确实没有」")


# ---------------------------------------------------------------- 2
def test_ipo_unknown_min_rating_opens_the_gate_not_shuts_it():
    """认不出的 min_rating 一律放行 + 出声，不许静默拦光。

    旧阈值是 `_RATING_RANK.get(min_rating, 99)` —— 认不出就是 99，比 AAA 还高，
    于是一个笔误（小写 / 尾随空格）把整条申购线静默关掉。
    评级列本身带空格也一样中招：`_rating_ok('AA ', 'AA')` 旧代码返回 False。
    """
    from scanner.sources.cb_ipo import _rating_ok
    assert _rating_ok("AAA", "aa+") is True, "配置写小写就把 AAA 拦了"
    assert _rating_ok("AAA", "AA ") is True, "配置带尾随空格就把 AAA 拦了"
    assert _rating_ok("AA ", "AA") is True, "接口给的评级带空格就被拦了"
    assert _rating_ok("A", "AA") is False, "真不达标的还是要拦住"

    r = _ipo_run([_ipo_row("123001", "甲转债", 0, "A+")], {"min_rating": "AA+++"})
    assert len(r.opportunities) == 1, "认不出的门不该拦掉任何一只"
    assert any("认不出" in n and "不设评级门" in n for n in r.notes), r.notes
    ok("min_rating 认不出时放行并出声，评级两侧都归一，不再被一个空格关掉整条线")


# ---------------------------------------------------------------- 3
def test_approved_missing_code_is_unchecked_not_absent():
    """代码取不到 → 「未核对」，不是「未查到发行记录」，也不计 stale。

    旧代码拿 it["code"]（缺失时是字符串「未取到」）去查 cov_idx，必然落空，
    于是印成「未查到发行记录」并挂「距获批 N 天仍未查到发行记录」——
    一次都没查，却说成查过没找到。这一类条目正是 v5.9.2 新放行的那批
    （空代码不再被并条），所以这个口子是那一轮开的。
    """
    T = dt.date(2026, 8, 12)
    row = {"代码": "", "简称": "无代码股份",
           "公告标题": "关于向不特定对象发行可转换公司债券<em>同意注册</em>的批复",
           "公告时间": (T - dt.timedelta(days=150)).strftime("%Y-%m-%d %H:%M:%S"),
           "公告链接": "http://x/1"}
    r = _approved_run([row], [], {})
    o = r.opportunities[0]
    assert o.metrics["发行状态"].startswith("未核对"), o.metrics["发行状态"]
    assert not any("仍未查到发行记录" in f for f in o.flags), o.flags
    assert not any("获批已满" in n for n in r.notes), \
        f"没法核对的不该计进 stale：{r.notes}"
    assert any("未查到发行记录 0 只" in n for n in r.notes), r.notes
    ok("获批栏：正股代码取不到时记「未核对」，不冒充「查过、没找到」")


# ---------------------------------------------------------------- 4
def test_cross_border_skip_counts_only_what_would_have_shown():
    """only_cross_border 略去的条数只数**达到阈值**的，和另外两个开关同口径。"""
    import scanner.sources.fund_premium as fp
    T = dt.date(2026, 8, 12)

    def run(only_cross):
        ctx = Context(cfg={"capital": 100000,
                           "fund_premium": {"only_cross_border": only_cross,
                                            "sanity_median_pct": 99}},
                      today=T, mock=True)
        return fp.FundPremiumSource(ctx).fetch()

    off, on = run(False), run(True)
    lost = len(off.opportunities) - len(on.opportunities)
    said = [n for n in on.notes if "only_cross_border" in n]
    assert said, on.notes
    num = int("".join(ch for ch in said[0].split("只")[0] if ch.isdigit()))
    assert num == lost, f"说略去 {num} 只，实际少了 {lost} 条"
    ok("only_cross_border 报的条数 = 真正少印的条数（不再把没到阈值的也算进去）")


# ---------------------------------------------------------------- 5
def test_disabled_source_says_it_was_turned_off():
    """config 里关掉一个源时，那一栏不许印一个光秃秃的「无」。

    run.py 跳过被关的源 → results 里没有它 → res is None → 旧代码原样印「无」。
    而 _KIND_ORDER 特意保证栏目不消失，两件事凑起来就是：关掉一个源，
    报告里那一栏和「今天没有」长得一模一样，连「取数异常」的旗都没有可挂的。
    """
    cfg = {"sources": {"cb_approved": False}, "capital": 100000}
    ctx = Context(cfg=cfg, today=dt.date(2026, 8, 12), mock=True)
    txt = render_console([SourceResult(kind=Kind.CB_IPO)], ctx)
    seg = txt.split("▎转债获批公告")[1].split("▎")[0]
    assert "关掉" in seg and "sources.cb_approved" in seg, seg
    assert "不是「今天没有」" in seg, seg

    # 没关的时候一个字都不多说
    ctx2 = Context(cfg={"capital": 100000}, today=dt.date(2026, 8, 12), mock=True)
    seg2 = render_console([SourceResult(kind=Kind.CB_IPO)], ctx2) \
        .split("▎转债获批公告")[1].split("▎")[0]
    assert "关掉" not in seg2, seg2
    ok("被 config 关掉的栏目说清是关掉了，没关的照旧只印「无」")


# ---------------------------------------------------------------- 6
def test_window_margin_wording_tracks_the_constant():
    """「已多开 N 个工作日」里的 N 必须来自常量，不许写死在字符串里。"""
    import re
    hits = 0
    for f in (pathlib.Path("scanner/sources/cb_ipo.py"),
              pathlib.Path("scanner/sources/cb_redeem.py")):
        s = f.read_text(encoding="utf-8")
        for m in re.finditer(r"已多开 (\S+) 个工作日", s):
            hits += 1
            assert "WINDOW_UNKNOWN_MARGIN" in m.group(1), \
                f"{f}: 「已多开 {m.group(1)} 个工作日」把边际写死了"
    assert hits == 2, f"预期两处措辞，实际 {hits} 处"
    ok("窗口边际的措辞跟着 utils.WINDOW_UNKNOWN_MARGIN 走，改常量不会印错数")


# ---------------------------------------------------------------- 7
def test_source_registry_has_one_source_of_truth():
    """源名单只有一份权威。

    run.py 里曾另有一份手抄的 name→Kind 表（kmap），只在「源自己没兜住异常」
    那条兜底分支上用 —— 新增源忘了同步时不会平时暴露，会在源真的抛异常那天
    炸成 KeyError，把整个 run 带走。双保险的第二层自己成了单点。
    """
    import run as runner
    from scanner.config import DEFAULTS
    from scanner.models import SOURCE_KEYS
    assert set(runner._SOURCE_MAP) == set(SOURCE_KEYS.values()) == set(DEFAULTS["sources"]), (
        sorted(runner._SOURCE_MAP), sorted(SOURCE_KEYS.values()), sorted(DEFAULTS["sources"]))
    for name, cls in runner._SOURCE_MAP.items():
        assert SOURCE_KEYS[cls.kind] == name, (name, cls.kind)
    src = pathlib.Path("run.py").read_text(encoding="utf-8")
    assert "kmap" not in src, "run.py 里又出现了第二份 name→Kind 手抄表"
    ok("源名单三处一致，兜底分支不再靠第二份手抄表")


# ---------------------------------------------------------------- 8
def test_mock_never_overwrites_the_live_report():
    """--mock / --date 的产出不许占用实盘报告的文件名。

    check.sh 第六项跑的就是 `run.py --mock --format console markdown html`，
    而旧代码写盘只看日期戳 —— **每跑一次离线自检，当天那份实盘报告就被 mock
    覆盖掉**。包里 08-09/10/11/12 四份归档全是 mock 输出（满篇「示例转债」），
    而 STATE.md 还写着「reports/ 里的历史报告是实盘凭据」。
    cb_approved.py 顶部引用的「实盘 08-12 那份里的盖世食品 920826」就是这么没的。
    """
    import shutil
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    canary = "这是实盘报告，不许被覆盖"
    try:
        for stamp in (dt.date.today().strftime("%Y%m%d"), "20260808"):
            (tmp / f"scan_{stamp}.md").write_text(canary, encoding="utf-8")
            (tmp / f"scan_{stamp}.html").write_text(canary, encoding="utf-8")
        cfg = tmp / "c.yaml"
        cfg.write_text(f"output:\n  out_dir: {tmp.as_posix()}\n", encoding="utf-8")

        for extra in (["--mock"], ["--date", "2026-08-08"]):
            subprocess.run([sys.executable, "run.py", "--config", str(cfg),
                            "--format", "markdown", "html", "--exit-zero"] + extra,
                           capture_output=True, timeout=600)
        for p in tmp.glob("scan_*"):
            assert p.read_text(encoding="utf-8") == canary, \
                f"{p.name} 被非实盘产出覆盖了"
        assert list((tmp / "_scratch").glob("*.md")), "非实盘产出应当落在 _scratch/ 下"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok("--mock / --date 写进 reports/_scratch/，实盘归档不再被自检覆盖")


# ==================================================================
# v5.9.4：实盘 08-13 捞出来的一处 —— 门「够不着」的和门「减掉」的混成了一件事
# 两条都先在 v5.9.3 上验过是红的（撞的是行为，不是 ImportError）。
# ==================================================================

T594 = dt.date(2026, 8, 12)


def _ipo594_run(rows, cfg):
    import scanner.sources.cb_ipo as ci
    saved = ci._mock_df
    ci._mock_df = lambda: pd.DataFrame(rows)
    try:
        return ci.CBIpoSource(Context(cfg={"cb_ipo": cfg, "lookahead_days": 10},
                                      today=T594, mock=True)).fetch()
    finally:
        ci._mock_df = saved


def _row594(code, name, rating, cv=100.0):
    return {"债券代码": code, "债券简称": name, "申购日期": T594,
            "申购代码": code, "申购上限": 100.0, "正股代码": "300" + code[-3:],
            "正股简称": name, "正股价": 10.0, "转股价值": cv, "债现价": 100.0,
            "转股溢价率": 20.0, "原股东配售-股权登记日": T594,
            "原股东配售-每股配售额": 1.0, "发行规模": 5.0, "中签号发布日": None,
            "中签率": None, "上市时间": None, "信用评级": rating}


def test_unreadable_rating_is_not_treated_as_below_threshold():
    """接口给的评级认不出时，不许当成「低于一切」拦掉。

    **实盘 08-13 撞上的**：`N特宝转（118074）` 的信用评级列是 `AA+sti`，
    而 `_RATING_RANK` 里只有 `AA+`。v5.9.3 把**配置侧**的认不出改成了放行，
    但数据侧仍是 `_RATING_RANK.get(rating, -1)` —— 同一个错误的镜像。
    门一开，这只 AA+ 级的债会被拦掉，而 v5.9.3 新加的那句话会说
    「评级低于 cb_ipo.min_rating=AA」—— 比静默拦掉更糟：
    **它主动断言了一件代码判定不了的事**。
    """
    from scanner.sources.cb_ipo import _rating_ok

    # 先撞**行为**，别让 ImportError 抢在前面 —— 「新函数不存在」不算验红
    assert _rating_ok("AA+sti", "AA") is True, "认不出的评级被当成不达标拦掉了"
    assert _rating_ok("AA-", "AA") is False, "真不达标的还是要拦住"

    from scanner.sources.cb_ipo import _rating_unreadable
    assert _rating_unreadable("AA+sti", "AA") is True
    assert _rating_unreadable("AA+sti", "") is False, "门没开时不该多话"
    assert _rating_unreadable("AAA", "AA") is False

    r = _ipo594_run([_row594("118074", "N特宝转", "AA+sti"),
                  _row594("123001", "低评级转债", "A")], {"min_rating": "AA"})
    codes = {o.code for o in r.opportunities}
    assert codes == {"118074"}, f"AA+sti 那只该照常出条、A 那只该被拦：{codes}"

    joined = " | ".join(r.notes)
    assert "AA+sti" in joined and "认不出" in joined, joined
    assert "没参与评级门的筛" in joined or "没参与评级门" in joined, joined
    # 关键：不许把「认不出」说成「低于阈值」
    assert "1 只在申购窗口内、但评级低于" in joined, "真被拦的那 1 只还是要报数"
    assert "2 只在申购窗口内、但评级低于" not in joined, \
        f"把认不出的也算进「评级低于」了：{joined}"

    o = [o for o in r.opportunities if o.code == "118074"][0]
    assert any("认不出" in f for f in o.flags), o.flags
    assert all(len(f) <= 60 for f in o.flags), [len(f) for f in o.flags]

    # 门没开时，一个字都不多说
    r2 = _ipo594_run([_row594("118074", "N特宝转", "AA+sti")], {})
    assert not any("认不出" in n for n in r2.notes), r2.notes
    assert not any("认不出" in f for o in r2.opportunities for f in o.flags)
    ok("打新：接口给的评级认不出时照常出条并标出，不冒充「低于阈值」")


def test_missing_convert_value_does_not_pretend_it_passed_the_gate():
    """转股价值没取到时同理：不参与筛，但要说自己没参与过。

    `bad_cv = min_cv and cv is not None and cv < min_cv` 本来就是 fail-open，
    方向对；缺的是「照实说」那一半 —— 空着的那只混在列表里，读起来像它过了筛。
    """
    r = _ipo594_run([_row594("123001", "缺价值转债", "AAA", cv=None),
                  _row594("123002", "正常转债", "AAA", cv=100.0)],
                 {"min_convert_value": 90})
    assert len({o.code for o in r.opportunities}) == 2, "没取到的那只不该被拦"
    joined = " | ".join(r.notes)
    assert "转股价值本次没取到" in joined and "空不等于低" in joined, joined
    o = [o for o in r.opportunities if o.code == "123001"][0]
    assert any("没取到" in f for f in o.flags), o.flags

    # 门没开时不多话
    r2 = _ipo594_run([_row594("123001", "缺价值转债", "AAA", cv=None)], {})
    assert not any("没取到" in n for n in r2.notes), r2.notes
    ok("打新：转股价值没取到时照常出条并标出，不冒充「过了筛」")


# ==================================================================
# v5.9.5：门「够不着」的里面，混进了门「没取到」的
# 两条都先在 v5.9.4 上验过是红的，**撞的是行为不是 ImportError** ——
# 这两条一个新名字都不 import，只看 fetch() 印出来的东西。
# ==================================================================

def test_missing_rating_is_not_called_unreadable():
    """评级这一列**本次没取到**时，不许说成「取值本工具认不出」。

    v5.9.4 把两侧的「认不出」都改成了 fail-open，方向对；但它把**空值**
    也归进了「认不出」那一句。这两件事在本项目里从来是分开的：
    `min_convert_value` 那一半 v5.9.4 自己就分开了（「转股价值本次没取到…
    空不等于低」），评级这一半没有 —— 而 `STATE.md` 写的是「两处都修才对称」。

    「认不出」是**有值、写法不认识**（AA+sti，可能是 AA+ 的后缀写法）；
    「没取到」是**这一格是空的**。前者能把取值印出来让人自己判，后者印不出，
    只能说「这次没拿到」。混成一句的后果是报告里出现
    `窗口内有 3 只的评级取值本工具认不出（nan、、AA+sti）` ——
    那个 `nan` 和那个空串都不是「取值」，而「3 只」后面只列得出 2 个值。
    """
    r = _ipo594_run([_row594("118074", "N特宝转", "AA+sti"),
                     _row594("123001", "没评级转债", None),
                     _row594("123002", "空评级转债", ""),
                     _row594("123003", "低评级转债", "A")],
                    {"min_rating": "AA"})

    codes = {o.code for o in r.opportunities}
    assert codes == {"118074", "123001", "123002"}, \
        f"认不出/没取到的都该照常出条，只有 A 那只该被拦：{codes}"

    joined = " | ".join(r.notes)
    # ① 「认不出」那一句只许说有值的那一只
    unread = [n for n in r.notes if "认不出" in n]
    assert len(unread) == 1, f"「认不出」应当只有一句：{unread}"
    assert "1 只" in unread[0], f"认不出的只有 AA+sti 那 1 只：{unread[0]}"
    assert "AA+sti" in unread[0], unread[0]
    for bad in ("nan", "None", "、、", "（）"):
        assert bad not in unread[0], f"空值漏进「认不出」那一句了（{bad}）：{unread[0]}"

    # ② 「没取到」自成一句，且说清空不等于不达标
    miss = [n for n in r.notes if "没取到" in n]
    assert len(miss) == 1, f"评级没取到应当自成一句：{r.notes}"
    assert "2 只" in miss[0], f"没取到的是 2 只：{miss[0]}"
    assert "评级" in miss[0] and "照常出条" in miss[0], miss[0]

    # ③ 真被拦的那 1 只照旧报数，且不许把认不出/没取到算进去
    assert "1 只在申购窗口内、但评级低于" in joined, joined
    for n in ("2 只在申购窗口内、但评级低于", "3 只在申购窗口内、但评级低于"):
        assert n not in joined, f"把认不出/没取到的算进「评级低于」了：{joined}"
    ok("打新：评级「本次没取到」和「取值认不出」拆成两句，空值不冒充取值")


def test_missing_rating_never_prints_nan_to_the_reader():
    """空评级不许以 `nan` / `None` / 空串的样子进报告。

    `_num` 给 `—`、`fmt_date` 给 `—`、`cb_redeem._blank` 连 `"nan"` 这个
    字符串都拦下来 —— 只有 `cb_ipo` 的 `"评级": str(rating)` 是直通的。
    v5.9.4 新加的那条 flag 把这个老毛病从「metrics 里一个 nan」放大成
    「每只债挂一句『评级「nan」本工具认不出』」，所以连着一起收。
    """
    r = _ipo594_run([_row594("123001", "没评级转债", None),
                     _row594("123002", "空评级转债", ""),
                     _row594("123004", "NaN评级转债", float("nan"))],
                    {"min_rating": "AA"})
    assert len(r.opportunities) == 3, "三只都该出条（fail-open）"

    for o in r.opportunities:
        shown = str(o.metrics.get("评级", ""))
        assert shown not in ("nan", "None", ""), \
            f"{o.code} 的评级印成了「{shown}」—— 空值该印占位符"
        # flag 要说「没取到」，不许拿一个空值去填「认不出「x」」这个句式
        for f in o.flags:
            assert "认不出" not in f, f"{o.code} 空评级被说成认不出：{f}"
            assert "nan" not in f and "None" not in f and "「」" not in f, \
                f"{o.code} 的 flag 里漏出了空值：{f}"
        assert any("没取到" in f for f in o.flags), o.flags
        assert all(len(f) <= 60 for f in o.flags), [len(f) for f in o.flags]

    # 门没开时一个字都不多说（同 v5.9.4 那两条的分寸）
    r2 = _ipo594_run([_row594("123001", "没评级转债", None)], {})
    assert not any("没取到" in n for n in r2.notes), r2.notes
    assert not any(o.flags for o in r2.opportunities), \
        [o.flags for o in r2.opportunities]
    ok("打新：空评级印占位符、flag 说「没取到」，不拿空值去填「认不出」的句式")


def test_no_source_prints_nan_where_a_rating_should_be():
    """「评级」这一列**任何源**都不许把空值印成 `nan` —— 不是只有 cb_ipo。

    这一条是 v5.9.5 自己招出来的：那一轮修了 `cb_ipo` 的 `"评级": str(rating)`，
    并在 `STATE.md` / `HANDOFF.md` 里写了「全项目唯一一列没走占位符的直通」。
    **那句话是错的** —— `cb_allotment` 读的是同一张表（`bond_zh_cov`）的同一列，
    写法也一模一样，空评级照样印 `nan`。

    「修了一处、宣布唯一、下一轮再翻出第二处」正是这个项目反复吃的那个亏
    （v5.9.3 → v5.9.4 → v5.9.5 三轮都是同一个错误的镜像）。所以这条断言
    **按列钉，不按文件钉**：以后谁再往报告里加一处评级展示，这里会红。
    """
    import scanner.sources.cb_allotment as ca
    import scanner.sources.cb_ipo as ci

    row = dict(_row594("123456", "空评级转债", None))
    row["原股东配售-股权登记日"] = T594
    row["原股东配售-每股配售额"] = 1.0
    row["申购日期"] = T594 + dt.timedelta(days=1)

    got = {}

    saved = ci._mock_df
    ci._mock_df = lambda: pd.DataFrame([row])
    try:
        # 打新那栏（申购提醒 + 上市提醒两处 metrics 都要看）
        r = ci.CBIpoSource(Context(cfg={"cb_ipo": {}, "lookahead_days": 10},
                                   today=T594, mock=True)).fetch()
        got["cb_ipo"] = [o.metrics.get("评级") for o in r.opportunities]
        # 配债那栏 —— 同一张表、同一列
        saved_ret = ca._MOCK_RETURNS
        ca._MOCK_RETURNS = {row["正股代码"]: 5.0}
        try:
            r2 = ca.CBAllotmentSource(
                Context(cfg={"cb_allotment": {}, "lookahead_days": 10},
                        today=T594, mock=True)).fetch()
        finally:
            ca._MOCK_RETURNS = saved_ret
        got["cb_allotment"] = [o.metrics.get("评级") for o in r2.opportunities]
    finally:
        ci._mock_df = saved

    for src, vals in got.items():
        assert vals, f"{src} 一条都没出，这条断言什么都没验到"
        for v in vals:
            assert str(v) not in ("nan", "None", "NaN", ""), \
                f"{src} 把空评级印成了「{v}」—— 该印占位符"
    ok("「评级」列在打新与配债两栏都不把空值印成 nan（按列钉，不按文件钉）")


if __name__ == "__main__":
    print("验证本次修复：")
    for fn in (test_fn_name, test_no_bare_lambda_callsites, test_cache_empty_and_refresh,
               test_retry_gives_up, test_trading_window, test_trading_days_between,
               test_section_banner,
               test_frame_health_gate, test_merge_and_dedupe,
               test_lof_basis_label_is_honest, test_redeem_gate_ranking,
               test_only_redeemable_switch,
               test_data_date_from_column_name, test_cross_border_marker_not_a_flag,
               test_nav_drift_is_a_frame_note_not_a_per_item_flag,
               test_sign_cross_check,
               test_lof_missing_side_is_named, test_lof_join_zero,
               test_lof_sanity_median_gate, test_nav_col_whitelist,
               test_code_normalisation,
               test_net_of_fee_and_tiering, test_slippage_folded_into_number,
               test_sizing_and_absolute_profit, test_printed_numbers_multiply_out,
               test_partial_last_position_is_spelled_out,
               test_trade_outside_quote_is_flagged,
               test_action_word_follows_the_verdict,
               test_spread_flag_does_not_ask_to_double_count,
               test_min_profit_yuan_filters_on_money,
               test_no_liquidity_data_is_declared,
               test_liquidity_and_spread_flags,
               test_hint_budget_bites, test_banner_not_attributed_to_previous_item,
               test_footnotes_carry_what_flags_dropped,
               test_allotment_zero_explains_itself,
               test_every_script_compiles,
               test_delist_line_uses_real_shares_not_turnover,
               test_fund_announcements_use_the_fund_column,
               test_premium_side_has_no_fee_column,
               test_report_verify_mock_default,
               test_report_verify_partial_fill,
               test_report_verify_replay,
               # ---- v4.6 ----
               test_unknown_gate_is_its_own_tier,
               test_demote_switch_moves_order_but_never_the_count,
               test_premium_blocked_has_no_discount_wording,
               test_on_floor_scale_never_vanishes_silently,
               test_hint_budget_holds_on_worst_case,
               test_portfolio_total_only_counts_visible_rows,
               test_event_arb_truncation_claim_is_true,
               test_config_normalises_both_keyword_lists,
               test_selfcheck_runs_on_every_report,
               # ---- v4.6.1 ----
               test_off_book_flag_is_bound_to_the_slippage_sentence,
               test_worst_case_with_inverted_quote_still_fits_the_budget,
               test_emphasis_marks_never_reach_console_or_html,
               test_event_arb_renders_newest_first,
               test_premium_action_states_the_status_it_already_fetched,
               # ---- v5.0 ----
               test_listing_reminder_is_a_date_not_a_price,
               test_allotment_unit_follows_the_exchange,
               test_equity_weight_and_breakeven_are_pure_arithmetic,
               test_listing_column_shift_is_caught,
               # ---- 阶段 2 探针（diag_redeem.py）----
               test_redeem_title_classifier_separates_the_opposite_signal,
               test_redeem_date_extractor_flags_what_it_is_unsure_about,
               # ---- v5.1：cb_redeem 源 ----
               test_redeem_window_opens_on_trading_days,
               test_redeem_listed_without_date_never_vanishes,
               test_redeem_countdown_is_never_parsed_as_a_date,
               # ---- v5.1：probe2 回来之后新增的两条 ----
               test_redeem_unknown_tier_does_not_pretend_coverage,
               test_redeem_countdown_html_never_reaches_the_report,
               test_redeem_action_words_pass_the_report_invariants,
               # ---- v5.7：文档腐烂检查（不碰 scanner/，只读 md 和文件名）----
               test_every_probe_archive_is_registered_in_the_handoff,
               test_state_never_hardcodes_an_archive_number,
               test_event_arb_declares_it_when_the_search_hits_the_page_cap,
               # ---- v5.8：换判据（A→B）之后的对账检查 ----
               test_documented_exit_codes_match_the_criteria_in_code,
               # ---- v5.9.1：实盘 08-12 那份报告暴露出来的三处 ----
               test_item_note_reaches_every_format,
               test_approved_truncation_never_eats_the_stale_tier,
               test_approved_marks_private_placement_and_keeps_it_out_of_the_stale_count,
               # ---- v5.9.2：四处静默少给 ----
               test_ipo_gate_never_eats_the_payment_reminder,
               test_trading_window_never_shrinks_past_the_calendar_edge,
               test_approved_missing_code_never_merges_two_companies,
               test_event_arb_zero_says_which_kind_of_zero,
               # ---- v5.9.3：三处静默 + 四处口径 + 一处凭据被覆盖 ----
               test_ipo_gates_report_what_they_ate,
               test_ipo_unknown_min_rating_opens_the_gate_not_shuts_it,
               test_approved_missing_code_is_unchecked_not_absent,
               test_cross_border_skip_counts_only_what_would_have_shown,
               test_disabled_source_says_it_was_turned_off,
               test_window_margin_wording_tracks_the_constant,
               test_source_registry_has_one_source_of_truth,
               test_mock_never_overwrites_the_live_report,
               # ---- v5.9.4：实盘 08-13 的 AA+sti ----
               test_unreadable_rating_is_not_treated_as_below_threshold,
               test_missing_convert_value_does_not_pretend_it_passed_the_gate,
               # ---- v5.9.5：「认不出」里混进了「没取到」----
               test_missing_rating_is_not_called_unreadable,
               test_missing_rating_never_prints_nan_to_the_reader,
               # ---- v5.9.6：v5.9.5 漏掉的第二处（同一列，另一个源）----
               test_no_source_prints_nan_where_a_rating_should_be):
        fn()
    print("\n全部通过。")
