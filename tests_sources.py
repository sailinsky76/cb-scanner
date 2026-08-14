#!/usr/bin/env python3
"""数据源级回归测试：`python tests_sources.py`

用一张**故意做脏**的伪 bond_zh_cov 表（NaT / nan / '-' / 8位整数 / 空串混在一起）
直接喂给 cb_ipo 和 cb_allotment —— 这正是当初实盘崩溃的那张表的形状：
`bond_zh_cov` 里没排配债计划的转债，「原股东配售-股权登记日」整列是 NaT。

再单独验一遍事件套利的 <em> 清洗与链接编码，以及 markdown 链接不被空格截断。
"""
import sys
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, ".")
from scanner.sources.base import Context           # noqa: E402
from scanner.sources import (CBAllotmentSource, CBIpoSource,  # noqa: E402
                             EventArbSource)
from scanner.report import render_markdown, render_html, render_console  # noqa: E402

T = date(2026, 8, 8)
fails = []


def check(label, cond, extra=""):
    if not cond:
        fails.append(f"{label} {extra}")
    print(f"  {'[PASS]' if cond else '[FAIL]'} {label}{(' ' + extra) if extra else ''}")


def dirty_bond_df() -> pd.DataFrame:
    """列名对齐 bond_zh_cov()，日期列刻意混入各种脏值。"""
    return pd.DataFrame([
        # 正常行：今天申购、明天登记
        {"债券代码": "123999", "债券简称": "正常转债", "申购日期": T,
         "申购代码": "123999", "申购上限": 100.0, "正股代码": "300999", "正股简称": "正常股份",
         "正股价": 15.3, "转股价值": 98.5, "转股溢价率": 25.0, "发行规模": 8.2,
         "原股东配售-股权登记日": T + timedelta(days=1), "原股东配售-每股配售额": 1.3,
         "中签号发布日": pd.NaT, "中签率": float("nan"), "信用评级": "AA"},
        # ★ 当初崩溃的行：未排配债计划 → 登记日 NaT、每股配售额 NaN
        {"债券代码": "113888", "债券简称": "无配债转债", "申购日期": pd.NaT,
         "申购代码": "113888", "申购上限": 100.0, "正股代码": "600888", "正股简称": "无配股份",
         "正股价": 8.6, "转股价值": 105.0, "转股溢价率": 18.0, "发行规模": 15.0,
         "原股东配售-股权登记日": pd.NaT, "原股东配售-每股配售额": float("nan"),
         "中签号发布日": T, "中签率": 0.0123, "信用评级": "AA+"},
        # 8 位整数日期（akshare 偶发）
        {"债券代码": "123777", "债券简称": "整数日期转债", "申购日期": 20260810,
         "申购代码": "123777", "申购上限": 100.0, "正股代码": "300777", "正股简称": "小盘股份",
         "正股价": 9.5, "转股价值": 82.0, "转股溢价率": 40.0, "发行规模": 3.1,
         "原股东配售-股权登记日": "20260809", "原股东配售-每股配售额": 0.8,
         "中签号发布日": None, "中签率": None, "信用评级": "A+"},
        # 全脏行：'-' / 空串 —— 应该被安静跳过，不产出、不报错
        {"债券代码": "127000", "债券简称": "脏数据转债", "申购日期": "-",
         "申购代码": "", "申购上限": "-", "正股代码": "", "正股简称": "",
         "正股价": "-", "转股价值": "-", "转股溢价率": "-", "发行规模": "-",
         "原股东配售-股权登记日": "", "原股东配售-每股配售额": "-",
         "中签号发布日": "—", "中签率": "-", "信用评级": ""},
    ])


print("\n[1] cb_ipo / cb_allotment 吃脏表不崩（当初的 NaT 崩溃点）")
import copy                                        # noqa: E402
from scanner.config import DEFAULTS                # noqa: E402
ctx = Context(cfg=copy.deepcopy(DEFAULTS), today=T, mock=True)
df = dirty_bond_df()

