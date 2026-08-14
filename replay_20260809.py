"""用 2026-08-09 实盘输出回放 v4.6 的改动。

留档，不是单元测试（数据硬抄自当天日志）。要回答的是一个具体问题：
**这一轮改的是措辞、分档和提示预算，那它有没有顺手动了当天的数值和名次？**

跑法：python replay_20260809.py

当天那份报告的关键数（改动前）：
  折价侧 31 条 → 8 可兑现 / 1 成本吃光 / 22 赎回暂停
  组合合计 1,096 元，前 2 条占 74%
  溢价侧 37 条 → 27「可兑现」/ 10 申购暂停   ← 这 27 条正是本轮要修的谎
"""
import datetime as dt
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd  # noqa: E402

import scanner.sources.fund_premium as fp  # noqa: E402
from scanner.report import render_console  # noqa: E402
from scanner.sources.base import Context  # noqa: E402
from verify_report import verify_console  # noqa: E402

# 代码, 名称, 最新价, 折价率%, 日成交额(万), 买一, 卖一, 场内份额(万份)
# 盘口由当天的「建仓要吃卖一 X」「在买一/卖一 [a, b] 之外」两类提示反查得到；
# 没印过盘口提示的（501220 / 506005）当天就没有可用买卖盘 → None。
# 场内份额 = 当天印出的「场内规模(万)」÷ 单位净值；沪市段拿不到 → None。
LOG = [
    ("160220", "国泰民益LOF",     4.499, 3.51,  230.3, 4.474, 4.499, 2486.0),
    ("506005", "科创板博时",      1.539, 2.27, 1575.6, None,  None,  None),
    ("501220", "行业轮动FOF",     0.960, 3.17,   11.7, None,  None,  None),
    ("161131", "易方达科润LOF",   1.001, 3.65,   21.0, 1.001, 1.015, 4113.0),
    ("165516", "中信保诚周期LOF", 7.732, 2.52,   30.4, 7.734, 7.772, 3091.0),
    ("160215", "国泰价值LOF",     3.552, 2.87,   25.7, 3.552, 3.588,  764.0),
    ("169201", "浙商鼎盈LOF",     1.842, 2.52,   34.4, 1.842, 1.856,  252.0),
    ("501098", "科创建信LOF",     1.727, 2.60,   23.1, 1.735, 1.740, None),
    ("501217", "行业配置FOF",     1.041, 2.34,    1.9, 1.039, 1.060, None),
]
# 赎回暂停那条：折价最大，但走不通 —— 用来验它仍排在最后
BLOCKED = ("506008", "科创板长城", 1.242, 9.06, 514.1)

# 当天溢价侧前 10 全是跨境 ETF，走 ETF 帧 → 申购状态一个都没有
CROSS_ETF = [
    ("159509", "纳指科技ETF景顺",       2.843, 24.69),
    ("513310", "中韩半导体ETF华泰柏瑞", 4.928, 17.81),
    ("513100", "纳指ETF国泰",           2.266, 13.97),
    ("159941", "纳指ETF广发",           1.687, 13.29),
]

prices, navs, shares = {}, {}, {}
for code, name, px, disc, wan, bid, ask, onfloor_wan in LOG:
    nav = px / (1 - disc / 100)
    prices[code] = {"name": name, "px": px, "amt": wan * 1e4, "bid": bid, "ask": ask}
    navs[code] = {"nav": nav, "basis": "最新-单位净值", "redeem": "开放",
                  "sub": "开放", "date": "2026-08-07"}
    if onfloor_wan:                       # 份额 = 场内规模 ÷ 单位净值
        shares[code] = onfloor_wan * 1e4 / nav

code, name, px, disc, wan = BLOCKED
prices[code] = {"name": name, "px": px, "amt": wan * 1e4, "bid": None, "ask": None}
navs[code] = {"nav": px / (1 - disc / 100), "basis": "最新-单位净值",
              "redeem": "暂停", "sub": "暂停", "date": "2026-08-07"}

spot = pd.DataFrame([{"代码": c, "名称": n, "最新价": px,
                      "IOPV实时估值": px / (1 + prem / 100), "数据日期": "2026-08-07"}
                     for c, n, px, prem in CROSS_ETF])
empty = pd.DataFrame(columns=["基金代码", "基金简称", "市价", "折价率"])

fake = types.ModuleType("akshare")
fake.fund_etf_spot_em = lambda: spot
fake.fund_etf_fund_daily_em = lambda: empty
sys.modules["akshare"] = fake
fp._INTER_CALL_GAP = 0
fp._lof_prices_sina = lambda retries, today: (prices, None)
fp._lof_navs_ths = lambda retries, today: (navs, {}, None)
fp._lof_szse_shares = lambda retries, today: (shares, None)


