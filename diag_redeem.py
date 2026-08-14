"""阶段 2 的探针：强赎退出线到底做不做得成，先探再写。

**这个脚本不产出任何机会条目，也不接进 `scanner/`。** 它只回答问题。
按项目自己的规矩：探明之前不写实现 —— v3 那次栽的就是照着"应该有"去写。

──────────────────────────────────────────────────────────────────────
为什么单独探一次

日报里「已公告赎回转债」「强赎倒计时」对应的是**硬时点**，而这一类的价值
全在两个日期上：**最后交易日**和**最后转股日**。过了最后交易日还没动手，
拿到的就是赎回价（通常 100 元出头），一只 130 元的债当场变成 100 元。

麻烦在于：这两个日期**在公告正文里，不在标题里**。而 `event_arb` 现在做的
是标题关键词检索 —— 这将是它第一次需要读正文。读不读得到，我这儿断网，
猜不出来，所以先探。

三个问题，一个都不预设答案：

  Q1 这台机器上的 akshare 有哪些和赎回相关的接口？各返回什么列？
  Q2 「最后交易日 / 最后转股日」能不能只靠**表字段**拿到？
     拿不到的话，巨潮那个检索接口返不返回**正文**？
  Q3 一份已知的强赎名单，能对上几只？各自的最后交易日是什么？

──────────────────────────────────────────────────────────────────────
一个方向相反的陷阱，顺便一起探了

「不提前赎回」公告和「提前赎回」公告共享 `赎回` 这个词，而两者的含义
**完全相反**：前者是"这次不赎，继续持有"，后者是"限期离场"。
按关键词粗筛会把它们混成一栏，而混错的代价不是少赚，是照着反方向操作。
脚本会分别数出来给你看。

──────────────────────────────────────────────────────────────────────
用法

    python diag_redeem.py                  # 完整探测（联网，约 30-60 秒）
    python diag_redeem.py --names 春23,嘉泽 # 换一份对照名单
    python diag_redeem.py --fetch 2        # 额外抓 2 份公告正文试解析
    python diag_redeem.py --symbol 113065  # 顺带探需要 symbol 的接口

退出码：
    0 = 最后交易日拿得到 → 阶段 2 可以写实现了，把这份输出留着
    1 = 名单拿得到、日期拿不到 → 得读正文或换数据源，实现先别写
    2 = 连名单都拿不到 → 接口不可用/列名改版，这是取数问题
"""
from __future__ import annotations

import argparse
import inspect
import re
import sys
import time
import unicodedata
from functools import partial

sys.path.insert(0, ".")

from scanner.utils import parse_date, retry_call  # noqa: E402
from diag_common import apply_stream_fix  # noqa: E402

# ---------------------------------------------------------------------------
# 对照名单：2026-08-09 那份日报里的「已公告赎回」9 只 + 「强赎倒计时」1 只。
# 写死它是为了有个**可证伪的验收标准**（跑不出来就是没做成），
# 但它会随时间失效 —— 那 9 只赎回完就从表里掉了。所以脚本同时会把
# 当天的完整强赎名单打出来，晚几周跑照样有答案。--names 可整体替换。
# 注意这些是**简称片段**，不是完整债券简称（日报里就是这么写的），
# 所以用子串模糊匹配，并把匹配到的完整名字印出来供你核对。
_BENCH_ANNOUNCED = ["春23", "嘉泽", "宝莱", "文科", "正帆",
                    "齐翔2", "万孚", "青农", "应流"]
_BENCH_COUNTDOWN = ["宙邦"]

# 候选接口。**不保证存在** —— 脚本用 hasattr 判定并如实报告，
# 同时还会扫一遍 dir(akshare) 自动发现，免得 akshare 改了名字就探空。
_CANDIDATES = [
    ("bond_cb_redeem_jsl", "集思录·强赎汇总：最有可能直接带强赎状态与关键日期"),
    ("bond_zh_cov", "东财转债总表：本工具已在用，看它有没有赎回相关列"),
    ("bond_cb_jsl", "集思录转债总表"),
    ("bond_zh_cov_value_analysis", "转债价值分析（需 symbol）"),
    ("bond_zh_cov_info_ths", "同花顺转债详情（需 symbol）"),
    ("bond_zh_cov_info", "转债详情（需 symbol）"),
]

