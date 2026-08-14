"""报告级自检：解析 render_console() 的最终文本，验证四条不变量。

这是上三轮修复缺的那道门——函数级自检只能抓住"想到了但写错了"，
而这四条是对**已经印出来的文字**做的，口径再变也自动跟着验。

不变量（①~④覆盖 v4.2→4.2.2 的全部三轮问题，⑤是 v4.4 加的）：
  ① 可投 × 净收益 = 预估         ← v4.2 那类（594≠595）
  ② 组合合计 = 贪心复算           ← v4.2.2 那类（1096≠1111）
  ③ 动作词方向 = 净收益符号       ← v4.2.1 那类（负收益写"折价买入"）
  ④ 同一条里不出现互相矛盾的指令  ← v4.2.1 那类（"已扣过"配"请重算"）
  ⑤ 行内提示不超预算              ← v4.3 那类（一条挂 5 句 326 字，读到第三句就跳过）

①~④ 管的是「印出来的数对不对」，⑤ 管的是「印出来的话有没有人会读」。
后者不是审美问题：写在没人读的位置上的正确提示，和没写是一回事。

用法：
  from verify_report import verify_console
  errors = verify_console(text)          # 返回空列表 = 通过
  assert not errors, "\\n".join(errors)

不依赖数据结构，只依赖渲染后的文本——这一层和代码逻辑完全解耦，
所以即便有新的渲染 bug 把数值印岔了，它也能抓住。
"""
from __future__ import annotations

import re
from typing import List

# ---- 解析器 ---------------------------------------------------------------
# console 格式每条的结构：
#   [ ]观察  名称（代码）
#       动作：...
#       形态: LOF | ... | 净收益(%): 1.98 | 可投(万): 3.0 | 预估(元): 594 | ...
#       [!] ...；...

_ITEM_RE = re.compile(
    r"(?:\[!]今日|\[~]临近|\[ ]观察)\s+(.+?)（(\w+)）"
)

_METRICS_KV_RE = re.compile(
    r"([\w/()%]+):\s*([^\s|]+)"
)


def _parse_items(text: str) -> list:
    """解析 console 文本为 [{name, code, action, metrics:{}, flags_text}, ...]。"""
    lines = text.split("\n")
    items, cur = [], None
    for line in lines:
        # 栏目分隔（▎栏目名 / ▎数据源健康 / ▎口径与假设）必须关掉当前条目。
        # 不关的话，下一栏抬头的 banner「⚠ 折价侧 6 条按可执行度排序…」会被
        # 记到**上一栏最后一条**的 flags_text 上 —— ④ 和新的 ⑤ 都读这个字段，
        # 于是一条 66 字的栏目说明会被当成某条转债的行内提示去判预算。
        if line.lstrip().startswith("▎"):
            if cur is not None:
                items.append(cur)
                cur = None
            continue
        m = _ITEM_RE.search(line)
        if m:
            if cur is not None:
                items.append(cur)
            cur = {"name": m.group(1).strip(), "code": m.group(2).strip(),
                   "action": "", "metrics": {}, "flags_text": ""}
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if stripped.startswith("动作："):
            cur["action"] = stripped[3:]
        elif stripped.startswith("[!] "):
            cur["flags_text"] = stripped[4:]
        elif "|" in stripped and ":" in stripped:
            # metrics 行：key: value | key: value | ...
            for km in _METRICS_KV_RE.finditer(stripped):
                cur["metrics"][km.group(1)] = km.group(2)
    if cur is not None:
        items.append(cur)
    return items


def _parse_portfolio_note(text: str):
    """从组合口径注释里取出印出来的合计金额。"""
    m = re.search(r"合计预估\s*([\d,]+)\s*元", text)
    return int(m.group(1).replace(",", "")) if m else None


def _to_float(s: str):
    """安全转浮点数。"""
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


# ---- 四条不变量 -----------------------------------------------------------

def _check_multiply(items: list) -> List[str]:
    """① 可投 × 净收益 = 预估。"""
    errs = []
    for it in items:
        m = it["metrics"]
        if "预估(元)" not in m:
            continue
        wan = _to_float(m.get("可投(万)", ""))
        net = _to_float(m.get("净收益(%)", ""))
        est = _to_float(m.get("预估(元)", ""))
        if wan is None or net is None or est is None:
            continue
        expect = int(round(wan * 1e4 * net / 100))
        if expect != int(est):
            errs.append(
                f"[①乘法] {it['name']}({it['code']}): "
                f"可投 {wan}万 × 净收益 {net}% = {expect}，报告印的是 {int(est)}")
    return errs


def _check_portfolio(text: str, items: list) -> List[str]:
    """② 组合合计 = 按印出来的值贪心复算。"""
    printed = _parse_portfolio_note(text)
    if printed is None:
        return []  # 没有组合口径注释（可能全无折价条目），跳过

    # 从 text 解析本金
    cap_m = re.search(r"本金\s*([\d,]+)\s*×", text)
    if not cap_m:
        return ["[②合计] 无法解析本金"]
    capital = int(cap_m.group(1).replace(",", ""))

    # 按印出来的次序贪心铺满
    left, total = float(capital), 0.0
    for it in items:
        m = it["metrics"]
        if "预估(元)" not in m or "可投(万)" not in m or "净收益(%)" not in m:
            continue
        est_val = _to_float(m["预估(元)"])
        if est_val is None or est_val <= 0:
            continue
        if left <= 0:
            break
        full = _to_float(m["可投(万)"]) * 1e4
        net = _to_float(m["净收益(%)"])
        alloc = min(full, left)
        total += alloc * net / 100
        left -= alloc

    recalc = int(round(total))
    if recalc != printed:
        return [f"[②合计] 组合合计印的是 {printed} 元，按印出来的数贪心复算得 {recalc} 元"]
    return []


