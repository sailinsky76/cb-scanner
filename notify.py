#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
notify.py —— 把当天的扫描报告推到手机。

**独立文件，一个字都不动 run.py / scanner/。**
理由和 STATE.md 的维护期纪律一致：run.py 那条路被 96 条断言和两份回放钉着，
推送是一件与「报告对不对」无关的事，塞进去等于让一个 HTTP 超时能影响报告本身。
这个脚本只读 reports/ 里已经落盘的 .md / .html，读不到就照实说。

依赖：只用标准库（urllib / smtplib / email），不新增任何 requirements。

用法：
    python notify.py --code 0                    # 读今天那份报告并推送
    python notify.py --code 2                    # 全部源失败，推告警
    python notify.py --code 0 --date 20260813    # 指定日期
    python notify.py --code 0 --dry-run          # 只打印要推什么，**不发、不吃额度**
    python notify.py --selftest                  # 不联网，验证解析器 / 转换器 / 发送次数

渠道靠环境变量选，填哪个用哪个，可同时填多个：
    SC_SENDKEY          Server酱 Turbo（sctapi.ftqq.com，微信服务号；免费 5 条/天）
    SMTP_USER      邮件发件地址，如 you@126.com    ← **全文走这条**
    SMTP_PASS      邮箱「客户端授权码」，**不是登录密码**
    SMTP_TO        收件人，选填，默认发给自己
    SMTP_HOST / SMTP_PORT  选填，覆盖自动推导（465=SSL，587=STARTTLS）
    BARK_URL       Bark（iOS，形如 https://api.day.app/你的key）
    FEISHU_WEBHOOK 飞书自定义机器人
    DINGTALK_WEBHOOK 钉钉自定义机器人（若设了加签，另填 DINGTALK_SECRET）

退出码：
    0 = 至少一个渠道推送成功
    1 = 配了渠道但全部失败   ← daily_run.ps1 看到这个就**不关机**
    2 = 一个渠道都没配置

============================================================================
v3 改了两处，都是「同一条信息在不同渠道该长什么样」这个问题的两面。

① **新增邮件渠道，它负责全文。**
   分工定死：Server酱 管「今天要不要动手」（横幅那一眼，**内容与 v2 一字不变**），
   邮件管「具体怎么动手」（HTML 报告直接渲染在邮件正文里，71 条一条不少）。

   为什么不让 Server酱 发全文：微信服务号模板消息的展示层砍在开头几十字，
   把全文塞进 desp 只会让详情页变长，横幅那一眼并不会因此多告诉你一个字。
   为什么 HTML 进正文而不是只当附件：附件要点开，点开还可能没有预览器 ——
   正文是「打开邮件就看见」的东西，少一次点击就少一次「今天算了」。

   **结构变化**：渠道函数入参从 (title, body) 改成一个 Payload。
   邮件要的是全文和 HTML，摘要里根本没有这两样 —— 签名不动就传不进去。
   Server酱 收到的 title / desp 与 v2 逐字相同，变的只是取值路径。
   自检里有一条 (Server酱|推的还是摘要不是全文) 专门钉住这件事。

② **修飞书：msg_type=text 不渲染 markdown。**
   v2 往 text 里塞 markdown，飞书原样显示 `## 🔴 今日待办` 和 `**动作**：`。
   而且换成 interactive 卡片还不够 —— 卡片 JSON 1.0 的 lark_md 支持
   加粗 / 斜体 / 列表 / 链接 / 代码块，但**不支持 `#` 标题、`>` 引用、`行内代码`**，
   这三样会原样显示。JSON 2.0 支持，但自定义机器人发 2.0 会被服务端退回
   「请升级至最新版本客户端」并替换成兜底消息。所以只能 1.0 + 降级转换。

   这个 bug 现在没暴露，是因为只配了 SC_SENDKEY。它属于「配上就坏」那一类 ——
   等真要用飞书时才发现，就是在最不想调试的时候调试。
============================================================================