def run(**over):
    cfg = {"max_discount": 10, "max_premium": 10, "sanity_median_pct": 99}
    cfg.update(over)
    ctx = Context(cfg={"capital": 100000, "fund_premium": cfg},
                  today=dt.date(2026, 8, 9))
    return fp.FundPremiumSource(ctx).fetch(), ctx


res, ctx = run()

print("折价侧（改动后）")
print(f"{'':2}{'代码':<8}{'名称':<15}{'折价%':>7}{'净收益%':>9}{'可投万':>7}{'预估元':>7}"
      f"  {'场内规模万':>9}  提示")
for i, o in enumerate(res.opportunities, 1):
    m = o.metrics
    if "折价率(%)" not in m:
        continue
    print(f"{i:<2}{o.code:<8}{o.name:<15}{m['折价率(%)']:>7}"
          f"{m.get('净收益(%)', '—'):>9}{m.get('可投(万)', '—'):>7}"
          f"{m.get('预估(元)', '—'):>7}  {m.get('场内规模(万)', '—'):>9}"
          f"  {len(o.flags)} 句 / {len('；'.join(o.flags))} 字")

print("\n栏目级说明")
for n in res.notes:
    print("  ·", n)

# ---- 与 08-09 那份报告逐项对账 -------------------------------------------
by = {o.code: o for o in res.opportunities}
LIVE = {"160220": (1.98, 3.0, 594), "506005": (0.74, 3.0, 222),
        "501220": (1.64, 0.58, 95), "161131": (0.77, 1.05, 81),
        "165516": (0.48, 1.52, 73), "160215": (0.36, 1.29, 46),
        "169201": (0.25, 1.72, 43), "501098": (0.34, 1.16, 39),
        "501217": (-0.98, None, None)}
# 容差不是"放水"，是这份回放的**输入精度**：净值是我拿印出来的折价率(2 位小数)
# 反推的、成交额是印出来的 1 位小数，真值落在一个区间里。区间两端算出来的净收益
# 相差约 0.01 个百分点 —— 实测 165516 取 2.515% 得 0.48、取 2.520% 得 0.49，
# 两个都在打印精度内。所以 ±0.01pt / ±1 元 以内视为一致；超出才是代码动了数。
bad = []
for code, (net, wan, yuan) in LIVE.items():
    m = by[code].metrics
    got = m.get("净收益(%)")
    if got is None or abs(got - net) > 0.011:
        bad.append(f"{code} 净收益 {got} 与实盘 {net} 差得超出输入精度")
    if wan is not None and abs(m.get("可投(万)", -9) - wan) > 0.011:
        bad.append(f"{code} 可投 {m.get('可投(万)')} ≠ 实盘 {wan}")
    if yuan is not None and abs(m.get("预估(元)", -9) - yuan) > 1:
        bad.append(f"{code} 预估 {m.get('预估(元)')} ≠ 实盘 {yuan}")

order = [o.code for o in res.opportunities if "折价率(%)" in o.metrics]
if order[:8] != list(LIVE)[:8]:
    bad.append(f"折价侧名次变了：{order[:8]}")
total = [n for n in res.notes if "合计预估" in n]
import re as _re
got_total = int(_re.search(r"合计预估 ([\d,]+) 元", total[0]).group(1).replace(",", ""))
if abs(got_total - 1096) > 3:            # 8 条各差 ≤1 元，合计容差给 3
    bad.append(f"组合合计 {got_total} 元，实盘 1,096 元")

errs = verify_console(render_console([res], ctx))
print("\n对账")
print("  数值与名次：", "与实盘一致（差异在输入精度内）" if not bad else "有变化 ↓")
for b in bad:
    print("    ✗", b)
print("  报告级自检：", "通过（5 条不变量）" if not errs else f"未通过 {errs}")

worst = max(res.opportunities, key=lambda o: len("；".join(o.flags)))
print(f"  最长的一条提示：{worst.code} {len(worst.flags)} 句 / "
      f"{len('；'.join(worst.flags))} 字（预算 3 句 / 150 字）")

# ---- 开关的影响 ------------------------------------------------------------
# 注意：当天溢价侧前 10 **全是**跨境 ETF，申购状态一个都没取到，所以这份回放里
# 两种设置看不出差别 —— 要有差别，得有「申购确认开放」的境内 LOF 跟它们抢名次。
# 那种数据这份日志里没有，所以开关的效果用 selftest 里的合成用例验，不在这里冒充。
for flag in (True, False):
    r, _ = run(demote_unknown_gate=flag)
    prem = [o.code for o in r.opportunities if "溢价率(%)" in o.metrics]
    note = [n for n in r.notes if n.startswith("溢价侧") and "可执行度" in n]
    print(f"\ndemote_unknown_gate={str(flag):<5} 溢价侧次序 {prem}")
    print(f"{'':30}{note[0] if note else '（无分档说明）'}")
