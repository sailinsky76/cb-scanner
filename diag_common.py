"""所有 `diag_*.py` 共用的一点东西。**不含任何取数逻辑、不接进 `scanner/`。**

现在只有一件事：把输出流的编码修对。

为什么要单独一个文件 —— 这一条是**实打实付了代价才搬出来的**：

* v5.4 那轮，`py -3.11 diag_cbplan.py > docs/probes/probe5.txt` 在简中 Windows 上崩了：
  重定向时 stdout 走 locale 编码（GBK），而 `✓ / ✗ / ⚠` 在 GBK 里编不出来，
  **第一次成功取数就 `UnicodeEncodeError`**。不重定向时控制台是 UTF-8，所以一直没事。
* v5.5 那轮修了它，但**只修在 `diag_cbplan.py` 里**，另外五个探针没管；
  当时在 `HANDOFF.md` §9 写了一句「别的 `diag_*.py` 还没有这个修复，重定向时留意」。
* v5.6 这轮 `diag_sources.py` 就**照原样崩了一次**（`docs/probes/probe7.txt` 是现场：
  `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`，
  崩在第一张表取数成功之后），那次跑动整个白费，那个待查的 host 一个字都没答上。

**教训**：把坑写进文档只是「记住了」，把修复搬进公共模块才是「修好了」。
所以现在它在这里，六个探针一律 `from diag_common import apply_stream_fix` 然后
**在任何 print 之前**调一次。新写探针照抄这两行。
"""
from __future__ import annotations

import sys

__all__ = ["stream_fix_plan", "apply_stream_fix"]


def stream_fix_plan(encoding, is_tty: bool) -> tuple:
    """返回 `(要改成的编码 or None, errors 策略)`。**纯函数，改判定必须改这一个。**

    `errors` 一律 `backslashreplace`，**不许用** `replace`：
    后者会把 `✓` 和 `✗` 一起降级成 `?`，而这两个符号是探针里
    「取到 / 没取到」的唯一区分 —— 那等于把纪律 5 的头号敌人请进存档。
    """
    enc = (encoding or "").lower().replace("-", "").replace("_", "")
    if enc.startswith("utf8"):
        return (None, "backslashreplace")    # 已经是 UTF-8，一个字节都不动
    if is_tty:
        return (None, "backslashreplace")    # 控制台照它自己的编码走，只兜住异常
    return ("utf-8", "backslashreplace")     # 重定向进文件 → 强制 UTF-8


def apply_stream_fix() -> None:
    """把上面的判定落到 stdout/stderr 上。**兜不住就算了，不许让它自己把探针弄崩。**"""
    for stream in (sys.stdout, sys.stderr):
        rc = getattr(stream, "reconfigure", None)
        if rc is None:
            continue
        try:
            is_tty = bool(getattr(stream, "isatty", lambda: False)())
            enc, errs = stream_fix_plan(getattr(stream, "encoding", None), is_tty)
            rc(encoding=enc, errors=errs) if enc else rc(errors=errs)
        except Exception:
            pass
