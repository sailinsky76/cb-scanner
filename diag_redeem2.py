"""阶段 2 的**第二次**探针：第一次探针答完了「日期拿不拿得到」，
这一次只答「拿到的这一列，能不能分辨强赎和到期」。

**同样不产出机会条目、不接进 `scanner/`**（纪律 1）。它只回答问题。

──────────────────────────────────────────────────────────────────────
为什么还要再探一次

`diag_redeem.py` 退出码 0，结论是「最后交易日直接从表字段拿到了」。
但对着 docs/probes/probe.txt 逐行看，那个结论跳过了一件会**直接害到人**的事：

    嘉泽  最后交易日 08-18  到期日 2026-08-24
    文科  最后交易日 08-14  到期日 2026-08-20
    青农  最后交易日 08-19  到期日 2026-08-25
    万孚  最后交易日 08-26  到期日 2026-09-01
    宝莱  最后交易日 08-31  到期日 2026-09-04

最后交易日紧贴到期日（差 4-6 天），这五只 2020 年 8/9 月发行、6 年期
——**是自然到期摘牌，不是强赎**。而春23（到期 2029）、正帆（2031）、
应流（2031）离到期还有几年，那才是强赎形态。

分错的代价不是少赚：
  · 强赎 → 动作词是「限期离场，否则拿赎回价」
  · 到期 → 动作词是「拿本息」
方向完全相反，会当场撞在不变量 ④（同一条里不出现互相矛盾的指令）上。

唯一可能把两者分开的字段是 `强赎状态` / `强赎条款`，而第一次探针
**只印了列名，一个取值都没打**（18 列的列名还被 `…` 截断了 2 个）。

──────────────────────────────────────────────────────────────────────
五个问题，一个都不预设答案

  Q1  bond_cb_redeem_jsl 的 18 列**全名**是什么？各列非空多少行？
  Q2  `强赎状态` / `强赎条款` 的**全部取值**是什么？
      这些取值能不能把「近到期组」和「远期组」分开？←—— 退出码就看这一条
  Q3  对照名单**按代码**重匹配一遍。齐翔2 第一次没对上，
      是因为按名称模糊匹配，表里大概率叫「齐翔转2」。
  Q4  这张表 320 行是什么口径？「不在表里」等于「没在赎回」还是「看不到」？
      最后交易日 空/非空各多少，非空里过去/未来各多少？
  Q5  `强赎天计数` 的**原始 repr** 是什么？
      宙邦那一行第一次印成 `0001-12-15` —— 倒计时字段被日期解析器啃了。

──────────────────────────────────────────────────────────────────────
用法

    python diag_redeem2.py                 # 完整探测（联网，约 1 分钟）
    python diag_redeem2.py --rows 20       # 多印几行原始数据供肉眼核对
    python diag_redeem2.py --names 春23,嘉泽 # 换一份对照名单
    python diag_redeem2.py --selftest      # 纯函数离线自测，不联网，秒回

退出码（**机械判定，判据写死在 decide() 里，可证伪**）：
    0 = 状态字段能把强赎和到期分开 → v5.1 报告里可以出现「强赎」二字
    1 = 分不开（字段缺失/覆盖率不够/取值不互斥/样本不足）
        → v5.1 只印「最后交易日」，不下强赎判断
    2 = 连表都拿不到 → 这是取数问题，先跑 diag_sources.py
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from functools import partial

sys.path.insert(0, ".")

from scanner.utils import parse_date, retry_call  # noqa: E402
from diag_common import apply_stream_fix  # noqa: E402

# ---------------------------------------------------------------------------
# 探测目标。只碰这两张表 —— 第一次探针已经证明其余 9 个接口带不来新东西。
_TABLE = "bond_cb_redeem_jsl"       # 集思录·强赎汇总，320 行 × 18 列
_XREF = "bond_zh_cov"               # 东财转债总表，1047 行，用来拿「代码」做交叉匹配

# 对照名单：08-09 那份日报的「已公告赎回」9 只 + 「强赎倒计时」1 只。
# 和第一次探针同一份，这样两次输出可以直接对着看。
_BENCH = ["春23", "嘉泽", "宝莱", "文科", "正帆",
          "齐翔2", "万孚", "青农", "应流", "宙邦"]

_LAST_TRADE = "最后交易日"
_MATURITY = "到期日"
_COUNTDOWN = "强赎天计数"
_STATUS_HINTS = ("强赎状态", "强赎条款", "状态", "条款")
_CODE_COLS = ("代码", "债券代码", "转债代码")
_NAME_COLS = ("名称", "债券简称", "转债名称", "简称")

# 分组阈值。**这两个数只用来分组做证据，不进实现、不印进报告。**
# 4-6 天是这批自然到期样本的实测间隔，取 10 天留余量；
# 一年以上必然不是「最后交易日≈到期日」的到期摘牌形态。
# 中间那段（11-365 天）刻意不判 —— 判不了就说判不了，别猜。
_NEAR_MAX = 10
_FAR_MIN = 365

# 判定门槛：状态列在「最后交易日非空」的行上至少覆盖这么多，才算这列有用。
_MIN_COVERAGE = 0.80
_MIN_GROUP = 2                      # 每组至少这么多行，才够下判断


# ═══════════════════════ 纯函数（不联网，--selftest 覆盖）═══════════════════════

def split_frag(frag: str) -> tuple:
    """把简称片段拆成「前缀 + 数字后缀」。「齐翔2」→ ('齐翔', '2')。

    日报里写的是简称片段而不是完整债券简称，而完整简称里往往夹了一个
    「转」字：齐翔2 → 齐翔转2。直配不上时靠这个拆分做宽配。
    """
    s = str(frag or "").strip()
    m = re.match(r"^(.*?)(\d+)$", s)
    if m and m.group(1):
        return m.group(1), m.group(2)
    return s, ""


def loose_variants(frag: str) -> list:
    """生成一组候选写法，从窄到宽。宽配只在直配 0 命中时才用。"""
    s = str(frag or "").strip()
    if not s:
        return []
    prefix, digits = split_frag(s)
    out = [s]
    if digits:
        out += [f"{prefix}转{digits}", f"{prefix}转债{digits}"]
    out += [f"{prefix}转债", prefix]
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def name_matches(frag: str, name: str) -> bool:
    """宽配判据：前缀命中，且数字后缀（若有）也在名字里。

    「齐翔2」对「齐翔转2」→ True（前缀「齐翔」在，数字「2」在）。
    「齐翔2」对「齐翔转债」→ False（没有 2，可能是另一只）。
    宽配必然会多捞 —— 所以脚本把**所有**候选都印出来让你核对，不替你选。
    """
    n = str(name or "")
    prefix, digits = split_frag(frag)
    if not prefix or prefix not in n:
        return False
    if not digits:
        return True
    return digits in n[n.index(prefix) + len(prefix):]


def norm_code(v) -> str:
    """代码归一：去空白、去交易所后缀，取末尾 6 位数字。

    集思录可能给 `113065`，东财可能给 `113065.SH` 或带空格。
    按代码匹配才是对的 —— 名称匹配已经在齐翔2 上翻过一次车。
    """
    s = re.sub(r"[^0-9]", "", str(v or ""))
    return s[-6:] if len(s) >= 6 else s


def gap_days(last_trade, maturity):
    """到期日 − 最后交易日，单位自然日。任一缺失返回 None。"""
    a, b = parse_date(last_trade), parse_date(maturity)
    if a is None or b is None:
        return None
    return (b - a).days


def gap_bucket(g) -> str:
    """把间隔分桶。**桶名带「疑」字是刻意的** —— 这是证据分组，不是结论。"""
    if g is None:
        return "缺日期（分不了组）"
    if g < 0:
        return "负数（到期日早于最后交易日，数据有问题）"
    if g <= _NEAR_MAX:
        return f"≤{_NEAR_MAX}天（疑自然到期摘牌）"
    if g >= _FAR_MIN:
        return f"≥{_FAR_MIN}天（疑强赎）"
    return f"{_NEAR_MAX + 1}-{_FAR_MIN - 1}天（判不了，不猜）"


def is_bogus_date(d) -> bool:
    """年份离谱 = 解析器把不是日期的东西啃成了日期。"""
    return d is not None and not (1900 <= d.year <= 2100)


def looks_forced(v: str) -> bool:
    """取值的字面意思是不是「在强赎」。用于**矛盾检测**，不用于下结论。

    「不提前赎回」「未满足」这类反向/否定写法必须先排掉 ——
    和 diag_redeem.py 里 classify_title() 同一个坑：
    「不提前赎回」是「提前赎回」的子串，含义完全相反。
    """
    s = str(v or "")
    if any(neg in s for neg in ("不", "未", "否", "到期")):
        return False
    return "强赎" in s or "赎回" in s


def decide(table_ok: bool, evidence: list) -> tuple:
    """机械判定退出码。**判据全在这里，改判据必须改这一个函数。**

    evidence: [{"col":列名, "coverage":float, "n_unique":int,
                "near":{值:计数}, "far":{值:计数}}, …]

    返回 (退出码, [理由行…])。
    """
    if not table_ok:
        return 2, [f"连 {_TABLE} 都没拿到 —— 这是取数问题，不是策略问题。"]
    if not evidence:
        return 1, ["表里没有任何候选状态列（强赎状态/强赎条款）。",
                   "→ v5.1 只印「最后交易日」，一个字的强赎判断都不下。"]

    why = []
    for e in evidence:
        col, cov, nu = e["col"], e["coverage"], e["n_unique"]
        near, far = e["near"], e["far"]
        n_near, n_far = sum(near.values()), sum(far.values())

        if cov < _MIN_COVERAGE:
            why.append(f"「{col}」：在最后交易日非空的行上只覆盖 {cov:.0%}"
                       f"（要求 ≥{_MIN_COVERAGE:.0%}）—— 覆盖不够就是「没取到」，"
                       f"按纪律 5 不能当成「没强赎」。")
            continue
        if nu < 2:
            why.append(f"「{col}」：全表只有 {nu} 个取值，这一列分不了任何东西。")
            continue
        if n_near < _MIN_GROUP or n_far < _MIN_GROUP:
            why.append(f"「{col}」：近到期组 {n_near} 行 / 远期组 {n_far} 行，"
                       f"至少一组不足 {_MIN_GROUP} 行 —— **样本不足，判不了**。"
                       f"这不等于这列没用，换个日子再跑一次。")
            continue

        only_near = [v for v in near if v not in far]
        only_far = [v for v in far if v not in near]
        if only_near and only_far:
            # 互斥还不够。字面意思必须和分组方向对得上 —— 否则就是两个证据打架，
            # 而两个证据打架时机械放行，等于把一个方向反了的映射写进报告。
            backwards = [v for v in only_near if looks_forced(v)]
            if backwards:
                why.append(
                    f"「{col}」：取值互斥，但**方向反了** —— {backwards} 字面意思是"
                    f"「在强赎」，却只出现在近到期组（最后交易日紧贴到期日 ≤{_NEAR_MAX} 天）。")
                why.append(
                    "  这说明状态字段和间隔证据互相矛盾，至少有一个不能信。"
                    "两个证据打架时不下结论 —— 这正是不变量 ④ 要拦的东西。")
                continue
            return 0, [
                f"「{col}」能把两组分开，且字面方向对得上：",
                f"  只出现在近到期组（疑到期摘牌）的取值：{', '.join(map(str, only_near))}",
                f"  只出现在远期组（疑强赎）的取值：{', '.join(map(str, only_far))}",
                "→ v5.1 可以按这一列下强赎/到期判断，报告里可以出现「强赎」二字。",
                "  **实现时按取值配，不按间隔天数配** —— 上面那两个阈值只是本探针的分组工具。",
                "  照抄之前先看一眼上面 Q2 的交叉表，映射方向以那张表为准。",
            ]
        why.append(f"「{col}」：取值在两组之间**没有互斥项**"
                   f"（近到期组 {sorted(map(str, near))}，远期组 {sorted(map(str, far))}）"
                   f"—— 有这列，但它分不开这两件事。")

    why.append("")
    why.append("→ v5.1 只印「最后交易日」，措辞一律用这五个字，不下强赎判断。")
    why.append("  三档照旧：窗口内 / 已过最后交易日 / 在名单里但日期未取到。")
    return 1, why


# ═══════════════════════════ 联网部分 ═══════════════════════════

def _hr(title: str) -> None:
    print(f"\n{'─' * 68}\n▎{title}")


def _fetch(ak, name: str):
    """取一张表。取不到就如实说，不返回空表冒充成功。"""
    fn = getattr(ak, name, None)
    if fn is None:
        print(f"  ✗ 本机 akshare 没有 {name}")
        return None
    t0 = time.time()
    df, err = retry_call(fn, label=name, attempts=2, backoff=(2.0,),
                         reject_empty=False)
    sec = time.time() - t0
    if df is None:
        print(f"  ✗ {name}：调用失败（{sec:.1f}s）{err}")
        return None
    print(f"  ✓ {name}：{len(df)} 行 × {len(df.columns)} 列（{sec:.1f}s）")
    return df


def _pick(df, candidates):
    """按候选顺序挑一个存在的列名。"""
    cols = [str(c) for c in df.columns]
    return next((c for c in candidates if c in cols), None)


def probe_columns(df, rows: int) -> None:
    """Q1：18 列全名（**不截断**）+ 每列非空行数 + 原始样例行。"""
    _hr(f"Q1  {_TABLE} 的全部列名与非空行数")
    print(f"  共 {len(df.columns)} 列，逐列列出（第一次探针把最后 2 列截断成了 …）：\n")
    for i, c in enumerate(df.columns, 1):
        col = df[c]
        nn = int(col.notna().sum())
        samples = [repr(str(v)) for v in col.dropna().head(2)]
        print(f"  {i:>2}. {str(c):<12} 非空 {nn:>4}/{len(df)}　"
              f"dtype={col.dtype}　样例 {', '.join(samples) if samples else '（全空）'}")

    key = [c for c in (_LAST_TRADE, _MATURITY, "代码", "名称", "强赎状态",
                       "强赎条款", _COUNTDOWN, "强赎价") if c in map(str, df.columns)]
    if key:
        print(f"\n  ── 原始前 {rows} 行（只取关键列，**未经任何解析**）──")
        for _, r in df.head(rows).iterrows():
            print("    " + "　".join(f"{c}={str(r.get(c))!r}" for c in key))


def probe_status(df) -> list:
    """Q2：状态/条款字段的全部取值 + 与间隔分组的交叉表。这一节决定退出码。"""
    _hr("Q2  「强赎状态 / 强赎条款」的全部取值，以及它能不能分开强赎与到期")

    cols = [str(c) for c in df.columns]
    cands = [c for c in cols if any(h in c for h in _STATUS_HINTS)]
    if not cands:
        print("  ✗ 表里没有任何名字含「状态 / 条款 / 强赎」的列。")
        return []
    print(f"  候选状态列 {len(cands)} 个：{', '.join(cands)}")

    # 先给每行打上间隔分组
    buckets, gaps = [], []
    for _, r in df.iterrows():
        g = gap_days(r.get(_LAST_TRADE), r.get(_MATURITY))
        gaps.append(g)
        buckets.append(gap_bucket(g))
    near_key = f"≤{_NEAR_MAX}天（疑自然到期摘牌）"
    far_key = f"≥{_FAR_MIN}天（疑强赎）"

    print("\n  ── 间隔分组（到期日 − 最后交易日）──")
    for b in sorted(set(buckets)):
        print(f"    {b:<28} {buckets.count(b):>4} 行")
    print("    ↑ 这个分组**只是本探针用来做证据的工具**，不是实现里的判据。")
    print("      按天数阈值给转债贴「强赎」标签就是造一个判定规则（纪律 2），不做。")

    evidence = []
    lt_nonnull = [i for i in range(len(df))
                  if parse_date(df.iloc[i].get(_LAST_TRADE)) is not None]
    for c in cands:
        col = df[c]
        vals = [("（空）" if _blank(v) else str(v).strip()) for v in col]
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1

        print(f"\n  ── 「{c}」全部取值（{len(counts)} 个，全列出）──")
        for v, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"    {v!r:<28} {n:>4} 行")

        cov = (sum(1 for i in lt_nonnull if vals[i] != "（空）") / len(lt_nonnull)
               if lt_nonnull else 0.0)
        print(f"    在「最后交易日可解析」的 {len(lt_nonnull)} 行上，本列非空 {cov:.0%}")

        near, far = {}, {}
        for i, b in enumerate(buckets):
            if b == near_key:
                near[vals[i]] = near.get(vals[i], 0) + 1
            elif b == far_key:
                far[vals[i]] = far.get(vals[i], 0) + 1
        print(f"    交叉表：近到期组（{sum(near.values())} 行）{near or '—'}")
        print(f"            远期组（{sum(far.values())} 行）{far or '—'}")

        evidence.append({"col": c, "coverage": cov,
                         "n_unique": len([v for v in counts if v != "（空）"]),
                         "near": {k: v for k, v in near.items() if k != "（空）"},
                         "far": {k: v for k, v in far.items() if k != "（空）"}})
    return evidence


def _blank(v) -> bool:
    return v is None or str(v).strip() in ("", "nan", "None", "-", "—", "NaT")


def probe_bench(df, xref, names: list) -> None:
    """Q3：对照名单按**代码**重匹配。第一次探针按名称配，齐翔2 掉了。"""
    _hr(f"Q3  对照名单按代码重匹配（{len(names)} 只）")

    r_name = _pick(df, _NAME_COLS)
    r_code = _pick(df, _CODE_COLS)
    print(f"  {_TABLE}：名称列=「{r_name}」　代码列=「{r_code}」")
    if xref is not None:
        x_name, x_code = _pick(xref, _NAME_COLS), _pick(xref, _CODE_COLS)
        print(f"  {_XREF}：名称列=「{x_name}」　代码列=「{x_code}」")
    else:
        x_name = x_code = None

    r_names = [str(v) for v in df[r_name]] if r_name else []
    r_codes = [norm_code(v) for v in df[r_code]] if r_code else []

    for frag in names:
        print(f"\n  · {frag}")

        # ① 直配：片段是不是某个名字的子串
        direct = [n for n in r_names if frag in n]
        # ② 宽配：前缀 + 数字后缀（只在直配落空时看）
        loose = [n for n in r_names if name_matches(frag, n)] if not direct else []
        hits = direct or loose
        how = "直配" if direct else ("宽配" if loose else "未命中")
        print(f"    {_TABLE} 按名称：{how} → {hits if hits else '（无）'}")
        if not direct and loose:
            print(f"      宽配候选写法：{loose_variants(frag)}")
            print("      ↑ 宽配会多捞，**这里不替你选**，看名字自己核对。")

        # ③ 交叉表拿代码，再用代码回配 —— 这是本轮真正要验的那条路
        if xref is not None and x_name and x_code:
            xn = [str(v) for v in xref[x_name]]
            xdirect = [i for i, n in enumerate(xn) if frag in n]
            xloose = ([i for i, n in enumerate(xn) if name_matches(frag, n)]
                      if not xdirect else [])
            idxs = xdirect or xloose
            if not idxs:
                print(f"    {_XREF} 也没配上 —— 这只可能已经摘牌退出总表了。")
                continue
            for i in idxs[:3]:
                code = norm_code(xref.iloc[i][x_code])
                nm = xn[i]
                back = [j for j, c in enumerate(r_codes) if c and c == code]
                if back:
                    row = df.iloc[back[0]]
                    lt = parse_date(row.get(_LAST_TRADE))
                    mt = parse_date(row.get(_MATURITY))
                    g = gap_days(row.get(_LAST_TRADE), row.get(_MATURITY))
                    print(f"    ✓ 按代码 {code}（{nm}）回配到 {_TABLE}："
                          f"表内名称={str(row.get(r_name))!r}")
                    print(f"      最后交易日={lt}　到期日={mt}　"
                          f"间隔={g if g is not None else '—'} 天 → {gap_bucket(g)}")
                    for c in (x for x in map(str, df.columns)
                              if any(h in x for h in _STATUS_HINTS)):
                        print(f"      {c}={str(row.get(c))!r}")
                else:
                    print(f"    ✗ 代码 {code}（{nm}）在 {_TABLE} 里没有对应行"
                          f" —— 说明这张表**不收**它，Q4 会说这意味着什么。")


def probe_scope(df, xref) -> None:
    """Q4：320 行是什么口径。「不在表里」= 没在赎回，还是看不到？"""
    _hr(f"Q4  {_TABLE} 的收录口径：{len(df)} 行到底是哪 {len(df)} 只")

    total = len(df)
    parsed = [parse_date(v) for v in df[_LAST_TRADE]] if _LAST_TRADE in df else []
    if not parsed:
        print(f"  ✗ 表里没有「{_LAST_TRADE}」列。")
        return

    import datetime as _dt
    today = _dt.date.today()
    empty = sum(1 for v, d in zip(df[_LAST_TRADE], parsed) if _blank(v))
    unparsed = sum(1 for v, d in zip(df[_LAST_TRADE], parsed)
                   if not _blank(v) and d is None)
    ok = [d for d in parsed if d is not None]
    past = sum(1 for d in ok if d < today)
    same = sum(1 for d in ok if d == today)
    future = sum(1 for d in ok if d > today)

    print(f"  今天 {today}")
    print(f"  「{_LAST_TRADE}」：空 {empty} 行　有值但解析不了 {unparsed} 行　"
          f"可解析 {len(ok)} 行（合计 {total}）")
    print(f"    可解析里：已过 {past}　今天 {same}　未来 {future}")
    if unparsed:
        print("    ⚠ 有值却解析不了 —— 这批必须单独看，别当成空值静默丢掉（纪律 5）。")

    if future:
        print("\n  ── 未来的最后交易日，按剩余自然日分布 ──")
        for lo, hi, lab in ((0, 3, "0-3 天"), (4, 7, "4-7 天"),
                            (8, 14, "8-14 天"), (15, 30, "15-30 天"),
                            (31, 10 ** 6, "30 天以上")):
            n = sum(1 for d in ok if d > today and lo <= (d - today).days <= hi)
            if n:
                print(f"    {lab:<10} {n:>4} 只")

    # 320 vs 1047：这张表是不是总表的子集，还是两边各有各的
    if xref is not None:
        r_code, x_code = _pick(df, _CODE_COLS), _pick(xref, _CODE_COLS)
        if r_code and x_code:
            rc = {norm_code(v) for v in df[r_code] if norm_code(v)}
            xc = {norm_code(v) for v in xref[x_code] if norm_code(v)}
            print(f"\n  ── 与 {_XREF}（{len(xref)} 行）按代码求交 ──")
            print(f"    本表 {len(rc)} 个代码，其中 {len(rc & xc)} 个在总表里，"
                  f"{len(rc - xc)} 个不在（总表已摘牌？）")
            print(f"    总表 {len(xc)} 个代码，其中 {len(xc - rc)} 个不在本表里")
            print("    ↑ 最后这个数就是「看不到」的规模。**它不等于「没在赎回」**（纪律 5）。")

    print(f"\n  ── 结论要你自己下的那一问 ──")
    print(f"  这张表 {total} 行。空的 {empty} 行是什么形态，")
    print(f"  决定了「不在表里」的含义：")
    print(f"    · 若空行也带状态值（如「未满足」）→ 表收的是**全部存续债的强赎跟踪**，")
    print(f"      「不在表里」= 这只债看不到，不能当成「没在赎回」。")
    print(f"    · 若空行没有任何状态 → 表可能只收**已触发/已公告**的，")
    print(f"      那 v5.1 的第三档「在名单里但日期未取到」就必须留着。")
    print(f"  Q2 那张交叉表已经把证据摆出来了，照着看一眼即可。")


def probe_countdown(df) -> None:
    """Q5：强赎天计数的原始 repr —— 宙邦那行被解析成 0001-12-15 的坑。"""
    _hr(f"Q5  「{_COUNTDOWN}」的原始值，以及它为什么会变成日期")

    if _COUNTDOWN not in map(str, df.columns):
        print(f"  ✗ 表里没有「{_COUNTDOWN}」列。")
        return

    vals = [v for v in df[_COUNTDOWN] if not _blank(v)]
    uniq = list(dict.fromkeys(str(v) for v in vals))
    print(f"  非空 {len(vals)} 行，去重后 {len(uniq)} 种写法。前 20 种（原始 repr）：\n")
    for v in uniq[:20]:
        d = parse_date(v)
        flag = ""
        if is_bogus_date(d):
            flag = f"　⚠ parse_date 把它啃成了 {d} —— 年份离谱，这不是日期"
        elif d is not None:
            flag = f"　⚠ parse_date 解析出了 {d} —— 这一列不该被解析成日期"
        print(f"    {v!r:<20}{flag}")

    bogus = [v for v in uniq if is_bogus_date(parse_date(v))]
    print(f"\n  被误解析成日期的写法：{len(bogus)} 种 {bogus[:5]}")
    print("  成因（沙箱里已复现）：parse_date 先按空格截断，"
          f"'12/15 | 30' → '12/15'，")
    print("  再交给 pandas 兜底，被读成「12 月 15 日」，年份缺省成 0001。")
    print("  只有首个数字 ≤12 时才会中招（'20/15 | 30' 返回 None）——"
          "**恰好是倒计时刚开始的那批**。")
    print("  → v5.1 的处理：这一列当**不透明字符串**原样透传，一次 parse_date 都不调。")


# ═══════════════════════════ 离线自测 ═══════════════════════════

def selftest() -> int:
    """纯函数离线自测。联网前先跑这个，语法/逻辑错不用等到联网才发现。"""
    n = [0]

    def ok(msg):
        n[0] += 1
        print(f"  [PASS] {msg}")

    assert split_frag("齐翔2") == ("齐翔", "2"), split_frag("齐翔2")
    assert split_frag("春23") == ("春", "23")
    assert split_frag("嘉泽") == ("嘉泽", "")
    ok("片段拆分：齐翔2 → 前缀「齐翔」+ 后缀「2」")

    assert name_matches("齐翔2", "齐翔转2")
    assert not name_matches("齐翔2", "齐翔转债")      # 没有 2，可能是另一只
    assert name_matches("春23", "春23转债")
    assert not name_matches("嘉泽", "青农转债")
    ok("宽配判据：齐翔2 配上齐翔转2，但不会误配齐翔转债")

    assert "齐翔转2" in loose_variants("齐翔2")
    assert loose_variants("齐翔2")[0] == "齐翔2"      # 窄的排前面
    ok("候选写法从窄到宽，原始片段排第一")

    assert norm_code("113065.SH") == "113065"
    assert norm_code(" 113065 ") == "113065"
    assert norm_code(113065) == "113065"
    ok("代码归一：去后缀去空白，取末尾 6 位")

    assert gap_days("2026-08-18", "2026-08-24") == 6
    assert gap_days("2026-08-07", "2031-03-18") == 1684
    assert gap_days(None, "2026-08-24") is None
    assert "疑自然到期摘牌" in gap_bucket(6)
    assert "疑强赎" in gap_bucket(1684)
    assert "判不了" in gap_bucket(100)
    ok("间隔分桶：6 天入近到期组、1684 天入远期组、100 天明确判不了")

    # 复现 0001-12-15：这条是本轮的核心取证，钉死它
    assert is_bogus_date(parse_date("12/15 | 30")), parse_date("12/15 | 30")
    assert parse_date("20/15 | 30") is None
    assert not is_bogus_date(parse_date("2026-08-18"))
    ok("倒计时字段陷阱：'12/15 | 30' 被 parse_date 啃成 0001 年，已钉死")

    # 判定函数：三种退出码各钉一条
    code, _ = decide(False, [])
    assert code == 2
    code, _ = decide(True, [])
    assert code == 1
    code, why = decide(True, [{"col": "强赎状态", "coverage": 1.0, "n_unique": 3,
                               "near": {"到期": 5}, "far": {"已公告强赎": 3}}])
    assert code == 0, why
    code, why = decide(True, [{"col": "强赎状态", "coverage": 1.0, "n_unique": 2,
                               "near": {"X": 5}, "far": {"X": 3}}])
    assert code == 1, why          # 取值不互斥 → 分不开
    code, why = decide(True, [{"col": "强赎状态", "coverage": 0.3, "n_unique": 3,
                               "near": {"到期": 5}, "far": {"强赎": 3}}])
    assert code == 1, why          # 覆盖率不够 → 按纪律 5 不能当成没强赎
    code, why = decide(True, [{"col": "强赎状态", "coverage": 1.0, "n_unique": 3,
                               "near": {"到期": 1}, "far": {"强赎": 3}}])
    assert code == 1, why          # 样本不足 → 判不了，不是判否
    ok("退出码判据：互斥→0，不互斥/覆盖不足/样本不足→1，表拿不到→2")

    assert looks_forced("公告实施强赎")
    assert not looks_forced("公告不提前赎回")     # 「不提前赎回」是反向信号
    assert not looks_forced("未满足强赎条件")
    assert not looks_forced("到期赎回")
    ok("字面判据先排否定写法：不提前赎回 / 未满足 / 到期赎回 都不算「在强赎」")

    code, why = decide(True, [{"col": "强赎状态", "coverage": 1.0, "n_unique": 3,
                               "near": {"公告实施强赎": 5}, "far": {"已满足条件": 3}}])
    assert code == 1 and any("方向反了" in w for w in why), why
    ok("矛盾检测：取值互斥但字面方向反了 → 不放行，两个证据打架时不下结论")

    print(f"\n全部通过（{n[0]} 条）。")
    return 0


# ═══════════════════════════ 入口 ═══════════════════════════

def main() -> int:
    apply_stream_fix()          # **必须在任何 print 之前**（见 diag_common）
    ap = argparse.ArgumentParser(description="强赎退出线探针·第二次（阶段 2）")
    ap.add_argument("--names", default=None, help="逗号分隔的对照名单，替换内置的 10 只")
    ap.add_argument("--rows", type=int, default=12, help="Q1 里印几行原始数据（默认 12）")
    ap.add_argument("--selftest", action="store_true", help="纯函数离线自测，不联网")
    args = ap.parse_args()

    if args.selftest:
        print("=" * 68)
        print(" diag_redeem2 纯函数自测（不联网）")
        print("=" * 68)
        return selftest()

    names = ([s.strip() for s in args.names.split(",") if s.strip()]
             if args.names else list(_BENCH))

    print("=" * 68)
    print(" 强赎退出线探针·第二次　—— 只答「这一列能不能分开强赎与到期」")
    print("=" * 68)
    try:
        import akshare as ak
    except ImportError:
        print("\n✗ 没装 akshare。pip install -U akshare")
        return 2
    print(f"akshare {getattr(ak, '__version__', '?')} | python {sys.version.split()[0]}")

    _hr("取表")
    df = _fetch(ak, _TABLE)
    xref = _fetch(ak, _XREF)
    if df is None:
        code, why = decide(False, [])
    else:
        probe_columns(df, args.rows)
        evidence = probe_status(df)
        probe_bench(df, xref, names)
        probe_scope(df, xref)
        probe_countdown(df)
        code, why = decide(True, evidence)

    _hr(f"结论（退出码 {code}）")
    for line in why:
        print(f"  {line}")
    print("\n  把这份输出整个贴回来 —— 它是 v5.1 里每个列名和每句措辞的出处。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