# 表字段里可能藏着日期的列名线索
_DATE_HINTS = ("赎回", "最后交易", "最后转股", "转股截止", "摘牌", "登记日",
               "到期", "强赎", "停止")

# 巨潮标题检索的候选关键词。「不提前赎回」单列一行，就是为了把反向信号数出来。
_KW_CANDIDATES = ["提前赎回", "不提前赎回", "强制赎回", "赎回结果",
                  "停止交易", "到期赎回"]

# 检索接口返回的列里，哪些名字**可能**装着正文
_BODY_HINTS = ("内容", "正文", "全文", "content", "text", "body", "摘要")


# ═══════════════════════ 纯函数（不联网，可离线自测）═══════════════════════

def norm_text(s) -> str:
    """全角转半角、压掉空白。公告正文里 `２０２６年 ８月` 这种写法不少见。"""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s\u3000]+", "", t)


# 日期：2026年8月18日 / 2026-08-18 / 2026/8/18 都吃
_DATE_RE = re.compile(r"(\d{4})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*日?")

# 标签 → 别名。顺序有讲究：长的先匹配，"最后转股日"不能被"转股日"抢走。
_LABEL_ALIASES = (
    ("最后交易日", ("最后交易日期", "最后交易日", "停止交易日", "终止交易日", "摘牌日")),
    ("最后转股日", ("最后转股日期", "最后转股日", "停止转股日", "转股截止日")),
    ("赎回登记日", ("赎回登记日", "赎回股权登记日")),
    ("赎回价格", ("赎回价格", "赎回价")),
)

# 标签后面多远之内的日期算数它的。40 字是拍的：够覆盖"为2026年8月18日（星期二）"
# 这类插入语，又不至于把下一句的日期抢过来。抢没抢过来，看 snippet 就知道 ——
# **探针印原文片段，不只印解析结果**，解析错了要能一眼看见。
_LOOKAHEAD = 40

# 并列句的标志词，以及"往前看几个字"的窗口。两个都是拍的，
# 拍偏的方向是**多标而不是漏标**：多标只多印一个 ⚠，漏标是一个静悄悄的错日期。
_PARALLEL_HINT = ("分别", "依次")
_NEIGHBOUR = 8


def classify_title(title: str) -> str:
    """公告标题分类。

    「不提前赎回」必须在「提前赎回」之前判 —— 前者包含后者的全部字符，
    顺序反了会把"这次不赎"读成"限期离场"，方向完全相反。
    """
    t = norm_text(title)
    if not t:
        return "空标题"
    if "不提前赎回" in t or "不行使" in t or "不赎回" in t:
        return "不提前赎回"                 # ← 反向信号，绝不能并进强赎
    if "提前赎回" in t or "强制赎回" in t or "强赎" in t:
        return "提前赎回"
    if "赎回结果" in t or "赎回实施" in t or "赎回进展" in t:
        return "赎回结果/实施"
    if "到期赎回" in t or "兑付" in t:
        return "到期赎回"
    if "停止交易" in t or "停止转股" in t or "摘牌" in t:
        return "停止交易提示"
    if "赎回" in t:
        return "其他含赎回"
    return "无关"


