"""探针 v2：查清 LOF 折价侧为什么没有数据，并当场试出能不能修好。

    py -3.11 diag_lof_coverage.py

背景：run.py 报了「两路都取到数，但有效样本里没有任何 LOF」。
场内日行情只补进 52 个新代码，一个 LOF 段的都没有。

—— v2 相对 v1 的改动（读 akshare 1.18.83 源码之后）——
1) `fund_lof_spot_em` 只返回行情（最新价/涨跌/成交），**没有 IOPV 也没有折价率**。
   所以「换个 host 就能修好」不成立：换通了也只拿回市价，净值仍然缺。
   真正要凑齐的是 [LOF 市价] × [LOF 净值] 两块。
2) 于是把探针改成按这两块分别列候选、逐个实测，最后做一次端到端试算 ——
   直接告诉我们「这条路能算出多少只 LOF 的折溢价、数值合不合理」。
3) 新增两条 v1 没测的候选：
   - 市价：`fund_etf_category_sina('LOF基金')` —— 新浪源，与东财 push2 完全不同的
     供应商，东财不通时它大概率还活着。
   - host：`2.push2.eastmoney.com` —— akshare 自己在 LOF 代码映射里就用这个。

—— 四个小节 ——
  A. 现有两帧的实际构成（含「类型」列取值），说明 LOF 到底缺在哪一步
  B. LOF 市价的候选源，逐个实测
  C. LOF 净值的候选源，逐个实测
  D. 端到端试算：市价 × 净值 → 折溢价，看数值分布是否合理

输出会比较长，整段贴回来即可。
"""
import sys
import time
from collections import Counter

# v5.6：这里原来是一处**更弱的**同类修复 —— `errors="replace"` 会把 ✓ 和 ✗
# 一起降级成 `?`（纪律 5 明令不许），而且它无条件改流、连控制台一起改，
# 还只管 stdout 不管 stderr。统一换成 diag_common 里那一份。
from diag_common import apply_stream_fix  # noqa: E402

apply_stream_fix()                    # **必须在任何 print 之前**

LOF_PREFIX = ("160", "161", "162", "163", "164", "165", "166", "167", "168",
              "501", "502", "505", "506", "150")

# fund_lof_spot_em 的板块码与字段，原样照抄 akshare
LOF_PARAMS = {
    "pn": "1", "pz": "100", "po": "1", "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2", "invt": "2", "wbp2u": "|0|0|0|web", "fid": "f3",
    "fs": "b:MK0404,b:MK0405,b:MK0406,b:MK0407",
    "fields": "f2,f12,f14",
}
# push2delay 是 ETF 那路正在用的；2.push2 是 akshare 取 LOF 代码映射时用的
HOSTS = ["push2delay.eastmoney.com", "2.push2.eastmoney.com",
         "push2.eastmoney.com", "1.push2.eastmoney.com",
         "88.push2.eastmoney.com"]

SUMMARY = {}          # 末尾汇总，方便一眼看清


# ---------------------------------------------------------------- 小工具
def _sec(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _norm_code(v):
    """统一成 6 位数字代码；非法返回空串。"""
    s = str(v).strip().split(".")[0]
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6) if 0 < len(s) <= 6 else ""


def _to_f(v):
    """'3.10%' / '1,234' / '---' / '' / None → float 或 None。"""
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace(",", "")
    if s in ("", "-", "--", "---", "nan", "NaN", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_lof(code):
    return code.startswith(LOF_PREFIX)


def _hist(codes, top=12):
    return Counter(c[:3] for c in codes if len(c) >= 3).most_common(top)


def _ratio(vals):
    n = len(vals)
    g = sum(1 for v in vals if v is not None)
    return g, n, (g / n * 100 if n else 0.0)


def _quantiles(xs):
    """返回 (p5, 中位数, p95)；空表返回三个 None。"""
    if not xs:
        return None, None, None
    s = sorted(xs)

    def q(p):
        return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]
    return q(0.05), q(0.50), q(0.95)


def _col(df, *keywords):
    for kw in keywords:
        for c in df.columns:
            if kw in str(c):
                return c
    return None