--- v2 修的两处（实盘第一天暴露的），保留备查 --------------------------------
① **每个渠道发了两条。** v1 拿 `probe = fn(title, body)` 当「这个渠道配了没」
   的判据，可 fn 只在**没配**时才提前返回 None —— 配了就先发送再返回。
   于是那个叫 probe 的调用其实是第一次发送。
   改法：配置检查（纯函数，只读环境变量）和发送彻底分开，见 CHANNELS。
② **微信里看到的是截断的。** 不是本脚本的问题，是微信模板消息的字段限制。
   本脚本能做的是让**截断的那一段本身就够用**：待办数和退出数提到标题里。
   至于「看全文」，v3 交给邮件了。
----------------------------------------------------------------------------
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape as _esc
from pathlib import Path
from typing import NamedTuple

TIMEOUT = 20
RETRIES = 3
REPORT_DIR = Path(__file__).resolve().parent / "reports"

# 退出码 → 一句人话。和 README「怎么读一份残缺的报告」那张表同源。
CODE_TEXT = {
    0: ("✅", "六个源都完整取到数"),
    1: ("⚠️", "报告已生成，但**有源取数失败或结果残缺** —— 空栏目不代表没有机会"),
    2: ("🛑", "**所有源都失败，这份报告没有参考价值**"),
}


class FatalChannelError(Exception):
    """不该重试的失败。

    典型是 SMTP 认证错误：授权码填错了，重试三次不会变对，
    只会让网易那边把你这个 IP 记上一笔。要区分「暂时不通」和「配错了」。
    """


class Payload(NamedTuple):
    """一次推送要发的全部素材。

    为什么不是 (title, body)：不同渠道要的东西不一样。
      · Server酱 / Bark / 飞书 / 钉钉 —— 要 title + body（摘要）
      · 邮件                          —— 要 md_full + html（全文）
    把它们塞进一个只有两格的签名里，结果就是全文永远传不进来。
    """
    title: str      # 通知标题，短，微信横幅上截不掉的那部分
    body: str       # 摘要正文（markdown），summarize() 的产物
    md_full: str    # 报告全文 markdown，读不到就是 ""
    html: str       # 报告全文 HTML，读不到就是 ""
    stamp: str      # YYYYMMDD
    code: int       # run.py 的退出码


# ---------------------------------------------------------------- 解析

def _lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _section(lines, start_pred, stop_pred):
    """取出 [满足 start_pred 的行之后, 满足 stop_pred 的行之前) 这一段。"""
    out, on = [], False
    for ln in lines:
        if on:
            if stop_pred(ln):
                break
            out.append(ln)
        elif start_pred(ln):
            on = True
    return out


def _columns(lines):
    """从 '## 可转债打新（6 条）' 这类抬头取出 (栏名, 条数)。
    '## 🔴 今日待办' 没有「（N 条）」，天然不会被匹配进来。"""
    out = []
    for ln in lines:
        m = re.match(r"^##\s+(.+?)[（(](\d+)\s*条[）)]\s*$", ln.strip())
        if m:
            out.append((m.group(1).strip(), int(m.group(2))))
    return out


