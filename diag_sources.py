"""联网自检：确认 fund_premium 依赖的两个接口在你的网络下是否可用。

    py -3.11 diag_sources.py

为什么单独写一个：这两个接口分别走不同的 host，
    fund_etf_spot_em       → push2delay.eastmoney.com
    fund_etf_fund_daily_em → fund.eastmoney.com
    fund_lof_spot_em       → 88.push2.eastmoney.com   （v3 已弃用）
线上那次 LOF 连续三次 RemoteDisconnected，就是 88.push2 这个分片不通，
而不是限流 —— 特征是每次都在同样的耗时上以同样方式失败，重试没有意义。

脚本会对每个接口报告：耗时、行数、返回列、以及**能不能算出折溢价**。
最后一项才是关键：接口通了不等于有用，v2 的 fund_lof_spot_em 就是通了也没用。
"""
import sys
import time

from diag_common import apply_stream_fix

REQUIRED_ANY = ("IOPV", "实时估值", "参考净值", "折价率", "溢价率")


def probe(api_name: str, deprecated: str = "") -> dict:
    import akshare as ak

    fn = getattr(ak, api_name, None)
    if fn is None:
        return {"api": api_name, "ok": False, "why": "akshare 无此接口（建议 pip install -U akshare）"}

    t0 = time.time()
    try:
        df = fn()
    except Exception as e:
        return {"api": api_name, "ok": False, "sec": time.time() - t0,
                "why": f"{type(e).__name__}: {e}", "deprecated": deprecated}
    sec = time.time() - t0

    cols = [str(c) for c in df.columns]
    usable = [c for c in cols if any(k in c for k in REQUIRED_ANY)]
    return {"api": api_name, "ok": True, "sec": sec, "rows": len(df),
            "cols": cols, "usable": usable, "deprecated": deprecated}


def show(r: dict):
    print(f"\n{'─' * 68}\n{r['api']}")
    if r.get("deprecated"):
        print(f"  【已弃用】{r['deprecated']}")
    if not r["ok"]:
        sec = f"（耗时 {r['sec']:.1f}s）" if "sec" in r else ""
        print(f"  ✗ 取数失败{sec}：{r['why']}")
        return
    print(f"  ✓ 取数成功：{r['rows']} 行，耗时 {r['sec']:.1f}s")
    print(f"  返回列（{len(r['cols'])}）：{', '.join(r['cols'][:14])}"
          + ("…" if len(r["cols"]) > 14 else ""))
    if r["usable"]:
        print(f"  ✓ 可用于折溢价的列：{', '.join(r['usable'])}")
    else:
        print("  ✗ 没有任何估值/折价率列 —— 接口通了也算不出折溢价")


def main():
    apply_stream_fix()          # **必须在任何 print 之前**（probe7 就是倒在这里）
    try:
        import akshare as ak
    except ImportError:
        print("未安装 akshare：pip install -U akshare")
        return 2
    print(f"akshare {getattr(ak, '__version__', '?')} | python {sys.version.split()[0]}")

    results = [
        probe("fund_etf_spot_em"),
        probe("fund_etf_fund_daily_em"),
        probe("fund_lof_spot_em",
              deprecated="v3 已不再使用：返回列无 IOPV 也无折价率，且 host 是 88.push2"),
    ]
    for r in results:
        show(r)

    spot, daily = results[0], results[1]
    healthy = [r for r in (spot, daily) if r["ok"] and r.get("usable")]

    print(f"\n{'═' * 68}\n结论：")
    if len(healthy) == 2:
        print("  两路都正常，折价与溢价两侧都有数据。")
    elif not any(r["ok"] for r in (spot, daily)):
        print("  两路都不通 —— 先查本机网络 / 代理 / 是否被防火墙拦了 eastmoney。")
    elif daily in healthy and spot not in healthy:
        print("  只有场内日行情可用：折价侧正常，跨境溢价侧本次缺失。")
    elif spot in healthy and daily not in healthy:
        print("  只有 ETF 实时行情可用：**折价侧不可用**，报告里的「折价 0 条」"
              "不代表没机会。")
        print("  可试：pip install -U akshare；确认 lxml 已安装"
              "（fund_etf_fund_daily_em 走 pd.read_html）。")
    else:
        print("  接口能连上但缺少估值列 —— 多半是接口改版，先升级 akshare。")

    if results[2]["ok"] and not results[2]["usable"]:
        print("\n  旁证：fund_lof_spot_em 即使取数成功也没有估值列 —— "
              "这正是它被弃用的原因，不是网络问题。")
    return 0 if len(healthy) == 2 else 1


if __name__ == "__main__":
    sys.exit(main())