# ---------------------------------------------------------------- A. 现有两帧
def probe_frames():
    import akshare as ak
    _sec("A. 现有两帧的实际构成 —— LOF 缺在哪一步")

    spot = daily = None
    try:
        t = time.time()
        spot = ak.fund_etf_spot_em()
        print(f"  fund_etf_spot_em       {len(spot)} 行  ({time.time() - t:.1f}s)")
    except Exception as e:
        print(f"  ✗ fund_etf_spot_em: {type(e).__name__}: {str(e)[:100]}")
    try:
        t = time.time()
        daily = ak.fund_etf_fund_daily_em()
        print(f"  fund_etf_fund_daily_em {len(daily)} 行  ({time.time() - t:.1f}s)")
    except Exception as e:
        print(f"  ✗ fund_etf_fund_daily_em: {type(e).__name__}: {str(e)[:100]}")

    spot_codes = set()
    if spot is not None:
        cc = _col(spot, "代码")
        spot_codes = {c for c in (_norm_code(v) for v in spot[cc]) if c}
        print(f"\n  [ETF帧] {len(spot_codes)} 个代码")
        print(f"    前缀分布: {_hist(list(spot_codes))}")
        print(f"    LOF 段: {sum(1 for c in spot_codes if _is_lof(c))} 个")

    if daily is None:
        SUMMARY["日行情帧"] = "取数失败"
        return
    cc = _col(daily, "基金代码", "代码")
    daily_codes = [c for c in (_norm_code(v) for v in daily[cc]) if c]
    dset = set(daily_codes)
    lof_rows = [c for c in daily_codes if _is_lof(c)]
    print(f"\n  [日行情帧] {len(dset)} 个代码")
    print(f"    前缀分布: {_hist(daily_codes)}")
    print(f"    LOF 段: {len(lof_rows)} 个   ← 折价侧的关键")

    # 「类型」列取值：一眼看出这个接口到底覆盖了哪几类场内基金
    ct = _col(daily, "类型")
    if ct is not None:
        print(f"    「{ct}」列取值: "
              f"{Counter(str(v).strip() for v in daily[ct]).most_common(15)}")

    only = dset - spot_codes
    print(f"\n  仅存在于日行情帧的代码: {len(only)} 个")
    print(f"    前缀分布: {_hist(list(only))}")
    print(f"    其中 LOF 段: {sum(1 for c in only if _is_lof(c))} 个")

    print("\n  日行情帧关键列可解析比例：")
    cols = [c for c in daily.columns if str(c) in ("市价", "折价率")]
    cols += [c for c in daily.columns if "单位净值" in str(c)][:2]
    for c in cols:
        g, n, p = _ratio([_to_f(v) for v in daily[c]])
        print(f"    {str(c):<22} {g}/{n} ({p:.1f}%)")

    if lof_rows:
        mask = [_is_lof(_norm_code(v)) for v in daily[cc]]
        sub = daily[mask]
        print("\n  日行情帧里的 LOF 样本（前 5 行）：")
        show = [c for c in ("基金代码", "基金简称", "类型", "市价", "折价率")
                if c in sub.columns]
        for _, r in sub.head(5).iterrows():
            print("    " + " | ".join(f"{c}={r[c]}" for c in show))
        for c in ("市价", "折价率"):
            if c in sub.columns:
                g, n, p = _ratio([_to_f(v) for v in sub[c]])
                print(f"    LOF 行的 {c}: {g}/{n} 可解析 ({p:.1f}%)")
        SUMMARY["日行情帧LOF"] = f"{len(lof_rows)} 行"
    else:
        print("\n  ⚠ 日行情帧里一条 LOF 段代码都没有 —— 这个接口根本不覆盖 LOF，")
        print("    不是「净值列为空」。折价侧要另找市价+净值两个源。")
        SUMMARY["日行情帧LOF"] = "0（接口不覆盖）"


# ---------------------------------------------------------------- B. 市价候选
def _price_from_clist(host):
    import requests
    url = f"https://{host}/api/qt/clist/get"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    r = requests.get(url, params=LOF_PARAMS, headers=headers, timeout=15)
    j = r.json() or {}
    data = j.get("data") or {}
    diff = data.get("diff") or []
    total = data.get("total")
    out = {}
    for d in diff:
        code = _norm_code(d.get("f12"))
        px = _to_f(d.get("f2"))
        if code and px:
            out[code] = (str(d.get("f14") or ""), px)
    return out, total


