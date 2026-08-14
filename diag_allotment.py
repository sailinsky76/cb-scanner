"""配债一手党专用体检：一次取数，判定「真没标的」还是「代码/接口问题」。

起因：这一栏长期 0 条，而报告只印一个「无」—— 两种完全不同的情况长得一模一样：

  ① 真没标的。股权登记日 = 申购日 −1 个交易日，只有发行公告出来后才有值，
     一只券在 10 天窗口里可见约 2-3 天，空窗完全正常。
  ② 列位移了。akshare 的 bond_zh_cov() 是**按位置**给东财返回的 70 多列命名的
     （`big_df.columns = [...]` 一长串），东财加一列，「原股东配售-股权登记日」
     就静默接到相邻字段上 —— 拿到的全是过去的日期，天天 0 条，且看起来完全正常。

分辨它们靠一条语义不变量：**登记日应该在申购日前 1 天**（跨周末最多 3 天）。
这条在全表上成立 → 列位是对的，0 条就是真的没标的；
不成立 → 两列里至少有一列接错了字段，跟"今天有没有机会"无关。

用法：
    python diag_allotment.py             # 用 config.yaml 里的 lookahead_days
    python diag_allotment.py --days 30   # 放宽窗口，看看更远处有没有

联网跑，约 10-30 秒（bond_zh_cov 要翻页）。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, ".")

from scanner.config import load_config  # noqa: E402
from scanner.utils import fmt_date, parse_date, retry_call, to_float  # noqa: E402
from diag_common import apply_stream_fix  # noqa: E402

REG_COL = "原股东配售-股权登记日"
PS_COL = "原股东配售-每股配售额"
APPLY_COL = "申购日期"


def _hr(title: str) -> None:
    print(f"\n{'-' * 60}\n▎{title}")


def main() -> int:
    apply_stream_fix()          # **必须在任何 print 之前**（见 diag_common）
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="向前看多少天（默认取配置）")
    args = ap.parse_args()

    cfg = load_config("config.yaml")
    look = args.days if args.days is not None else int(cfg.get("lookahead_days", 10))
    today = date.today()
    end = today + timedelta(days=look)

    _hr("1. 取数")
    import akshare as ak
    df, err = retry_call(ak.bond_zh_cov, attempts=3, backoff=(2.0, 5.0))
    if df is None:
        print(f"  ✗ bond_zh_cov 拉取失败：{err}")
        print("  → 这就是取数故障，与配债逻辑无关。先跑 python diag_sources.py")
        return 2
    print(f"  ✓ 取到 {len(df)} 行 × {len(df.columns)} 列")

    _hr("2. 关键列是否还在（列名改版探针）")
    missing = [c for c in (REG_COL, PS_COL, APPLY_COL) if c not in df.columns]
    if missing:
        print(f"  ✗ 缺列：{missing}")
        print(f"  实际列名：{list(df.columns)}")
        print("  → akshare 改了列名或东财改版。这是代码问题，不是没数据。")
        return 2
    print(f"  ✓ 三列都在：{REG_COL} / {PS_COL} / {APPLY_COL}")

    _hr("3. 登记日列的内容")
    regs = [parse_date(v) for v in df[REG_COL]]
    parsed = [d for d in regs if d is not None]
    print(f"  可解析 {len(parsed)} / {len(df)} 行"
          f"（空值 {len(df) - len(parsed)} 行 —— 未排配债计划的券本就为空，正常）")
    if not parsed:
        print("  ✗ 一行都解析不出来 —— 这一列拿到的不是日期。")
        print(f"  前 5 个原始值：{list(df[REG_COL].head())}")
        print("  → 代码/接口问题，不是没数据。")
        return 2
    print(f"  最早 {fmt_date(min(parsed), '%Y-%m-%d')}　"
          f"最晚 {fmt_date(max(parsed), '%Y-%m-%d')}")

    _hr("4. 语义不变量：登记日 = 申购日 − 1 天（列位移探针）")
    diffs = Counter()
    for reg, ap_v in zip(regs, df[APPLY_COL]):
        apply_d = parse_date(ap_v)
        if reg is None or apply_d is None:
            continue
        diffs[(apply_d - reg).days] += 1
    total = sum(diffs.values())
    if total == 0:
        print("  ? 没有一行同时有登记日和申购日，判不了")
    else:
        ok_n = sum(n for d, n in diffs.items() if 0 < d <= 3)
        print(f"  申购日 − 登记日 的分布（前 6 种，共 {total} 行）：")
        for d, n in diffs.most_common(6):
            mark = "  ← 正常" if 0 < d <= 3 else ""
            print(f"    差 {d:>4} 天：{n:>4} 行{mark}")
        rate = ok_n / total
        print(f"  落在 1-3 天的比例：{rate:.1%}")
        if rate < 0.5:
            print("  ✗ 这条关系不成立 → **列位大概率移位了**，两列里至少一列接错字段。")
            print("    对照 akshare 源码里 bond_zh_cov 的 `big_df.columns = [...]`，")
            print("    看东财是不是新增/删除了列导致整体错位。")
            print("  → 这是代码问题。0 条不代表没机会。")
            return 2
        print("  ✓ 关系成立 → 列位是对的，登记日这一列拿到的确实是登记日。")

    _hr(f"5. 窗口内有没有标的（{fmt_date(today, '%Y-%m-%d')} ~ {fmt_date(end, '%Y-%m-%d')}）")
    hits, future = [], []
    for _, r in df.iterrows():
        reg = parse_date(r.get(REG_COL))
        if reg is None or reg < today:
            continue
        ps = to_float(r.get(PS_COL))
        row = (reg, str(r.get("债券简称", "")).strip(),
               str(r.get("正股简称", "")).strip(), ps)
        (hits if reg <= end else future).append(row)

    if hits:
        print(f"  ✓ 窗口内 {len(hits)} 只：")
        for reg, nm, stock, ps in sorted(hits):
            print(f"    {fmt_date(reg, '%Y-%m-%d')}  {nm}／{stock}  每股配售额 {ps}")
        print("  → 报告这一栏本次应该出条。如果实际是 0 条，那才是代码问题。")
    else:
        print(f"  窗口内 0 只。")
    if future:
        print(f"  窗口外还有 {len(future)} 只（放宽 --days 可看到）：")
        for reg, nm, stock, ps in sorted(future)[:5]:
            print(f"    {fmt_date(reg, '%Y-%m-%d')}  {nm}／{stock}")

    _hr("结论")
    if hits:
        print("  接口正常、列位正确、窗口内有标的 —— 出 0 条就是 bug，贴报告给我。")
        return 0
    if future:
        print("  接口正常、列位正确，只是最近的登记日还在窗口外。")
        print(f"  0 条是真的 0，不是 bug。想早点看到就把 lookahead_days 调大。")
    else:
        print("  接口正常、列位正确，表里当前没有任何未来的登记日。")
        print("  0 条是真的 0：登记日要等发行公告才有值，通常只提前 2-3 个交易日，")
        print("  一只券在窗口里可见约 2-3 天 —— 空窗期出 0 条属正常。")
        print("  按 2026 年约 7 只/月的发行节奏，每月应能命中若干次；")
        print("  若连续几周一次都不中，把这份输出留着，那就不是节奏问题了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
