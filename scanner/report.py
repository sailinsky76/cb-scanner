"""报告渲染：终端(console) / Markdown / HTML。

设计：各类机会 metrics 键并不一致（申购 vs 缴款、溢价 vs 折价…），
所以按“每条一小块”渲染，而不是强行套统一表格 —— 对异构字段更稳健，
作为每日速览也更好读。顶部给“今日待办”聚焦硬性时点。
"""
from __future__ import annotations

import html as _html
import re
from datetime import date

from .models import SOURCE_KEYS, Kind, SourceResult, Urgency

# ---- markdown 强调记号的收口（v4.6.1）------------------------------------
# 提示与脚注是照 markdown 写的，`**这样**` 标重点。可 config 默认的 formats 是
# ["console", "html"]，markdown 那一路根本没开 —— 于是这些记号原样打进了终端和
# 网页：08-09 那份 HTML 里 7 处、console 里 4 处裸露的 `**…**`。
#
# 三种渲染各有各的正确做法，所以不在源字符串里删，而是在渲染层分别处理：
#   console  → 去掉记号（终端没有粗体这个概念）
#   html     → 转成 <b>（先转义再替换：转义不产生 `*`，顺序安全）
#   markdown → 原样保留，它本来就是这个语法
_EMPH_RE = re.compile(r"\*\*(.+?)\*\*")


def _plain(text) -> str:
    """去掉 markdown 的 ** 强调记号，供终端使用。"""
    return _EMPH_RE.sub(r"\1", str(text))

# 新栏目**必须登记在这里**，不能只靠 _group_by_kind 的自动追加：
# 那一路是 `by_kind.setdefault(o.kind, …)`，**一条都没命中的栏目根本不进字典** ——
# 于是 0 条那天整栏连标题带栏目说明一起消失，和「今天没有」长得一模一样。
# v5.9 的 cb_approved 恰好是「某天可能真的 0 条」的栏目（180 天累计量），
# 而它的栏目说明里有两句必须每天都在（下限不是全集 / 上了名单≠值得埋伏）。
_KIND_ORDER = [Kind.CB_IPO, Kind.CB_REDEEM, Kind.CB_ALLOT,
               Kind.FUND_PREM, Kind.EVENT, Kind.CB_APPROVED]
_URG_BADGE = {Urgency.TODAY: "🔴今日", Urgency.SOON: "🟠临近", Urgency.WATCH: "⚪观察"}
# console 走 stdout → Windows GBK 编不了 emoji；ASCII 替代
_URG_BADGE_CON = {Urgency.TODAY: "[!]今日", Urgency.SOON: "[~]临近", Urgency.WATCH: "[ ]观察"}


def _all_opps(results):
    out = []
    for r in results:
        out.extend(r.opportunities)
    return out


def _metrics_line(m: dict) -> str:
    return " | ".join(f"{k}: {v}" for k, v in m.items())


def _result_map(results):
    """Kind -> SourceResult，用于把错误/口径提示贴回对应栏目。"""
    return {r.kind: r for r in results}


def _banners(res) -> list:
    """某个栏目要展示的告警行。

    错误排在口径提示前面：先说「这栏没跑对」，再说「所以哪个结论不能信」。
    """
    if res is None:
        return []
    out = []
    if res.error:
        out.append(f"本栏取数异常：{res.error}")
    out.extend(res.notes)
    return out


def _footnotes(results) -> list:
    """全报告的常量口径说明，去重后保持首次出现的次序。

    这一层是 v4.4 加的。之前所有解释性文字都挂在**每一条**上，于是同一句
    「买卖价差…已折进净收益…」在 08-08 的报告里出现了 5 遍、每遍 111 字。
    它们和当天的数字无关，属于「读第一遍要看、读第五十遍碍事」的那类，
    统一沉到报告末尾印一次。
    """
    out, seen = [], set()
    for r in results:
        for f in getattr(r, "footnotes", ()) or ():
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def _empty_text(res, kind=None, ctx=None) -> str:
    """空栏目的文案。取数失败 / 源被关掉时都必须说明白 0 条不等于没机会。

    v5.9.3 补的是第二种：`run.py` 跳过 `sources: {xxx: false}` 的源，于是
    `results` 里根本没有它 → `res is None` → 上一版原样印一个「无」。而
    `_KIND_ORDER` 特意保证栏目**不消失**（0 条那天栏目说明也要在），两件事凑在
    一起的效果是：关掉一个源，报告里那一栏和「今天没有」长得**一模一样**。
    这正是本项目一路在修的那种 0 —— 而且这次连「取数异常」的旗都没有可挂的。

    判据从 ctx.cfg 现取，不另存一份状态：报告要说的是「这一栏这次为什么空」，
    而那个理由就写在配置里。
    """
    if res is not None and res.error:
        return "无 —— 但本栏取数异常，0 条不代表没有机会"
    if res is None and kind is not None and ctx is not None:
        key = SOURCE_KEYS.get(kind)
        if key and not (getattr(ctx, "cfg", None) or {}).get("sources", {}).get(key, True):
            return (f"无 —— **本栏已在 config.yaml 的 sources.{key} 里关掉了**，"
                    "本次根本没跑，不是「今天没有」")
    return "无"