def extract_key_dates(text: str) -> dict:
    """从公告正文里找关键日期。

    返回 {标签: {"date": date|None, "raw": 原文片段, "ambiguous": bool}}。

    三种结果都要如实返回，因为它们要的后续动作不同：

    - **找到了**：date 有值、ambiguous 为假。
    - **找到标签没找到日期**：date=None + 原文片段。这是最有价值的一种失败 ——
      说明措辞变了或日期被表格拆开了，不是"这份公告里没这个日期"。
    - **找到了但可能张冠李戴**：ambiguous=True。触发它的是这种并列写法：
      「最后交易日、最后转股日分别为2026年8月18日、2026年8月20日」——
      从"最后转股日"往后找，先撞上的是 8月18日，那是**前一个标签**的值。
      判据有三条，命中任一条就存疑：标签和日期之间夹了另一个标签、
      中间出现「分别／依次」这类并列词、或者紧挨着的前文就是另一个标签。
      （只夹在中间那一条不够用 —— 并列句里**后一个**标签的中间是干净的，
      恰恰是它会拿到错的那个日期。）
      不猜哪个对，只把疑点标出来 —— 探针的职责是报告，不是替你赌一个。
      宁可多标几个：探针里的假警报只多印一个 ⚠，漏标一个就是一个静悄悄的错日期。
    """
    t = norm_text(text)
    all_aliases = [a for _, aliases in _LABEL_ALIASES for a in aliases]
    out = {}
    for label, aliases in _LABEL_ALIASES:
        for alias in aliases:
            i = t.find(alias)
            if i < 0:
                continue
            j = i + len(alias)
            after = t[j:j + _LOOKAHEAD]
            if label == "赎回价格":
                m = re.search(r"(\d+(?:\.\d+)?)\s*元", after)
                out[label] = {"date": None,
                              "value": float(m.group(1)) if m else None,
                              "raw": alias + after, "ambiguous": False}
                break
            m = _DATE_RE.search(after)
            d, amb = None, False
            if m:
                d = parse_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
                others = [a for a in all_aliases if a != alias and a not in alias]
                between = after[:m.start()]
                before = t[max(0, i - _NEIGHBOUR):i]
                amb = (any(a in between for a in others)
                       or any(h in between for h in _PARALLEL_HINT)
                       or any(a in before for a in others))
            out[label] = {"date": d, "raw": alias + after, "ambiguous": bool(amb)}
            if d is not None:
                break          # 拿到日期就收；没拿到就换个别名再试
    return out


def match_names(wanted, pool) -> dict:
    """把简称片段模糊匹配到实际名字。返回 {片段: [匹配到的完整名字…]}。"""
    pool = [str(p) for p in pool]
    return {w: [p for p in pool if w in p] for w in wanted}


# ═══════════════════════════ 联网部分 ═══════════════════════════

def _hr(title: str) -> None:
    print(f"\n{'─' * 68}\n▎{title}")