def probe_price():
    """返回 (最佳源标签, {code: (name, price)})。"""
    import akshare as ak
    _sec("B. LOF 市价的候选源")
    best = (None, {})

    # B1 新浪：完全不同的供应商，东财不通时最有希望
    fn = getattr(ak, "fund_etf_category_sina", None)
    if fn is None:
        print("  ✗ fund_etf_category_sina 不存在（akshare 太旧）")
    else:
        t = time.time()
        try:
            df = fn(symbol="LOF基金")
            cc, cn, cp = _col(df, "代码"), _col(df, "名称"), _col(df, "最新价")
            got = {}
            for _, r in df.iterrows():
                code, px = _norm_code(r.get(cc)), _to_f(r.get(cp))
                if code and px and px > 0:
                    got[code] = (str(r.get(cn) or ""), px)
            lof_n = sum(1 for c in got if _is_lof(c))
            print(f"  ✓ 新浪 fund_etf_category_sina('LOF基金'): {len(df)} 行，"
                  f"可用市价 {len(got)} 个，其中 LOF 段 {lof_n} 个"
                  f"  ({time.time() - t:.1f}s)")
            print(f"      前缀分布: {_hist(list(got))}")
            print(f"      样例: {list(got.items())[:3]}")
            SUMMARY["市价-新浪"] = f"{len(got)} 个（LOF段 {lof_n}）"
            if len(got) > len(best[1]):
                best = ("新浪LOF列表", got)
        except Exception as e:
            print(f"  ✗ 新浪 fund_etf_category_sina: {type(e).__name__}: {str(e)[:110]}"
                  f"  ({time.time() - t:.1f}s)")
            SUMMARY["市价-新浪"] = f"失败 {type(e).__name__}"

    # B2 akshare 原生（88.push2），确认是不是真的不通
    fn = getattr(ak, "fund_lof_spot_em", None)
    if fn is not None:
        t = time.time()
        try:
            df = fn()
            cc, cn, cp = _col(df, "代码"), _col(df, "名称"), _col(df, "最新价")
            got = {}
            for _, r in df.iterrows():
                code, px = _norm_code(r.get(cc)), _to_f(r.get(cp))
                if code and px and px > 0:
                    got[code] = (str(r.get(cn) or ""), px)
            print(f"  ✓ akshare fund_lof_spot_em: {len(df)} 行，可用市价 {len(got)} 个"
                  f"  ({time.time() - t:.1f}s)")
            SUMMARY["市价-原生88.push2"] = f"{len(got)} 个"
            if len(got) > len(best[1]):
                best = ("fund_lof_spot_em", got)
        except Exception as e:
            print(f"  ✗ akshare fund_lof_spot_em: {type(e).__name__}: {str(e)[:110]}"
                  f"  ({time.time() - t:.1f}s)")
            SUMMARY["市价-原生88.push2"] = f"失败 {type(e).__name__}"

    # B3 东财 clist 换 host（只取第一页，够判断通不通）
    print("\n  东财 clist 换 host（LOF 板块码，取第 1 页）：")
    for host in HOSTS:
        t = time.time()
        try:
            got, total = _price_from_clist(host)
            if got:
                print(f"    ✓ {host:<28} total={total}  本页 {len(got)} 条  "
                      f"({time.time() - t:.1f}s)  样例: {list(got.items())[:2]}")
                SUMMARY[f"市价-{host}"] = f"total={total}"
                if not best[1]:
                    best = (f"clist@{host}", got)   # 只有一页，仅作兜底
            else:
                print(f"    ✗ {host:<28} 返回无数据 ({time.time() - t:.1f}s)")
                SUMMARY[f"市价-{host}"] = "无数据"
        except Exception as e:
            print(f"    ✗ {host:<28} {type(e).__name__}: {str(e)[:70]}  "
                  f"({time.time() - t:.1f}s)")
            SUMMARY[f"市价-{host}"] = f"失败 {type(e).__name__}"

    print(f"\n  → 市价最佳源: {best[0] or '无'}（{len(best[1])} 个代码）")
    return best