def _group_by_kind(opps):
    """按 Kind 分组，并给出渲染顺序。

    已知类别按 _KIND_ORDER 固定顺序在前（即使没命中也保留“无”栏位），
    后续扩展的新数据源（如审计层）自动追加在后面 —— 这样在 models.py 加一个
    Kind、写一个新 Source，不必回来改渲染层。
    """
    by_kind = {k: [] for k in _KIND_ORDER}
    for o in opps:
        by_kind.setdefault(o.kind, []).append(o)
    order = _KIND_ORDER + [k for k in by_kind if k not in _KIND_ORDER]
    return by_kind, order


# ----------------------------- 终端 -----------------------------
def render_console(results, ctx) -> str:
    L = []
    opps = _all_opps(results)
    today_items = [o for o in opps if o.urgency == Urgency.TODAY]

    L.append("=" * 64)
    L.append(f" 低容量套利扫描  {ctx.today.isoformat()}   本金 {int(ctx.capital):,} × {ctx.accounts} 户")
    L.append("=" * 64)

    # 今日待办
    L.append(f"\n【今日待办】{len(today_items)} 条")
    if today_items:
        for o in sorted(today_items, key=lambda x: x.sort_key()):
            L.append(f"  → [{o.kind.value}] {o.name}（{o.code}）：{o.action}")
    else:
        L.append("  （今日无硬性时点）")

    # 分类明细
    by_kind, order = _group_by_kind(opps)
    rmap = _result_map(results)

    for k in order:
        items = sorted(by_kind[k], key=lambda x: x.sort_key())
        r = rmap.get(k)
        L.append(f"\n{'-'*64}\n▎{k.value}  （{len(items)} 条）")
        for b in _banners(r):
            L.append(f"  [!] {b}")
        if not items:
            L.append(f"  {_empty_text(r, k, ctx)}")
            continue
        for o in items:
            L.append(f"\n  {_URG_BADGE_CON[o.urgency]}  {o.name}（{o.code}）")
            L.append(f"      动作：{o.action}")
            if o.metrics:
                L.append(f"      {_metrics_line(o.metrics)}")
            if o.flags:
                L.append("      [!] " + "；".join(o.flags))
            if o.link:
                L.append(f"      链接：{o.link}")
            if o.note:
                L.append(f"      注：{o.note}")

    # 口径与假设（常量说明，全报告一次）
    fns = _footnotes(results)
    if fns:
        L.append(f"\n{'-'*64}\n▎口径与假设（每天一样，看过一次可跳过）")
        for i, f in enumerate(fns, 1):
            L.append(f"  {i}. {f}")

    # 数据源健康
    L.append(f"\n{'-'*64}\n▎数据源健康")
    for r in results:
        status = "OK" if not r.error else f"ERROR({r.error})"
        L.append(f"  {r.kind.value}: 扫描 {r.rows_scanned} 行, 命中 {len(r.opportunities)} 条 [{status}]")

    L.append("\n（以上为规则命中提示，非投资建议；套利收益随资金增长必然摊薄，数字为数量级估算）")
    # 在**出口**统一去记号，而不是逐个字段去：漏一个字段就又漏出去一次，
    # 而这一层是所有文本的唯一出口。verify_report 读的也是这份文本，
    # 于是 ⑤ 的字数预算量的就是读者真正看到的字数。
    return _plain("\n".join(L))