def _callable_without_args(fn) -> bool:
    """能不能直接无参调用（有必填参数的接口本轮跳过，除非给了 --symbol）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return all(p.default is not inspect.Parameter.empty
               or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
               for p in sig.parameters.values())


def discover(ak) -> list:
    """扫一遍 akshare，自动发现可能相关的接口名。

    curated 那张表是我猜的，这一段是**让机器去找**：akshare 改名、加接口，
    curated 表会探空，而这里照样能捞出来。
    """
    hits = []
    for n in dir(ak):
        if n.startswith("_"):
            continue
        low = n.lower()
        if any(k in low for k in ("redeem", "bond_cb", "bond_zh_cov", "convert")):
            hits.append(n)
    return sorted(set(hits))


def probe_tables(ak, symbol: str | None) -> tuple:
    """Q1 + Q2：接口盘点 + 表字段里有没有现成的日期列。"""
    _hr("Q1  akshare 里有哪些和赎回相关的接口")

    auto = discover(ak)
    print(f"  自动发现 {len(auto)} 个候选：{', '.join(auto) if auto else '（一个都没有）'}")
    curated = [n for n, _ in _CANDIDATES]
    extra = [n for n in auto if n not in curated]
    if extra:
        print(f"  其中 {len(extra)} 个不在我写死的候选表里：{', '.join(extra)}")
        print("  → 说明 akshare 版本比这份脚本新，下面会一并试。")

    frames, verdicts = {}, []
    todo = list(dict.fromkeys(curated + auto))
    for name in todo:
        why = dict(_CANDIDATES).get(name, "（自动发现）")
        fn = getattr(ak, name, None)
        if fn is None:
            print(f"\n  ✗ {name}：本机 akshare 没有这个接口　{why}")
            verdicts.append((name, "缺失"))
            continue
        if not _callable_without_args(fn):
            if symbol:
                call = partial(fn, symbol=symbol)
                label = f"{name}(symbol={symbol})"
            else:
                print(f"\n  – {name}：需要参数，本轮跳过（加 --symbol 代码 可探）　{why}")
                verdicts.append((name, "需参数"))
                continue
        else:
            call, label = fn, name

        t0 = time.time()
        df, err = retry_call(call, label=label, attempts=2, backoff=(2.0,),
                             reject_empty=False)
        sec = time.time() - t0
        if df is None:
            print(f"\n  ✗ {label}：调用失败（{sec:.1f}s）{err}")
            verdicts.append((name, "调用失败"))
            continue
        try:
            rows, cols = len(df), [str(c) for c in df.columns]
        except Exception as e:                      # 不是 DataFrame
            print(f"\n  ? {label}：返回的不是表（{type(df).__name__}）：{e}")
            verdicts.append((name, "非表结构"))
            continue

        print(f"\n  ✓ {label}：{rows} 行 × {len(cols)} 列（{sec:.1f}s）　{why}")
        print(f"    列：{', '.join(cols[:16])}" + ("…" if len(cols) > 16 else ""))
        frames[name] = df
        verdicts.append((name, f"{rows}行"))

    _hr("Q2-a  表字段里有没有现成的「最后交易日 / 最后转股日」")
    date_cols = {}
    for name, df in frames.items():
        hit = [c for c in map(str, df.columns) if any(h in c for h in _DATE_HINTS)]
        if not hit:
            continue
        date_cols[name] = hit
        print(f"\n  {name} 命中 {len(hit)} 列：{', '.join(hit)}")
        for c in hit[:6]:
            vals = [str(v) for v in df[c].dropna().head(3)]
            print(f"    {c:<16} 样例：{vals}")
    if not date_cols:
        print("  没有任何一张表带赎回相关的列。")
        print("  → 那么日期只能从公告正文来，往下看 Q2-b。")
    else:
        print("\n  ↑ **看样例值，别看列名。** 列名叫「赎回价格」而值是 nan 的情况很常见，")
        print("    那等于没有；有值且解析得出日期，才算真拿到了。")
    return frames, date_cols


def probe_announcements(ak, fetch_n: int) -> tuple:
    """Q2-b：巨潮检索能不能拿到正文；顺便把反向信号数出来。"""
    _hr("Q2-b  巨潮检索：标题够不够用，返不返回正文")

    fn = getattr(ak, "stock_zh_a_disclosure_report_cninfo", None)
    if fn is None:
        print("  ✗ 本机 akshare 没有 stock_zh_a_disclosure_report_cninfo")
        print("  → event_arb 那一栏本身也跑不了，先 pip install -U akshare")
        return {}, False

    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=30)          # 探针放宽到 30 天，别探出个空窗
    tally, kept, has_body = {}, [], False

    for i, kw in enumerate(_KW_CANDIDATES):
        if i:
            time.sleep(1.0)                    # 巨潮连打会被掐，和 event_arb 一个道理
        df, err = retry_call(
            partial(fn, symbol="", market="沪深京", keyword=kw,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d")),
            label=f"cninfo({kw})", attempts=2, backoff=(2.0,), reject_empty=False)
        if df is None:
            print(f"\n  ✗ 「{kw}」检索失败：{err}")
            tally[kw] = None
            continue

        cols = [str(c) for c in df.columns]
        body_cols = [c for c in cols if any(h in c.lower() for h in _BODY_HINTS)]
        has_body = has_body or bool(body_cols)

        kinds = {}
        for _, r in df.iterrows():
            k = classify_title(r.get("公告标题", ""))
            kinds[k] = kinds.get(k, 0) + 1
            if k == "提前赎回":
                kept.append(r)
        tally[kw] = (len(df), kinds)
        detail = "，".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1]))
        print(f"\n  「{kw}」：{len(df)} 条　→ {detail or '（无）'}")
        if i == 0:
            print(f"    返回列：{', '.join(cols)}")
            print(f"    正文列：{body_cols if body_cols else '**没有** —— 只给标题+链接'}")

    _hr("Q2-c  反向信号：「不提前赎回」被混进来了没有")
    wrong = sum(v[1].get("不提前赎回", 0) for v in tally.values()
                if isinstance(v, tuple))
    right = sum(v[1].get("提前赎回", 0) for v in tally.values()
                if isinstance(v, tuple))
    print(f"  标题判为「提前赎回」{right} 条，「不提前赎回」{wrong} 条。")
    if wrong:
        print("  → 这 %d 条的含义和强赎**完全相反**（这次不赎、继续持有）。" % wrong)
        print("    真做实现时，关键词只配 `提前赎回` 会把它们一起捞进来，")
        print("    必须在标题层面先排掉 —— classify_title() 那个顺序就是干这个的。")
    else:
        print("  → 本次窗口内没有反向公告。**别把这当成不存在**：换个窗口就会有，")
        print("    这类公告在不触发强赎条件、或公司选择不赎时按季度出。")

    if fetch_n and kept:
        _probe_body(kept[:fetch_n])
    elif fetch_n:
        print("\n  （--fetch 指定了，但本次没有「提前赎回」类公告可抓）")
    return tally, has_body


def _probe_body(rows) -> None:
    """--fetch：真去抓几份公告，看拿回来的是什么、能不能解析出日期。"""
    _hr(f"Q2-d  抓 {len(rows)} 份公告正文试解析")
    try:
        import requests
    except ImportError:
        print("  ✗ 没装 requests，跳过。（pip install requests）")
        return

    from scanner.utils import clean_url, strip_html
    for r in rows:
        title = strip_html(r.get("公告标题", ""))
        url = clean_url(r.get("公告链接", ""))
        print(f"\n  · {strip_html(r.get('简称',''))}　{title[:40]}")
        print(f"    {url}")
        if not url:
            print("    ✗ 没有链接")
            continue
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            print(f"    ✗ 抓取失败：{type(e).__name__}: {e}")
            continue
        ctype = resp.headers.get("Content-Type", "?")
        print(f"    HTTP {resp.status_code}　{ctype}　{len(resp.content):,} 字节")
        if "pdf" in ctype.lower():
            print("    → 拿回来的是 PDF，纯文本解析不了，得先过一层 pdf 提取。")
            continue
        text = resp.text
        got = extract_key_dates(text)
        if not got:
            print("    ✗ 正文里一个目标标签都没找到 —— 大概率是详情页外壳，")
            print("      真正的公告是页面里嵌的 PDF 链接，还得再跳一次。")
            continue
        for label, info in got.items():
            d = info.get("date")
            val = info.get("value")
            shown = d.isoformat() if d else (f"{val} 元" if val is not None else "**没解析出来**")
            warn = "　⚠ 存疑：标签和日期之间夹了另一个标签，可能张冠李戴" \
                if info.get("ambiguous") else ""
            print(f"    {label}：{shown}{warn}")
            print(f"      原文：…{info['raw'][:60]}…")


def check_bench(frames: dict, date_cols: dict, names: list, label: str) -> dict:
    """Q3：对照名单能不能对上，各自的最后交易日是什么。"""
    _hr(f"Q3  对照名单：{label}（{len(names)} 只）")
    if not frames:
        print("  没有任何一张表拿到手，对不了。")
        return {}

    result = {}
    for tname, df in frames.items():
        name_col = next((c for c in map(str, df.columns)
                         if c in ("名称", "债券简称", "转债名称", "简称")), None)
        if name_col is None:
            continue
        hits = match_names(names, df[name_col].astype(str).tolist())
        found = {k: v for k, v in hits.items() if v}
        print(f"\n  在 {tname}（按「{name_col}」模糊匹配）：对上 {len(found)}/{len(names)}")
        for frag, matched in hits.items():
            if not matched:
                print(f"    ✗ {frag}：没对上")
                continue
            row = df[df[name_col].astype(str) == matched[0]].iloc[0]
            dates = []
            for c in date_cols.get(tname, []):
                d = parse_date(row.get(c))
                if d is not None:
                    dates.append(f"{c}={d.isoformat()}")
            shown = "　".join(dates) if dates else "（该行没有可解析的日期）"
            print(f"    ✓ {frag} → {matched[0]}　{shown}")
            result.setdefault(frag, []).extend(dates)
    return result


def main() -> int:
    apply_stream_fix()          # **必须在任何 print 之前**（见 diag_common）
    ap = argparse.ArgumentParser(description="强赎退出线探针（阶段 2）")
    ap.add_argument("--names", default=None,
                    help="逗号分隔的对照名单，替换内置的 08-09 那 9 只")
    ap.add_argument("--fetch", type=int, default=0,
                    help="额外抓 N 份公告正文试解析（默认 0，不抓）")
    ap.add_argument("--symbol", default=None,
                    help="顺带探需要 symbol 的接口，例：--symbol 113065")
    args = ap.parse_args()

    announced = ([s.strip() for s in args.names.split(",") if s.strip()]
                 if args.names else list(_BENCH_ANNOUNCED))

    print("=" * 68)
    print(" 强赎退出线探针　—— 只回答问题，不写实现")
    print("=" * 68)
    try:
        import akshare as ak
    except ImportError:
        print("\n✗ 没装 akshare。pip install -U akshare")
        return 2
    print(f"akshare {getattr(ak, '__version__', '?')} | python {sys.version.split()[0]}")

    frames, date_cols = probe_tables(ak, args.symbol)
    tally, has_body = probe_announcements(ak, args.fetch)

    got = check_bench(frames, date_cols, announced, "已公告赎回")
    if not args.names:
        check_bench(frames, date_cols, _BENCH_COUNTDOWN, "强赎倒计时")

    # ---------------------------------------------------------------
    _hr("结论")
    got_last_trade = any(any("最后交易" in d for d in ds) for ds in got.values())
    hit_names = len(got)

    if got_last_trade:
        print("  ✓ 最后交易日**直接从表字段拿到了**。")
        print(f"    对照名单对上 {hit_names}/{len(announced)} 只。")
        print("  → 阶段 2 可以写实现了：加一个 cb_redeem 源，读上面那张表，")
        print("    按「最后交易日 ∈ [今天, 今天+N交易日]」出条，紧急度按剩余交易日算。")
        print("    把这份输出留着 —— 它是实现里每个列名的出处。")
        return 0

    if frames and hit_names:
        print(f"  – 名单拿得到（对上 {hit_names}/{len(announced)} 只），")
        print("    但**最后交易日没有从表字段拿到**。")
        print("  → 实现先别写。两条路各自的代价先量一量：")
        print("    ① 读公告正文：巨潮检索"
              + ("**返回了**正文列，可以试" if has_body else "**只给标题+链接**，"
                 "还得再跳一次详情页，且正文多半是 PDF"))
        print("    ② 换数据源：集思录/同花顺的转债详情页可能直接带这两个日期，")
        print("       但那是新接口，要按 diag_sources.py 那套先验一次可用性。")
        print("    在两条路里选之前，先回答一句：这一栏每天大概几条？")
        print("    如果常年 1-2 条，人工点开公告的成本可能低于把正文解析做对。")
        return 1

    print("  ✗ 连名单都没拿到 —— 这是取数问题，不是策略问题。")
    print("    上面 Q1 里每个接口的失败原因就是线索；先跑 python diag_sources.py")
    print("    确认网络，再 pip install -U akshare 试一次。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
