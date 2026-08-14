"""用 2026-08-08 实盘日志里的 9 条可赎回折价回放 v4.2 的排序。

这不是单元测试（数据是硬抄日志的），是留档：v4.2 把折价侧的档内排序从「按幅度」
改成「按绝对收益」，依据就是这一次回放 —— 9 条的名次全变了。
跑法：python replay_20260808.py
"""
import sys, types, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
import scanner.sources.fund_premium as fp
from scanner.sources.base import Context

# 代码, 名称, 最新价, 折价率%, 日成交额(万), 买一, 卖一   —— 全部抄自 08-08 日志
#
# v4.2 首版这张表把 165516 / 501098 的卖一写成了 None，因为 20:38 那份日志里
# 它们**没有买卖价差提示**，我据此推断"没有盘口"。20:56 的实盘输出证明这是错的：
# 价差提示只在超过 spread_alert_pct(0.5%) 时才印，低于阈值的价差照样存在、照样
# 吃钱 —— 这两只的滑点分别是 0.50 和 0.73 个百分点，各差 77 元和 85 元。
# 教训：**提示没出现 ≠ 那个量为零**，而这正是把滑点折进数值里的理由 ——
# 靠阈值提示，低于阈值的成本就永远看不见。
# 买一由 21:03 那次运行的「落在买一/卖一之外」提示反查得到：
# 165516 [7.734, 7.772]、501098 [1.735, 1.740] —— 两只的价差分别只有
# 0.49% 和 0.29%，都压在 spread_alert_pct(0.5%) 底下，所以老版全程噤声。
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

empty = pd.DataFrame(columns=["代码", "名称", "最新价", "IOPV实时估值"])
fake = types.ModuleType("akshare")
fake.fund_etf_spot_em = fake.fund_etf_fund_daily_em = lambda: empty
sys.modules["akshare"] = fake
fp._INTER_CALL_GAP = 0
fp._lof_prices_sina = lambda retries, today: (prices, None)
fp._lof_navs_ths = lambda retries, today: (navs, {}, None)

ctx = Context(cfg={"capital": 100000,
                   "fund_premium": {"max_discount": 0, "sanity_median_pct": 99}},
              today=dt.date(2026, 8, 8))
res = fp.FundPremiumSource(ctx).fetch()

log_codes = {c for c, *_ in LOG}

# 源层有三种口径键名：折价路径 折价率(%)、溢价路径 溢价率(%)、集思录增强 T-1溢价率(%)。
# 这里只做展示，取到哪个用哪个 —— 不能假设每条都是折价条目（那正是这一版的崩溃点）。
def _rate(m):
    for k in ("折价率(%)", "溢价率(%)", "T-1溢价率(%)"):
        if k in m:
            return m[k]
    return "—"

print(f"{'':2}{'代码':<8}{'名称':<15}{'折价%':>7}{'净收益%':>9}{'可投万':>7}{'预估元':>7}")
for i, o in enumerate(res.opportunities, 1):
    m = o.metrics
    mark = "" if o.code in log_codes else "  ← 不在 08-08 日志里"
    print(f"{i:<2}{o.code:<8}{o.name:<15}{_rate(m):>7}"
          f"{m.get('净收益(%)', '—'):>9}{m.get('可投(万)', '—'):>7}"
          f"{m.get('预估(元)', '—'):>7}{mark}")

extra = [o.code for o in res.opportunities if o.code not in log_codes]
if extra:
    print(f"\n[!] 多出 {len(extra)} 条不在 08-08 日志里：{extra}")
    print("  回放本该只跑 LOF 那一路（ETF 帧被 fake akshare 打空）。")
    print("  多出条目 = ETF 帧真的载入了，看下面的 notes 确认是哪一路。")

old = [c for c, *_ in sorted(LOG, key=lambda x: -x[3])]
new = [o.code for o in res.opportunities if o.code in log_codes]
moved = sum(a != b for a, b in zip(old, new))
print(f"\n按幅度排 {old}")
print(f"按元排   {new}")
print(f"名次变动：{moved}/{len(old)}")
for n in res.notes:
    print("\n·", n)