import scanner.sources.cb_ipo as m_ipo            # noqa: E402
import scanner.sources.cb_allotment as m_allot    # noqa: E402
m_ipo._mock_df = lambda: df
m_allot._mock_df = lambda: df
m_allot._MOCK_RETURNS = {"300999": 12.0, "300777": 8.0}

try:
    r_ipo = CBIpoSource(ctx).fetch()
    check("cb_ipo.fetch() 未抛异常", True, f"命中 {len(r_ipo.opportunities)} 条")
except Exception as e:
    check("cb_ipo.fetch() 未抛异常", False, f"{type(e).__name__}: {e}")
    r_ipo = None

try:
    r_allot = CBAllotmentSource(ctx).fetch()
    check("cb_allotment.fetch() 未抛异常", True, f"命中 {len(r_allot.opportunities)} 条")
except Exception as e:
    check("cb_allotment.fetch() 未抛异常", False, f"{type(e).__name__}: {e}")
    r_allot = None

if r_ipo is not None:
    names = {o.name for o in r_ipo.opportunities}
    check("正常转债 命中申购", "正常转债" in names)
    check("整数日期转债 命中（20260810 未被当成 1970）",
          any(o.name == "整数日期转债" and o.action_date == date(2026, 8, 10)
              for o in r_ipo.opportunities))
    check("无配债转债 命中缴款提醒（中签号发布日=今天）",
          any(o.name == "无配债转债" and "缴款" in o.action for o in r_ipo.opportunities))
    check("脏数据转债 未误报", "脏数据转债" not in names)

if r_allot is not None:
    anames = {o.name.split("／")[0] for o in r_allot.opportunities}
    check("NaT 登记日的行被跳过（不是崩溃）", "无配债转债" not in anames)
    check("正常转债 命中配债", "正常转债" in anames)
    check("字符串 '20260809' 登记日被正确解析",
          any(o.action_date == date(2026, 8, 9) for o in r_allot.opportunities))

print("\n[2] event_arb 的 <em> 清洗与链接编码")
r_ev = EventArbSource(ctx).fetch()
titles = [o.metrics.get("公告", "") for o in r_ev.opportunities]
links = [o.link for o in r_ev.opportunities]
check("命中条数 > 0", len(r_ev.opportunities) > 0, f"{len(r_ev.opportunities)} 条")
check("标题里没有残留标签", all("<" not in t and ">" not in t for t in titles))
check("标题内容完整（关键词没被连带删掉）",
      any("要约收购" in t for t in titles), str(titles[:1]))
check("链接里没有裸空格", all(" " not in l for l in links))
check("链接空格已编码为 %20", all("%20" in l for l in links if "announcementTime" in l))
check("时间显示已去掉 00:00:00",
      all(not str(o.metrics.get("时间", "")).endswith("00:00:00") for o in r_ev.opportunities))

print("\n[3] 三种报告都能渲染，且 markdown 链接不被空格截断")
results = [x for x in (r_ipo, r_allot, r_ev) if x is not None]
for name, fn in (("console", render_console), ("markdown", render_markdown), ("html", render_html)):
    try:
        txt = fn(results, ctx)
        check(f"{name} 渲染成功", len(txt) > 0, f"{len(txt)} 字符")
        if name == "markdown":
            import re
            bad = [l for l in re.findall(r"\[公告链接\]\(([^)]*)\)", txt) if " " in l]
            check("markdown 链接内无空格（否则语法截断）", not bad, str(bad[:1]))
        if name == "html":
            check("html 里没有生效的 <em> 高亮", "<em>" not in txt)
            # v5.9.1：HTML 这一路曾经整个不印 o.note —— console/markdown 都印，
            # 只有它没有，于是打新那句「信用申购，申购日无需资金…」在每天真正被读的
            # 那份报告里一条都没出现过。
            check("html 印了条目级「注」（曾整个漏掉）", "信用申购" in txt)
    except Exception as e:
        check(f"{name} 渲染成功", False, f"{type(e).__name__}: {e}")

print("\n" + "=" * 60)
if fails:
    print(f"[FAIL] {len(fails)} 项未通过：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("[PASS] 全部通过")