def summarize(md_text):
    """
    返回 (title, body)。**v3 未改动本函数，输出与 v2 逐字相同。**

    从报告 markdown 里抽出**手机横幅上真正要看的那部分**：
      · 今日待办  —— 有 deadline 的事，漏了有实际代价
      · 转债退出提醒 —— 单条金额最大的一栏
      · 数据源健康 —— 只在有源不 OK 时才印；全绿时一个字不说

    折溢价那一栏**故意不推**：它要盯盘口、要卡 14:45 之后的窗口，
    在早上九点的通知里印一个净收益，等于替你认定这条路走得通。

    标题带上「待办 N · 退出 M」：微信服务号模板消息会截断正文，
    而标题不会 —— 所以判断「今天要不要动手」的信息必须放在标题里。
    """
    lines = _lines(md_text)
    parts = []

    raw_title = next((l for l in lines if l.startswith("# ")), "").lstrip("# ").strip()
    headline = next((l.strip() for l in lines if "本金" in l and "命中合计" in l), "")
    if headline:
        parts.append(headline)

    todo = _section(
        lines,
        lambda l: l.startswith("## ") and "今日待办" in l,
        lambda l: l.startswith("## "),
    )
    todo = [l for l in todo if l.strip().startswith("- ")]
    if todo:
        parts.append("\n## 🔴 今日待办\n" + "\n".join(todo))
    else:
        parts.append("\n## 今日待办\n- （无）")

    exits = _section(
        lines,
        lambda l: l.startswith("## ") and "转债退出提醒" in l,
        lambda l: l.startswith("## "),
    )
    picked = []
    for i, ln in enumerate(exits):
        if ln.startswith("### ") and ("🔴" in ln or "🟠" in ln):
            name = ln.lstrip("# ").strip()
            act = next(
                (
                    x.strip().replace("- **动作**：", "")
                    for x in exits[i + 1 : i + 4]
                    if "**动作**" in x
                ),
                "",
            )
            picked.append(f"- {name}\n  {act}" if act else f"- {name}")
    if picked:
        parts.append(
            "\n## ⏳ 转债退出（最后交易日临近）\n"
            + "\n".join(picked)
            + "\n\n> 本栏不区分强赎与到期，两者处置方向相反 —— 动手前看公告"
        )

    health = _section(
        lines,
        lambda l: l.startswith("### ") and "数据源健康" in l,
        lambda l: l.startswith("---") or l.startswith("> 以上为"),
    )
    bad = [l for l in health if l.strip().startswith("- ") and "OK" not in l]
    if bad:
        parts.append("\n## ⚠️ 取数异常\n" + "\n".join(bad))

    # 日期：标题里是 2026-08-13，报告文件名是 20260813
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw_title)
    stamp = f"{m.group(2)}-{m.group(3)}" if m else raw_title
    fname = f"scan_{m.group(1)}{m.group(2)}{m.group(3)}.html" if m else "今天那份.html"

    # 【栏目全貌】少推可以，不说不可以。
    cols = _columns(lines)
    if cols:
        inv = " · ".join(f"{n} {c}" for n, c in cols)
        parts.append(
            "\n## 📋 报告全貌（六栏）\n" + inv
            + f"\n\n> 手机只推「今日待办 + 转债退出」两项。折溢价那栏要盯实时盘口、"
            f"要卡 14:45 之后的窗口，在早上的通知里印净收益等于替你认定这条路走得通 —— "
            f"其余各栏开电脑看 `reports/{fname}`"
        )

    title = f"{stamp} 待办{len(todo)} · 退出{len(picked)}"
    if bad:
        title += " · 源异常"

    return title, "\n".join(parts).strip()


# ---------------------------------------------------------------- 飞书转换
# 【v3 修的 bug 在这里】
# lark_md（卡片 JSON 1.0）能渲染：**粗体** *斜体* ~~删除线~~ [文字](链接) 列表 代码块
# lark_md 渲染不了：# 标题、> 引用、`行内代码`   ← 这三样会原样显示给你看
# JSON 2.0 全都支持，但自定义机器人发 2.0 会被服务端退回并替换成兜底消息。
# 所以只能在发送前把这三样降级掉。降级是有损的，但「有损」好过「显示成乱码」。

def _to_lark_md(md):
    """把报告 markdown 转成 lark_md 认得的子集。纯函数。"""
    out = []
    for ln in _lines(md):
        s = ln.rstrip()
        m = re.match(r"^\s*#{1,6}\s+(.*)$", s)
        if m:                                   # ## 标题 → **标题**
            t = m.group(1).strip().rstrip("#").strip()
            out.append(f"**{t}**" if t else "")
            continue
        st = s.lstrip()
        if st.startswith(">"):                  # > 引用 → 💡 前缀
            out.append("💡 " + st.lstrip(">").strip())
            continue
        out.append(s)
    txt = "\n".join(out)
    # 去掉**单个**反引号（行内代码），保留 ``` 围栏 —— lark_md 认代码块
    txt = re.sub(r"(?<!`)`(?!`)", "", txt)
    return txt.strip()