# --------------------------- Markdown ---------------------------
def render_markdown(results, ctx) -> str:
    opps = _all_opps(results)
    today_items = [o for o in opps if o.urgency == Urgency.TODAY]
    M = [f"# 低容量套利每日扫描 · {ctx.today.isoformat()}",
         "",
         f"本金 **{int(ctx.capital):,}** × {ctx.accounts} 户　|　"
         f"命中合计 **{len(opps)}** 条　|　今日待办 **{len(today_items)}** 条",
         ""]

    M.append("## 🔴 今日待办")
    if today_items:
        for o in sorted(today_items, key=lambda x: x.sort_key()):
            M.append(f"- **[{o.kind.value}]** {o.name}（`{o.code}`）— {o.action}")
    else:
        M.append("- 今日无硬性时点")
    M.append("")

    by_kind, order = _group_by_kind(opps)
    rmap = _result_map(results)

    for k in order:
        items = sorted(by_kind[k], key=lambda x: x.sort_key())
        r = rmap.get(k)
        M.append(f"## {k.value}（{len(items)} 条）")
        for b in _banners(r):
            M.append(f"> ⚠ {b}\n")
        if not items:
            M.append(f"_{_empty_text(r, k, ctx)}_\n")
            continue
        for o in items:
            M.append(f"### {_URG_BADGE[o.urgency]} {o.name}（`{o.code}`）")
            M.append(f"- **动作**：{o.action}")
            if o.metrics:
                M.append("- " + "　".join(f"`{kk}={vv}`" for kk, vv in o.metrics.items()))
            if o.flags:
                M.append("- ⚠ " + "；".join(o.flags))
            if o.link:
                M.append(f"- [公告链接]({o.link})")
            if o.note:
                M.append(f"- _{o.note}_")
            M.append("")

    M.append("---")
    fns = _footnotes(results)
    if fns:
        M.append("### 口径与假设（每天一样，看过一次可跳过）")
        for i, f in enumerate(fns, 1):
            M.append(f"{i}. {f}")
        M.append("")
    M.append("### 数据源健康")
    for r in results:
        status = "✅ OK" if not r.error else f"❌ {r.error}"
        M.append(f"- {r.kind.value}：扫描 {r.rows_scanned} 行，命中 {len(r.opportunities)} 条 — {status}")
    M.append("\n> 以上为规则命中提示，不构成投资建议；套利收益随资金增长摊薄，百分比为数量级估算。")
    return "\n".join(M)