def _check_action_sign(items: list) -> List[str]:
    """③ 动作词方向 = 净收益符号。"""
    errs = []
    for it in items:
        m = it["metrics"]
        net = _to_float(m.get("净收益(%)", ""))
        if net is None:
            continue
        action = it["action"]
        # 净收益 ≤ 0 时不该说"折价买入"
        if net <= 0 and "折价买入" in action:
            errs.append(
                f"[③动作词] {it['name']}({it['code']}): "
                f"净收益 {net}% 但动作写了「折价买入」")
        # 净收益 > 0 时不该说"兑现不划算"
        if net > 0 and "兑现不划算" in action:
            errs.append(
                f"[③动作词] {it['name']}({it['code']}): "
                f"净收益 {net}% 但动作写了「兑现不划算」")
    return errs


def _check_contradictions(items: list) -> List[str]:
    """④ 同一条里不出现互相矛盾的指令。"""
    errs = []
    _PAIRS = [
        # (A 片段, B 片段, 描述)：A 和 B 不能同时出现在同一条的 flags 里
        ("已经", "按你真正买到的价格重算",
         "「已扣过」和「请重算」矛盾——要么让读者再扣一遍，要么说已经扣过"),
        ("已经", "折价要按你真正成交的价格重算",
         "「已折进」和「请重算」矛盾"),
    ]
    for it in items:
        ft = it["flags_text"]
        if not ft:
            continue
        for a, b, desc in _PAIRS:
            if a in ft and b in ft:
                errs.append(f"[④矛盾] {it['name']}({it['code']}): {desc}")
    return errs


def _parse_banners(text: str) -> list:
    """栏目抬头的 [!] 行（跟在 ▎栏目 后面、还没出现任何条目时的那些）。"""
    out, in_head = [], False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("▎"):
            in_head = True
            continue
        if _ITEM_RE.search(line):
            in_head = False
            continue
        if in_head and s.startswith("[!] "):
            out.append(s[4:])
    return out


# 预算：这三个数是**刻意定死**的，改它们就是在给自己开口子。
MAX_FLAGS_PER_ITEM = 3      # 一条最多几句行内提示
MAX_FLAG_CHARS = 60         # 单句上限
MAX_FLAGS_CHARS_TOTAL = 150  # 一条的提示合计上限
MAX_BANNER_CHARS = 160      # 单条栏目说明上限


def _check_budget(text: str, items: list) -> List[str]:
    """⑤ 提示预算：行内提示不许再长回去。

    为什么要有这条：v4.1→v4.3 每一轮都往提示里加话，没有一轮往外减 ——
    到 v4.3 时最差一条挂着 5 句 326 字，读到第三句人就跳过了，
    等于后面两句白写。但「写短点」是句没有约束力的话，下一轮照样会涨回来。

    这条把它变成机械约束：想加第 4 句，先删一句。预算不是审美偏好，
    是**注意力是有限的**这件事在代码里的表示。

    单句也卡 60 字，堵的是「把三句用顿号缝成一句」这种绕法；
    含「；」的单句直接按多句算，同理。
    """
    errs = []
    for it in items:
        ft = it["flags_text"]
        if not ft:
            continue
        parts = [p for p in ft.split("；") if p.strip()]
        if len(parts) > MAX_FLAGS_PER_ITEM:
            errs.append(
                f"[⑤预算] {it['name']}({it['code']}): 行内提示 {len(parts)} 句 > "
                f"{MAX_FLAGS_PER_ITEM} 句 —— 要加就得先删一句，或把常量说明挪进 footnotes")
        if len(ft) > MAX_FLAGS_CHARS_TOTAL:
            errs.append(
                f"[⑤预算] {it['name']}({it['code']}): 行内提示合计 {len(ft)} 字 > "
                f"{MAX_FLAGS_CHARS_TOTAL} 字")
        for p in parts:
            if len(p) > MAX_FLAG_CHARS:
                errs.append(
                    f"[⑤预算] {it['name']}({it['code']}): 单句 {len(p)} 字 > "
                    f"{MAX_FLAG_CHARS} 字 —— 「{p[:24]}…」"
                    "（口径解释归 footnotes，行内只留这一条独有的数）")
    for b in _parse_banners(text):
        if len(b) > MAX_BANNER_CHARS:
            errs.append(
                f"[⑤预算] 栏目说明 {len(b)} 字 > {MAX_BANNER_CHARS} 字 —— "
                f"「{b[:24]}…」（每天都一样的那部分归 footnotes）")
    return errs


# ---- 公开接口 -------------------------------------------------------------

def verify_console(text: str) -> List[str]:
    """解析 render_console 输出，返回全部不通过的不变量描述。空列表 = 全部通过。"""
    items = _parse_items(text)
    errs = []
    errs.extend(_check_multiply(items))
    errs.extend(_check_portfolio(text, items))
    errs.extend(_check_action_sign(items))
    errs.extend(_check_contradictions(items))
    errs.extend(_check_budget(text, items))
    return errs


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python verify_report.py <console_output.txt>")
        print("      或在 run.py --mock 时自动调用")
        sys.exit(0)
    txt = open(sys.argv[1], encoding="utf-8").read()
    errs = verify_console(txt)
    if errs:
        print(f"报告级自检未通过（{len(errs)} 条）：")
        for e in errs:
            print(f"  [x] {e}")
        sys.exit(1)
    else:
        print("报告级自检通过。")