def _chunk(text, size):
    """按行切块，不切断行。单个卡片元素过长会被飞书截掉，分块更稳。"""
    blocks, cur, n = [], [], 0
    for ln in text.split("\n"):
        if cur and n + len(ln) + 1 > size:
            blocks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        blocks.append("\n".join(cur))
    return blocks or [""]


LARK_HEADER_COLOR = {0: "green", 1: "orange", 2: "red"}


def _feishu_card(p):
    """构造飞书 interactive 卡片（JSON 1.0）。纯函数，便于离线断言。"""
    text = _to_lark_md(p.body)
    elements = []
    for i, c in enumerate(_chunk(text, 3000)[:8]):     # 封顶 8 块，防卡片超限
        if i:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": c}})
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": p.title[:100]},
                "template": LARK_HEADER_COLOR.get(p.code, "grey"),
            },
            "elements": elements,
        },
    }


# ---------------------------------------------------------------- 邮件构造
# 同样的纪律：构造（纯函数）和发送（有副作用）分开。
# v2 那个重复发送的 bug，根子就是这两件事被揉进了一个函数。

SMTP_PRESETS = {
    "126.com": ("smtp.126.com", 465),
    "163.com": ("smtp.163.com", 465),
    "yeah.net": ("smtp.yeah.net", 465),
    "qq.com": ("smtp.qq.com", 465),
    "foxmail.com": ("smtp.qq.com", 465),
    "gmail.com": ("smtp.gmail.com", 465),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
}


def _smtp_host_port(addr, host_override="", port_override=""):
    """从发件地址推导 SMTP 服务器。纯函数 —— 覆盖值由调用方从环境变量取进来。"""
    if host_override:
        return host_override, int(port_override or 465)
    domain = addr.rsplit("@", 1)[-1].strip().lower()
    if domain in SMTP_PRESETS:
        h, pt = SMTP_PRESETS[domain]
        return h, int(port_override or pt)
    return f"smtp.{domain}", int(port_override or 465)


def _plain_fallback_html(title, md):
    """报告没有 HTML 版时的兜底：把 markdown 原样包进 <pre>。

    比「什么都不发」好，比「假装排好版了」诚实。
    """
    return (
        '<html><body style="margin:0;padding:16px;background:#fff">'
        f'<h3 style="font-family:-apple-system,sans-serif">{_esc(title)}</h3>'
        '<pre style="font-family:ui-monospace,Menlo,Consolas,monospace;'
        'font-size:13px;line-height:1.65;white-space:pre-wrap;word-break:break-word">'
        f'{_esc(md)}</pre></body></html>'
    )


def build_email(p, sender, to):
    """构造整封邮件。纯函数 —— 不碰网络，可离线断言。

    结构（multipart/mixed）
      └ multipart/alternative
          ├ text/plain  = 报告全文 markdown   ← 便于搜索，纯文本客户端可读
          └ text/html   = 报告全文 HTML       ← 手机邮件客户端直接渲染，不用点附件
      ├ scan_YYYYMMDD.html   （附件，给浏览器打开和归档用）
      └ scan_YYYYMMDD.md     （附件）
    """
    msg = EmailMessage()
    # 邮件头不能带换行，否则是 header injection
    msg["Subject"] = p.title.replace("\n", " ").replace("\r", " ").strip()
    msg["From"] = formataddr(("转债扫描器", sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] or "localhost")

    plain = p.md_full or p.body
    msg.set_content(plain)
    msg.add_alternative(
        p.html if p.html else _plain_fallback_html(p.title, plain),
        subtype="html",
    )

    if p.html:
        msg.add_attachment(
            p.html.encode("utf-8"), maintype="text", subtype="html",
            filename=f"scan_{p.stamp}.html", params={"charset": "utf-8"},
        )
    if p.md_full:
        msg.add_attachment(
            p.md_full.encode("utf-8"), maintype="text", subtype="markdown",
            filename=f"scan_{p.stamp}.md", params={"charset": "utf-8"},
        )
    return msg


