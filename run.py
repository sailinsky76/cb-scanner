#!/usr/bin/env python3
"""低容量套利每日扫描器 —— 主入口。

用法：
  python run.py                      # 实盘：联网抓取（需能访问东财/巨潮/集思录）
  python run.py --mock               # 离线自检：用内置样例走完整流程，验证逻辑与报告
  python run.py --format markdown html
  python run.py --date 2026-08-08    # 指定“今天”（回溯测试用）
  python run.py --config config.yaml

单个数据源失败不会中断整体；报告底部有“数据源健康”面板。

写到哪（v5.9.3）——判据是「这次产出能不能代表那一天的真实市场」：
  实盘 + 日期就是系统当天  → reports/scan_YYYYMMDD.md / .html
  --mock                    → reports/_scratch/scan_YYYYMMDD.mock.md
  --date 指到别的日子        → reports/_scratch/scan_YYYYMMDD.asof.md
上一版三种情况写同一个文件名，而 check.sh 每次都会跑一遍 --mock ——
于是「跑一次自检」＝「用编的数覆盖当天的真报告」，且毫无痕迹。

退出码（挂 cron / 任务计划时按这个判断）：
  0 = 六个源都完整取到数
  1 = 报告已生成，但有源取数失败或结果残缺 —— 报告只能当残缺件看
  2 = 所有源都失败，这份报告没有任何参考价值
加 --exit-zero 可强制永远返回 0（不想让调度器告警时用）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# 让脚本可从任意目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scanner.config import load_config          # noqa: E402
from scanner.report import render_console, render_html, render_markdown  # noqa: E402
from scanner.sources import (                    # noqa: E402
    CBAllotmentSource, CBApprovedSource, CBIpoSource, CBRedeemSource,
    EventArbSource, FundPremiumSource,
)
from scanner.sources.base import Context         # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cb_scanner")

_SOURCE_MAP = {
    "cb_ipo": CBIpoSource,
    "cb_allotment": CBAllotmentSource,
    "cb_redeem": CBRedeemSource,
    "fund_premium": FundPremiumSource,
    "event_arb": EventArbSource,
    # v5.9 新增，**排在最后**：它要用的 bond_zh_cov 已经被 cb_ipo 拉过并落了缓存，
    # 同一个 cache_key 直接命中，这一栏不多打一次网络请求。
    "cb_approved": CBApprovedSource,
}


def parse_args():
    p = argparse.ArgumentParser(description="低容量套利每日扫描器")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--mock", action="store_true", help="离线自检：用内置样例数据")
    p.add_argument("--date", default=None, help="覆盖今天，格式 YYYY-MM-DD")
    p.add_argument("--format", nargs="*", default=None,
                   help="输出格式，覆盖配置：console markdown html")
    p.add_argument("--exit-zero", action="store_true",
                   help="即使有数据源失败也返回退出码 0")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)
    if args.format:
        cfg.setdefault("output", {})["formats"] = args.format

    today = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today())
    ctx = Context(cfg=cfg, today=today, mock=args.mock)

    enabled = cfg.get("sources", {})
    results = []
    for name, cls in _SOURCE_MAP.items():
        if not enabled.get(name, True):
            continue
        log.info("运行数据源：%s%s", name, "（mock）" if args.mock else "")
        try:
            results.append(cls(ctx).fetch())
        except Exception as e:  # 双保险：即使源内部没兜住也不炸整体
            log.exception("数据源 %s 异常", name)
            # v5.9.3：这里原来另有一份手抄的 name→Kind 表。它只在这条兜底分支上用，
            # 所以「新增源忘了同步」不会在平时暴露 —— 会在**源真的抛异常那一天**
            # 暴露成 `KeyError: 'xxx'`，把整个 run 炸掉。双保险的第二层自己成了单点。
            # `cls.kind` 是同一个事实的第一手出处，不需要第二份。
            from scanner.models import SourceResult
            results.append(SourceResult(kind=cls.kind, error=f"未捕获异常：{e}"))

    formats = cfg.get("output", {}).get("formats", ["console"])
    out_dir = Path(cfg.get("output", {}).get("out_dir", "./reports"))
    stamp = today.strftime("%Y%m%d")

    # 报告级自检**每次都跑**，不再只在 --mock 下跑。
    # 上一版的门是 `if args.mock`，于是那五条不变量从来没在实盘输出上跑过一次 ——
    # 而它们要抓的正是实盘数据才会凑出来的组合（比如一条同时命中 滑点/流动性/
    # 盘口/退市线 → 4 句提示撞破预算）。mock 数据永远凑不出那种组合。
    # console 文本无论要不要打印都渲染一遍：只是拼字符串，代价可以忽略，
    # 换来的是 formats 里没写 console 时自检也不会被绕过。
    console_text = render_console(results, ctx)
    if "console" in formats:
        print("\n" + console_text)

    from verify_report import verify_console          # noqa: E402
    selfcheck_errs = verify_console(console_text)
    if selfcheck_errs:
        log.error("报告级自检未通过（%d 条）：", len(selfcheck_errs))
        for e in selfcheck_errs:
            log.error("  [x] %s", e)
        # --mock 是自检场景，不通过就是失败；实盘不能因此不给报告 ——
        # 报告照出、照写盘，但退出码降级，调度器能发现。
        if args.mock:
            return 2
    else:
        log.info("报告级自检通过（5 条不变量）")

    # ---- 写盘：**非实盘的产出不许占用实盘文件名**（v5.9.3）------------------
    # 上一版无论怎么跑都写 `reports/scan_{今天}.md`，而 `check.sh` 的第六项跑的正是
    # `run.py --mock --format console markdown html` —— 于是**每跑一次离线自检，
    # 当天那份实盘报告就被 mock 数据覆盖掉**。包里 08-09/10/11/12 四份归档全部
    # 是 mock 输出（满篇「示例转债」），而 STATE.md 还写着「reports/ 里的历史报告
    # 留着，它们是实盘凭据」—— 凭据早就没了，只是没人去 diff 过。
    # `cb_approved.py` 顶部那段注释引用的「实盘 08-12 那份里的盖世食品 920826」
    # 就是被这样盖掉的。
    #
    # `--date` 同一个口子且更隐蔽：它拿**今天**的实时数据、盖上一个过去的日期戳，
    # 然后覆盖那一天的归档。回溯测试销毁被回溯的那天的凭据，方向正好反了。
    #
    # 判据只有一条：**这次产出能不能代表 stamp 那一天的真实市场**。
    #   · 实盘 + 日期就是系统当天  → 能，写 reports/scan_{stamp}.md（行为一字未变）
    #   · --mock                    → 不能，数据是编的
    #   · --date 指到别的日子        → 不能，数据是今天的
    # 后两种一律进 `reports/_scratch/`，并在文件名里带上模式，互相之间也不覆盖。
    live = not args.mock and today == date.today()
    if live:
        write_dir, tag = out_dir, ""
    else:
        write_dir, tag = out_dir / "_scratch", (".mock" if args.mock else ".asof")

    written = []
    for fmt, ext, render in (("markdown", "md", render_markdown),
                             ("html", "html", render_html)):
        if fmt not in formats:
            continue
        write_dir.mkdir(parents=True, exist_ok=True)
        fp = write_dir / f"scan_{stamp}{tag}.{ext}"
        fp.write_text(render(results, ctx), encoding="utf-8")
        written.append(fp)

    for fp in written:
        log.info("已写出：%s", fp)
    if written and not live:
        log.info("以上不是实盘产出（%s），已写进 %s —— "
                 "reports/ 下的归档不会被覆盖",
                 "mock 数据" if args.mock else f"今天的数据盖了 {stamp} 的日期戳",
                 write_dir)

    # ---- 退出码：让调度器能发现「跑完了但数据是残的」---------------------
    # 原本无论多少个源挂掉都返回 0，挂成定时任务时这种半残输出静悄悄地过去了，
    # 而最危险的恰恰是它：报告看着正常，缺的那一栏被当成「今天没机会」。
    failed = [r for r in results if r.error]
    if failed:
        for r in failed:
            log.warning("数据源未完整取数 %s：%s", r.kind.value, r.error)
        log.warning("本次 %d/%d 个数据源异常，报告仅供残缺参考",
                    len(failed), len(results))

    if selfcheck_errs:
        log.warning("报告已生成，但报告级自检有 %d 条未通过 —— 数值/措辞存疑，"
                    "别照着操作", len(selfcheck_errs))

    if args.exit_zero:
        return 0
    if results and len(failed) == len(results):
        return 2
    return 1 if (failed or selfcheck_errs) else 0


if __name__ == "__main__":
    sys.exit(main())