# ---------------------------------------------------------------- C. 净值候选
def probe_nav():
    """返回 [(标签, {code: {净值列名: 值}}, 附加信息, 净值列名表), ...]。"""
    import akshare as ak
    _sec("C. LOF 净值的候选源")
    out = []

    specs = (
        ("同花顺 fund_etf_category_ths('LOF')",
         lambda: ak.fund_etf_category_ths(symbol="LOF")),
        ("东财 fund_value_estimation_em('LOF')",
         lambda: ak.fund_value_estimation_em(symbol="LOF")),
    )
    for label, call in specs:
        t = time.time()
        try:
            df = call()
        except Exception as e:
            print(f"  ✗ {label}: {type(e).__name__}: {str(e)[:110]}")
            SUMMARY[f"净值-{label[:3]}"] = f"失败 {type(e).__name__}"
            continue
        cc = _col(df, "基金代码", "代码")
        nav_cols = [c for c in df.columns
                    if "单位净值" in str(c) and "累计" not in str(c)]
        if cc is None or not nav_cols:
            print(f"  ✗ {label}: 找不到代码列或净值列（列：{list(df.columns)[:10]}）")
            SUMMARY[f"净值-{label[:3]}"] = "缺列"
            continue
        table, extra = {}, {}
        for _, r in df.iterrows():
            code = _norm_code(r.get(cc))
            if not code:
                continue
            table[code] = {c: _to_f(r.get(c)) for c in nav_cols}
            for k in ("赎回状态", "申购状态", "基金类型", "最新-交易日", "估算日期"):
                if k in df.columns:
                    extra.setdefault(code, {})[k] = str(r.get(k))
        lof_n = sum(1 for c in table if _is_lof(c))
        print(f"  ✓ {label}: {len(df)} 行 / {len(table)} 个代码，其中 LOF 段 {lof_n} 个"
              f"  ({time.time() - t:.1f}s)")
        print(f"      净值列: {nav_cols}")
        for c in nav_cols:
            g, n, p = _ratio([v[c] for v in table.values()])
            print(f"        {str(c):<26} 可解析 {g}/{n} ({p:.1f}%)")
        if extra:
            k0 = next(iter(extra))
            print(f"      附加字段样例: {k0} → {extra[k0]}")
        for k in ("赎回状态", "申购状态"):
            if k in df.columns:
                print(f"      「{k}」取值: "
                      f"{Counter(str(v).strip() for v in df[k]).most_common(6)}")
        SUMMARY[f"净值-{label[:3]}"] = f"{lof_n} 个 LOF"
        out.append((label, table, extra, nav_cols))
    return out


# ---------------------------------------------------------------- D. 端到端试算
def dry_run(price_label, prices, nav_sources):
    _sec("D. 端到端试算：市价 × 净值 → 折溢价")
    if not prices:
        print("  市价一个都没取到，无法试算。")
        return
    if not nav_sources:
        print("  净值一个都没取到，无法试算。")
        return

    lof_prices = {c: v for c, v in prices.items() if _is_lof(c)}
    print(f"  市价源「{price_label}」的 LOF 段: {len(lof_prices)} 个\n")

    for label, table, extra, nav_cols in nav_sources:
        print(f"  —— 净值源：{label} ——")
        for navcol in nav_cols:
            pairs = []
            for code, (name, px) in lof_prices.items():
                nav = (table.get(code) or {}).get(navcol)
                if nav and nav > 0:
                    pairs.append((code, name, (px / nav - 1) * 100))
            if not pairs:
                print(f"    {str(navcol):<26} 交集 0 个")
                continue
            vals = [p for _, _, p in pairs]
            p5, med, p95 = _quantiles(vals)
            n_disc = sum(1 for p in vals if p <= -2.0)
            n_prem = sum(1 for p in vals if p >= 3.0)
            flag = "" if abs(med) < 3 else "   ⚠ 中位数离 0 太远，净值口径可能不对"
            print(f"    {str(navcol):<26} 交集 {len(pairs):>4} 个  "
                  f"中位数 {med:+.2f}%  p5 {p5:+.2f}%  p95 {p95:+.2f}%  "
                  f"折价≥2% {n_disc} 条  溢价≥3% {n_prem} 条{flag}")
            if n_disc:
                top = sorted((p for p in pairs if p[2] <= -2.0),
                             key=lambda x: x[2])[:6]
                print("        折价前 6:  " + " | ".join(
                    f"{c} {n[:8]} {p:+.2f}%" for c, n, p in top))
                c0 = top[0][0]
                if c0 in extra:
                    print(f"        首条附加信息: {extra[c0]}")
        print()


# ---------------------------------------------------------------- main
def main():
    try:
        import akshare as ak
    except ImportError:
        print("未安装 akshare")
        return 2
    print(f"akshare {getattr(ak, '__version__', '?')} | python {sys.version.split()[0]}")

    try:
        probe_frames()
    except Exception as e:
        print(f"\n  ⚠ 小节 A 异常中断：{type(e).__name__}: {e}")

    price_label, prices = None, {}
    try:
        price_label, prices = probe_price()
    except Exception as e:
        print(f"\n  ⚠ 小节 B 异常中断：{type(e).__name__}: {e}")

    nav_sources = []
    try:
        nav_sources = probe_nav()
    except Exception as e:
        print(f"\n  ⚠ 小节 C 异常中断：{type(e).__name__}: {e}")

    try:
        dry_run(price_label, prices, nav_sources)
    except Exception as e:
        print(f"\n  ⚠ 小节 D 异常中断：{type(e).__name__}: {e}")

    _sec("汇总")
    for k, v in SUMMARY.items():
        print(f"  {k:<32} {v}")
    print("\n  把以上完整输出贴回来即可，我按 D 段的交集数和中位数决定怎么改主流程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())