# ---------------------------------------------------------------- 渠道
# 【关键结构，v2 留下的】配置检查是**纯函数**（只读环境变量），发送是另一个函数。
# 任何一个既回答问题又产生副作用的函数，早晚会被当成谓词调用一次。

def _post(url, payload, headers=None, form=False):
    if form:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read().decode("utf-8", "replace")[:300]


def _env(name):
    return os.environ.get(name, "").strip()


def send_serverchan(p):
    """**推的内容与 v2 完全相同**：还是摘要，不是全文。全文归邮件管。

    唯一改动：title 从 [:100] 收到 [:32]，因为接口文档写的上限就是 32。
    今天的标题约 24 字，实际行为一致 —— 这只是把「将来往标题里加字段」的地雷拆掉。
    """
    st, txt = _post(
        f"https://sctapi.ftqq.com/{_env('SC_SENDKEY')}.send",
        {"title": p.title.replace("\n", " ")[:32], "desp": p.body[:31000]},
        form=True,
    )
    return (st == 200 and '"code":0' in txt.replace(" ", ""), txt)


def send_email(p):
    """全文渠道。HTML 直接进正文，附件同时带上。"""
    sender = _env("SMTP_USER")
    to = _env("SMTP_TO") or sender
    host, port = _smtp_host_port(sender, _env("SMTP_HOST"), _env("SMTP_PORT"))
    msg = build_email(p, sender, to)
    ctx = ssl.create_default_context()
    try:
        if port == 587:
            with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
                s.starttls(context=ctx)
                s.login(sender, _env("SMTP_PASS"))
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT) as s:
                s.login(sender, _env("SMTP_PASS"))
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        # 授权码错 / SMTP 服务没开。重试三次不会变对，只会让网易记你一笔。
        raise FatalChannelError(
            f"认证失败（{e.smtp_code}）—— 检查 SMTP_PASS 是不是「客户端授权码」"
            f"而非登录密码，以及网页版邮箱里 SMTP 服务是否已开启"
        ) from e
    kb = (len(p.html.encode("utf-8")) if p.html else 0) // 1024
    return (True, f"→ {to} via {host}:{port}，HTML 正文 {kb}KB")


def send_bark(p):
    # Bark 的通知体不适合长 markdown，只送前几行
    short = "\n".join(p.body.split("\n")[:14])[:900]
    st, txt = _post(
        _env("BARK_URL").rstrip("/"),
        {"title": p.title[:60], "body": short, "group": "cb_scanner"},
    )
    return (st == 200, txt)


def send_feishu(p):
    """**v3 修的 bug**：v2 发 msg_type=text，飞书不渲染 markdown，
    群里会看到一堆裸的 `## 🔴 今日待办` 和 `**动作**：`。
    改成 interactive 卡片 + lark_md 降级转换（见 _to_lark_md）。"""
    st, txt = _post(_env("FEISHU_WEBHOOK"), _feishu_card(p))
    return (st == 200 and '"StatusCode":0' in txt.replace(" ", ""), txt)


def send_dingtalk(p):
    hook = _env("DINGTALK_WEBHOOK")
    secret = _env("DINGTALK_SECRET")
    if secret:
        ts = str(round(time.time() * 1000))
        h = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
        hook = f"{hook}&timestamp={ts}&sign={urllib.parse.quote_plus(base64.b64encode(h))}"
    st, txt = _post(
        hook,
        {"msgtype": "markdown",
         "markdown": {"title": p.title[:60], "text": f"### {p.title}\n\n{p.body[:19000]}"}},
    )
    return (st == 200 and '"errcode":0' in txt.replace(" ", ""), txt)