# ----------------------------- HTML -----------------------------
def render_html(results, ctx) -> str:
    def esc(x):
        """转义。用于链接、属性这类**不该**被当正文解析的位置。"""
        return _html.escape(str(x))

    def esct(x):
        """转义 + 把 `**重点**` 变成 <b>。只用于正文，别用在 href 里。"""
        return _EMPH_RE.sub(r"<b>\1</b>", esc(x))

    opps = _all_opps(results)
    today_items = [o for o in opps if o.urgency == Urgency.TODAY]
    css = """
    body{font:14px/1.6 -apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
         max-width:900px;margin:24px auto;padding:0 16px;color:#1f2328;background:#fff}
    h1{font-size:20px;border-bottom:2px solid #d0d7de;padding-bottom:8px}
    h2{font-size:16px;margin-top:28px;border-left:3px solid #0969da;padding-left:8px}
    .todo{background:#fff8e6;border:1px solid #f0d58a;border-radius:8px;padding:10px 14px}
    .card{border:1px solid #d0d7de;border-radius:8px;padding:10px 14px;margin:10px 0}
    .m{color:#57606a;font-size:12.5px;margin-top:4px}
    .m code{background:#f6f8fa;border-radius:4px;padding:1px 5px;margin-right:4px}
    .flag{color:#9a6700;font-size:12.5px;margin-top:4px}
    .alert{background:#fff1f0;border:1px solid #ffb3ae;border-left:4px solid #cf222e;
           border-radius:6px;padding:8px 12px;margin:8px 0;color:#82071e;font-size:13px}
    .badge{font-size:12px;padding:1px 7px;border-radius:10px;color:#fff}
    .today{background:#cf222e}.soon{background:#bc4c00}.watch{background:#6e7781}
    a{color:#0969da}.foot{color:#6e7781;font-size:12px;margin-top:24px}
    .fn{margin-top:24px;border-top:1px solid #d0d7de;padding-top:12px;
        color:#57606a;font-size:12.5px}
    .fn summary{cursor:pointer;color:#6e7781;user-select:none}
    .fn ol{margin:8px 0 0;padding-left:20px}.fn li{margin:6px 0;line-height:1.55}
    """
    B = [f"<!doctype html><meta charset='utf-8'><title>套利扫描 {esc(ctx.today)}</title>",
         f"<style>{css}</style>",
         f"<h1>低容量套利每日扫描 · {esc(ctx.today)}</h1>",
         f"<p>本金 {int(ctx.capital):,} × {ctx.accounts} 户　|　命中 {len(opps)} 条　|　"
         f"今日待办 {len(today_items)} 条</p>"]

    B.append("<div class='todo'><b>🔴 今日待办</b><ul>")
    if today_items:
        for o in sorted(today_items, key=lambda x: x.sort_key()):
            B.append(f"<li>[{esc(o.kind.value)}] {esc(o.name)}（{esc(o.code)}）— {esct(o.action)}</li>")
    else:
        B.append("<li>今日无硬性时点</li>")
    B.append("</ul></div>")

    badge_cls = {Urgency.TODAY: "today", Urgency.SOON: "soon", Urgency.WATCH: "watch"}
    by_kind, order = _group_by_kind(opps)
    rmap = _result_map(results)
    for k in order:
        items = sorted(by_kind[k], key=lambda x: x.sort_key())
        r = rmap.get(k)
        B.append(f"<h2>{esc(k.value)}（{len(items)}）</h2>")
        for b in _banners(r):
            B.append(f"<div class='alert'>⚠ {esct(b)}</div>")
        if not items:
            B.append(f"<p class='m'>{esct(_empty_text(r, k, ctx))}</p>")
            continue
        for o in items:
            B.append("<div class='card'>")
            B.append(f"<span class='badge {badge_cls[o.urgency]}'>{esc(o.urgency.value)}</span> "
                     f"<b>{esc(o.name)}</b>（{esc(o.code)}）")
            B.append(f"<div>动作：{esct(o.action)}</div>")
            if o.metrics:
                B.append("<div class='m'>" +
                         "".join(f"<code>{esc(kk)}={esct(vv)}</code>" for kk, vv in o.metrics.items()) +
                         "</div>")
            if o.flags:
                B.append("<div class='flag'>⚠ " + esct("；".join(o.flags)) + "</div>")
            if o.link:
                B.append(f"<div class='m'><a href='{esc(o.link)}'>公告链接</a></div>")
            # v5.9.1：这一行**曾经不存在** —— console(149) 和 markdown(211) 都印 note，
            # 只有 HTML 这一路从头到尾没引用过它，于是 08-11 那份 md 里的 7 条条目级
            # 「注」在同一次运行的 html 里一条都没有。而 config 默认 formats 不含
            # markdown，HTML 恰恰是每天真正被读的那一份 —— 丢掉的包括打新那条
            # 「上市日是这条线上唯一要你判断的一天」、配债的面值口径，以及
            # cb_approved 的「获批日期取最早那条 / 该正股另有更早的转债」。
            # 后两句尤其要紧：本栏按正股代码归并，一家公司在窗口里有两个批次时
            # 日期会取偏，而唯一提示这件事的话只挂在 note 上。
            # 没被拦住是因为 verify_report 只解析 console 文本，5 条不变量不读 HTML；
            # 现在 selftest 里 test_item_note_reaches_every_format 钉住了三种格式。
            if o.note:
                B.append(f"<div class='m'>注：{esct(o.note)}</div>")
            B.append("</div>")

    fns = _footnotes(results)
    if fns:
        # HTML 用 <details> 折起来：网页上连"看过一次可跳过"的一行都不用占。
        B.append("<details class='fn'><summary>口径与假设（每天一样，"
                 f"共 {len(fns)} 条）</summary><ol>")
        for f in fns:
            B.append(f"<li>{esct(f)}</li>")
        B.append("</ol></details>")

    B.append("<div class='foot'>")
    for r in results:
        status = "OK" if not r.error else f"ERROR: {esct(r.error)}"
        B.append(f"{esc(r.kind.value)}：{r.rows_scanned} 行 / {len(r.opportunities)} 条 [{status}]<br>")
    B.append("以上为规则命中提示，不构成投资建议。</div>")
    return "".join(B)
