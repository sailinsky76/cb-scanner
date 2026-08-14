#!/usr/bin/env python3
"""utils.py 针对性自测：跑 `python tests_utils.py`，全绿才算修好。

覆盖三类历史故障：
  A. NaT / 空值 —— 当初配债源崩溃的直接原因
  B. 8 位整数日期 —— 被 pd.to_datetime 当纳秒时间戳解析成 1970-01-01
  C. <em> 标签与链接裸空格 —— 报告显示与 markdown 链接截断
"""
import sys
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, ".")
from scanner.utils import (clean_url, days_ago, fmt_date, parse_date,  # noqa: E402
                           safe_call, strip_html, to_float, within)

fails = []


def eq(label, got, want):
    ok = got == want
    if not ok:
        fails.append(f"{label}: 期望 {want!r}，实到 {got!r}")
    print(f"  {'[PASS]' if ok else '[FAIL]'} {label:<46} -> {got!r}")


print("\n[A] parse_date —— 空值：NaT 必须返回 None（当初的崩溃点）")
eq("parse_date(pd.NaT)", parse_date(pd.NaT), None)
eq("parse_date(None)", parse_date(None), None)
eq("parse_date(float('nan'))", parse_date(float("nan")), None)
eq("parse_date('')", parse_date(""), None)
eq("parse_date('-')", parse_date("-"), None)
eq("parse_date('—')", parse_date("—"), None)
eq("parse_date('NaT')", parse_date("NaT"), None)
eq("parse_date('nan')", parse_date("nan"), None)
eq("parse_date('暂无')", parse_date("暂无"), None)
eq("parse_date('不是日期')", parse_date("不是日期"), None)

print("\n[A'] within —— 含 NaT 的比较必须是 False，不能抛 TypeError")
t = date(2026, 8, 8)
eq("within(NaT, t, t+10)", within(pd.NaT, t, t + timedelta(days=10)), False)
eq("within(None, t, t+10)", within(None, t, t + timedelta(days=10)), False)
eq("within(nan, t, t+10)", within(float("nan"), t, t + timedelta(days=10)), False)
eq("within(t, t, t) 边界闭区间", within(t, t, t), True)
eq("within(t, NaT, t+10) 起点脏", within(t, pd.NaT, t + timedelta(days=10)), False)
eq("within(Timestamp, t, t+10)", within(pd.Timestamp("2026-08-09"), t, t + timedelta(days=10)), True)
eq("within(datetime, t, t+10)", within(datetime(2026, 8, 9, 15, 30), t, t + timedelta(days=10)), True)
eq("within 区间传反了也能判", within(date(2026, 8, 9), t + timedelta(days=10), t), True)

print("\n[B] parse_date —— 8 位整数不能被当成纳秒时间戳")
eq("parse_date(20260812) int", parse_date(20260812), date(2026, 8, 12))
eq("parse_date('20260812') str", parse_date("20260812"), date(2026, 8, 12))
eq("parse_date(20260812.0) float", parse_date(20260812.0), date(2026, 8, 12))
eq("parse_date(12345) 非日期数值", parse_date(12345), None)
eq("参照：pd.to_datetime(20260812) 会错成", pd.to_datetime(20260812).date(), date(1970, 1, 1))

print("\n[B'] parse_date —— 常规写法")
eq("parse_date('2026-08-12')", parse_date("2026-08-12"), date(2026, 8, 12))
eq("parse_date('2026/08/12')", parse_date("2026/08/12"), date(2026, 8, 12))
eq("parse_date('2026.08.12')", parse_date("2026.08.12"), date(2026, 8, 12))
eq("parse_date('2026年8月12日')", parse_date("2026年8月12日"), date(2026, 8, 12))
eq("parse_date('2026-08-12 09:30:00')", parse_date("2026-08-12 09:30:00"), date(2026, 8, 12))
eq("parse_date('2026-08-12T09:30:00')", parse_date("2026-08-12T09:30:00"), date(2026, 8, 12))
eq("parse_date(Timestamp)", parse_date(pd.Timestamp("2026-08-12 10:00")), date(2026, 8, 12))
eq("parse_date(date)", parse_date(date(2026, 8, 12)), date(2026, 8, 12))

print("\n[B''] fmt_date / days_ago")
eq("fmt_date(date)", fmt_date(date(2026, 8, 12)), "08-12")
eq("fmt_date(NaT) 不显示 None", fmt_date(pd.NaT), "—")
eq("fmt_date(None)", fmt_date(None), "—")
eq("days_ago(7, t)", days_ago(7, t), date(2026, 8, 1))
eq("days_ago(-3, t) 负数按 0", days_ago(-3, t), t)

print("\n[C] to_float —— 百分号只脱帽，不除 100")
eq("to_float('3.10%')", to_float("3.10%"), 3.10)
eq("to_float('-4.20%')", to_float("-4.20%"), -4.20)
eq("to_float('1,234.5')", to_float("1,234.5"), 1234.5)
eq("to_float('1.2万')", to_float("1.2万"), 12000.0)
eq("to_float(NaT)", to_float(pd.NaT), None)
eq("to_float('-')", to_float("-"), None)
eq("to_float(nan)", to_float(float("nan")), None)
eq("to_float(True) 布尔不当数字", to_float(True), None)

print("\n[D] strip_html —— 巨潮的 <em> 高亮")
eq("strip_html 单个 em",
   strip_html("关于<em>要约收购</em>报告书摘要的提示性公告"),
   "关于要约收购报告书摘要的提示性公告")
eq("strip_html 多个 em",
   strip_html("<em>换股</em>吸收合并暨<em>关联交易</em>公告"),
   "换股吸收合并暨关联交易公告")
eq("strip_html 实体反转义", strip_html("A&amp;B 重组&nbsp;公告"), "A&B 重组 公告")
eq("strip_html 字面转义标签保留", strip_html("标题含 &lt;em&gt; 字样"), "标题含 <em> 字样")
eq("strip_html(NaT) 不显示 NaT", strip_html(pd.NaT), "")
eq("strip_html(nan) 不显示 nan", strip_html(float("nan")), "")

print("\n[E] clean_url —— 公告链接里的裸空格")
raw = ("http://www.cninfo.com.cn/new/disclosure/detail?stockCode=600123"
       "&announcementId=1234567890&orgId=gssh0600123"
       "&announcementTime=2026-08-01 00:00:00")
got = clean_url(raw)
eq("clean_url 空格 → %20", got.endswith("announcementTime=2026-08-01%2000:00:00"), True)
eq("clean_url 不破坏 & 分隔", got.count("&"), 3)
eq("clean_url 不破坏 :", "http://" in got and "00:00:00" in got.replace("%20", " "), True)
eq("clean_url 幂等（不二次编码）", clean_url(got), got)
eq("clean_url 空值", clean_url(pd.NaT), "")
eq("clean_url 非 URL 原样", clean_url("暂无链接"), "暂无链接")

print("\n[F] safe_call")
eq("safe_call 成功", safe_call(lambda x: x * 2, 21), (42, None))
r, e = safe_call(lambda: 1 / 0)
eq("safe_call 失败返回 None", r, None)
eq("safe_call 带错误类型名", e.startswith("ZeroDivisionError"), True)

print("\n" + "=" * 60)
if fails:
    print(f"[FAIL] {len(fails)} 项未通过：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("[PASS] 全部通过")