# (显示名, 需要的环境变量, 发送函数)
# 邮件排在 Server酱 之后：横幅那一眼优先级最高，先把它发出去。
CHANNELS = [
    ("Server酱", ("SC_SENDKEY",), send_serverchan),
    ("邮件", ("SMTP_USER", "SMTP_PASS"), send_email),
    ("Bark", ("BARK_URL",), send_bark),
    ("飞书", ("FEISHU_WEBHOOK",), send_feishu),
    ("钉钉", ("DINGTALK_WEBHOOK",), send_dingtalk),
]


def push(p, dry_run=False):
    """返回 (配置了几个渠道, 成功了几个)。每个渠道**只发一次**。"""
    configured = succeeded = 0
    for name, envs, send in CHANNELS:
        if not all(_env(e) for e in envs):
            continue                       # 纯读环境变量，不碰网络
        configured += 1
        if dry_run:
            print(f"  {name}: [dry-run] 未发送")
            succeeded += 1
            continue
        for attempt in range(1, RETRIES + 1):
            try:
                ok, txt = send(p)
                if ok:
                    print(f"  {name}: ✅ {txt if name == '邮件' else ''}".rstrip())
                    succeeded += 1
                    break
                print(f"  {name}: ❌ 第{attempt}次 → {txt}")
            except FatalChannelError as e:
                print(f"  {name}: ❌ {e}（配置问题，不重试）")
                break
            except Exception as e:          # noqa: BLE001
                print(f"  {name}: ❌ 第{attempt}次 → {e}")
            if attempt < RETRIES:
                time.sleep(3 * attempt)
    return configured, succeeded


# ---------------------------------------------------------------- 自检

SAMPLE = (
    "# 低容量套利每日扫描 · 2026-08-13\r\n\r\n"
    "本金 **100,000** × 1 户　|　命中合计 **71** 条　|　今日待办 **3** 条\r\n\r\n"
    "## 🔴 今日待办\r\n"
    "- **[可转债打新]** N特宝转（`118074`）— 新债今日上市\r\n\r\n"
    "## 可转债打新（6 条）\r\n"
    "### 🔴今日 N特宝转（`118074`）\r\n"
    "- **动作**：新债今日上市\r\n\r\n"
    "## 转债退出提醒（4 条）\r\n"
    "### 🟠临近 嘉泽转债（`113039`）\r\n"
    "- **动作**：最后交易日 08-18（剩 3 个交易日）\r\n"
    "- `现价=159.8`\r\n\r\n"
    "## LOF/QDII折溢价（20 条）\r\n"
    "### ⚪观察 银华内需LOF（`161810`）\r\n"
    "- **动作**：折价 3.30% → 折价买入\r\n\r\n"
    "---\r\n### 数据源健康\r\n"
    "- 可转债打新：扫描 1048 行，命中 6 条 — ✅ OK\r\n"
    "- 事件套利：扫描 0 行 — ❌ 取数失败\r\n"
)

SAMPLE_HTML = (
    "<html><head><style>body{font-family:sans-serif}</style></head>"
    "<body><h1>低容量套利每日扫描 · 2026-08-13</h1>"
    "<table><tr><td>银华内需LOF</td><td>161810</td></tr></table>"
    "</body></html>"
)


def _selftest():
    """全程不联网。四组：解析（v1 起）、飞书转换（v3）、邮件（v3）、发送次数（v2 起）。"""
    global CHANNELS
    title, body = summarize(SAMPLE)
    p = Payload(title=title, body=body, md_full=SAMPLE, html=SAMPLE_HTML,
                stamp="20260813", code=0)

    checks = [
        # ---- 解析：v2 的 14 条，一条不动。summarize() 的输出必须逐字不变 ----
        ("标题取到日期", "08-13" in title),
        ("标题带待办数", "待办1" in title),
        ("标题带退出数", "退出1" in title),
        ("标题标出源异常", "源异常" in title),
        ("抬头进正文", "命中合计" in body),
        ("待办进正文", "N特宝转" in body),
        ("退出栏进正文", "嘉泽转债" in body and "08-18" in body),
        ("退出栏带公告告警", "动手前看公告" in body),
        ("异常源被点名", "事件套利" in body),
        ("健康的源不刷屏", "可转债打新：扫描" not in body),
        ("折溢价栏不推手机", "银华内需" not in body),
        ("说出没推的栏及条数", "LOF/QDII折溢价 20" in body),
        ("说清哪些没推", "手机只推" in body),
        ("指得出去哪看全的", "scan_20260813.html" in body),
    ]

    # ---- v3 ①：飞书 lark_md 转换。这一组在 v2 上全是红的 ----
    lk = _to_lark_md(body)
    card = _feishu_card(p)
    checks += [
        ("飞书|标题降级为粗体", "**🔴 今日待办**" in lk),
        ("飞书|不残留 # 标题", not re.search(r"^\s*#", lk, re.M)),
        ("飞书|不残留行内反引号", "`" not in lk),
        ("飞书|引用降级为 💡", "💡 本栏不区分强赎" in lk),
        ("飞书|保留粗体", "**动作**" in lk or "**100,000**" in lk),
        ("飞书|保留列表", "\n- " in lk),
        ("飞书|发卡片不发裸文本", card["msg_type"] == "interactive"),
        ("飞书|正文用 lark_md 标签",
         card["card"]["elements"][0]["text"]["tag"] == "lark_md"),
        ("飞书|卡片头随退出码变色",
         card["card"]["header"]["template"] == "green"
         and _feishu_card(p._replace(code=2))["card"]["header"]["template"] == "red"),
    ]

    # ---- v3 ②：邮件。核心断言是「邮件必须是全文，不能是摘要」 ----
    m = build_email(p, "me@126.com", "me@126.com")
    fnames = [x.get_filename() for x in m.iter_attachments() if x.get_filename()]
    plain = m.get_body(preferencelist=("plain",)).get_content()
    htm = m.get_body(preferencelist=("html",)).get_content()
    checks += [
        ("邮件|主题就是通知标题", m["Subject"] == title),
        ("邮件|主题不含换行", "\n" not in (m["Subject"] or "")),
        ("邮件|正文是全文不是摘要", "银华内需" in plain),
        ("邮件|摘要里没有的栏目在邮件里有", "LOF/QDII折溢价" in plain),
        ("邮件|HTML 进正文（不必点附件）", "<table>" in htm),
        ("邮件|两个附件都在",
         "scan_20260813.html" in fnames and "scan_20260813.md" in fnames),
        ("邮件|收发件人齐备", bool(m["From"]) and m["To"] == "me@126.com"),
        ("邮件|没有 HTML 时兜底不炸",
         "<pre" in build_email(p._replace(html=""), "a@126.com", "a@126.com")
         .get_body(preferencelist=("html",)).get_content()),
        ("邮件|126 推导出 smtp.126.com:465",
         _smtp_host_port("me@126.com") == ("smtp.126.com", 465)),
        ("邮件|环境变量可覆盖服务器",
         _smtp_host_port("me@126.com", "mail.corp.cn", "587") == ("mail.corp.cn", 587)),
    ]

    # ---- 「Server酱保留现状不动」的机器化表述 ----
    checks.append(("Server酱|推的还是摘要不是全文",
                   "银华内需" not in p.body and len(p.body) < len(p.md_full)))

    # ---- 发送次数：v2 的三条，签名改了但断言含义不变 ----
    calls = []
    saved, os.environ["FAKE_KEY"] = CHANNELS, "x"
    CHANNELS = [("假渠道", ("FAKE_KEY",), lambda q: (calls.append(1), (True, "ok"))[1])]
    try:
        cfg, ok = push(p)
    finally:
        CHANNELS = saved
        os.environ.pop("FAKE_KEY", None)
    checks.append(("成功时只发一次", len(calls) == 1))
    checks.append(("计数正确", (cfg, ok) == (1, 1)))

    calls2 = []
    saved = CHANNELS
    CHANNELS = [("没配的渠道", ("DEFINITELY_UNSET_XYZ",),
                 lambda q: (calls2.append(1), (True, "ok"))[1])]
    try:
        cfg2, _ = push(p)
    finally:
        CHANNELS = saved
    checks.append(("没配就不调用", len(calls2) == 0 and cfg2 == 0))

    # ---- 认证错误不重试（v3 新增；v2 会闷头重试三次）----
    tries = []

    def _auth_fail(_q):
        tries.append(1)
        raise FatalChannelError("授权码错")

    saved, os.environ["FAKE_KEY2"] = CHANNELS, "x"
    CHANNELS = [("假邮件", ("FAKE_KEY2",), _auth_fail)]
    try:
        push(p)
    finally:
        CHANNELS = saved
        os.environ.pop("FAKE_KEY2", None)
    checks.append(("认证失败只试一次", len(tries) == 1))

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'✅' if ok else '❌'} {n}")
    if bad:
        print(f"\n❌ 自检失败（{len(bad)}/{len(checks)}）：{bad}")
        return 2
    print(f"\n✅ 自检通过（{len(checks)} 条）")
    return 0


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=int, default=0, help="run.py 的退出码 0/1/2")
    ap.add_argument("--date", default=None, help="YYYYMMDD，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发送，不吃额度")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    stamp = args.date or date.today().strftime("%Y%m%d")
    fp = REPORT_DIR / f"scan_{stamp}.md"
    fp_html = REPORT_DIR / f"scan_{stamp}.html"
    icon, verdict = CODE_TEXT.get(args.code, ("❓", f"未知退出码 {args.code}"))

    md_full = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
    html = fp_html.read_text(encoding="utf-8", errors="replace") if fp_html.exists() else ""

    if args.code == 2 or not fp.exists():
        # 报告没生成 / 没参考价值 —— 照样推，而且要说清是哪一种。
        # 这一条是整个自动化里最重要的一推：静默才是最坏的结果。
        title = f"{icon} 扫描器 {stamp} 异常"
        why = verdict if args.code == 2 else f"**报告文件不存在**（{fp.name}）"
        body = (
            f"{why}\n\n"
            f"- run.py 退出码：`{args.code}`\n"
            f"- 到机器上跑 `py -3.11 diag_sources.py` 看是哪一路不通\n"
            f"- 多数情况是东财对连打全市场大表的限流，过几分钟重跑即可"
        )
    else:
        title, body = summarize(md_full)
        if args.code != 0:
            body = f"{icon} {verdict}\n\n{body}"
        title = f"{icon} {title}"

    p = Payload(title=title, body=body, md_full=md_full, html=html,
                stamp=stamp, code=args.code)

    print(f"[notify] {title}")
    if fp.exists() and not html:
        print(f"[notify] ⚠️ 没找到 {fp_html.name}，邮件正文会退化成纯文本 markdown")
    if args.dry_run:
        print("-" * 60)
        print(body)
        print("-" * 60)
        print(f"[dry-run] 邮件正文将是全文：markdown {len(md_full)} 字符 / "
              f"HTML {len(html)} 字符")
    n_cfg, n_ok = push(p, dry_run=args.dry_run)

    if n_cfg == 0:
        print("[notify] 一个渠道都没配置（SC_SENDKEY / SMTP_USER+SMTP_PASS / "
              "BARK_URL / FEISHU_WEBHOOK / DINGTALK_WEBHOOK）")
        return 2
    if n_ok == 0:
        print(f"[notify] {n_cfg} 个渠道全部失败 —— 不该关机")
        return 1
    if n_ok < n_cfg:
        # 部分成功仍返回 0（沿用 v2 语义，机器照常关机）。但要说出来：
        # 如果挂的是邮件，今天就没有全文可看，报告只留在盘上。
        print(f"[notify] ⚠️ {n_cfg - n_ok} 个渠道失败，报告仍在 reports/ 里")
    print(f"[notify] {n_ok}/{n_cfg} 个渠道推送成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
