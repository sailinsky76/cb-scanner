"""配置加载：内置默认值（DEFAULTS）与 config.yaml 深合并。

取舍：默认值写死在代码里，config.yaml 只写“要改的那几项”。
这样配置文件缺字段、写错缩进、甚至整个文件丢了，扫描器依然能跑起来——
定时任务里最难排查的故障就是“改了个配置，第二天早上静悄悄没跑”。

合并规则：
- dict 递归合并（用户只写 `fund_premium: {premium_alert_pct: 5}`，
  其余 discount_alert_pct / types / jisilu_cookie 仍取默认值）
- 其他类型（含 list）整体覆盖 —— `event_arb.keywords` 写了就是全量替换，
  而不是和默认关键词求并集，否则用户永远删不掉一个关键词
- 值为 None（YAML 里写了键但留空）视为“没写”，回落默认值
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path

log = logging.getLogger("cb_scanner")

# ------------------------- 内置默认值 -------------------------
# 改默认行为改这里。**这不是 config.yaml 的副本**：随包那份是「我这台机器的配置」，
# 会覆盖掉其中几项（v5.9.5 更正 —— 上一版这里写着「与随包的 config.yaml 保持一致」，
# 而两边的 capital 一直是 150000 vs 100000）。差异当前只有这一项，
# 后果是 config.yaml 丢了的时候报告抬头和组合口径会按 15 万算 ——
# 数没错，但那句「把 100,000 元铺满」是从配置里来的，别以为它是写死的。
DEFAULTS: dict = {
    "capital": 150000,          # 单账户可用资金(元)
    "accounts": 1,              # 参与打新的账户数
    "lookahead_days": 10,       # 打新/配债向前看多少天

    "sources": {                # 数据源开关
        "cb_ipo": True,
        "cb_allotment": True,
        "cb_redeem": True,
        "fund_premium": True,
        "event_arb": True,
        "cb_approved": True,
    },

    "cb_ipo": {
        "min_rating": "",             # 例 "AA-"；留空=不按评级过滤
        "min_convert_value": 0,       # 转股价值下限，0=不过滤
        "pay_window_trading_days": 2, # 缴款提醒提前几个**交易日**（不是日历天）
        "list_window_trading_days": 2, # 上市提醒提前几个**交易日**（v5.0）
                                       # 注意：min_rating / min_convert_value 是
                                       # “要不要申购”的门，不套在上市提醒上——
                                       # 债已经在手里了，不该因为评级低就不提醒你它今天上市
    },

    "cb_redeem": {
        # 【v5.1】转债退出提醒。这一栏只读「最后交易日」一列。
        "exit_window_trading_days": 5,   # 向前看几个**交易日**（不是日历天）
        "show_past": True,               # 已过最后交易日的还出不出条（观察档）
        "past_lookback_days": 10,        # 已过那一档往回看几天，避免翻出半年前的
        "max_unknown": 20,               # 「名单内但日期未取到」最多列几条（总数照说）
        "unknown_ratio_gate": 0.5,       # 「日期为空」占比 ≥ 这个数就当成常态：那一档
                                         # 不逐行印，只在栏目级说覆盖率。出处 docs/probes/probe2.txt
                                         # （319 行里 313 行为空 = 98%，空是常态不是异常）
    },

    "cb_approved": {
        # 【v5.9】转债获批公告。走巨潮公告检索（和 event_arb 同一张表）。
        "window_days": 180,           # 检索窗口（日历天）。实测 180 天约 22 只
        "max_items": 30,              # 最多列几只（0=不限），砍掉的条数照说
        "stale_days": 90,             # 获批满这么多天还没查到发行记录 → 标一句
        "hide_issued": False,         # 已发行的要不要从本栏隐去。默认 false=留着并标出来
        "keywords": ["同意注册", "注册批复", "证监会核准", "核准批复"],
        # 反向写法（不予注册/终止/中止/撤回/失效）**不做配置项** ——
        # 它是分类顺序的一部分，顺序反了方向就反了（见 sources/cb_approved.py）
    },

    "cb_allotment": {
        "max_prior_return_pct": 30,   # 正股“发债前三个月涨幅”上限，超过标红
        "prior_window_days": 95,      # 日历天≈3个月
        # v5.0：最小配售单位按债券代码前缀分市场（沪 11x=1手/1000元，深 12x=1张/100元），
        # 没做成配置项——那是**交易所规则**，不是偏好，写成可调的反而给了改错的机会
    },

    "fund_premium": {
        "premium_alert_pct": 3.0,
        "discount_alert_pct": 2.0,
        "only_cross_border": False,   # true=只看跨境标的
        "max_premium": 10,            # 溢价侧条数上限（0=不限）
        "max_discount": 10,           # 折价侧条数上限，两侧分开配额
        "redeem_fee_pct": 1.5,        # 赎回费假设(%)，1.5=持有<7日的监管下限
        "commission_pct": 0.03,       # 场内买入佣金(%)
        "min_turnover_wan": 100,      # 日成交额低于该值(万元)打流动性提示
        "turnover_participation_pct": 5,  # 单笔建仓最多占日成交额的百分比
        "max_position_pct": 30,       # 单只最多占本金的百分比
        "min_profit_yuan": 0,         # 预估收益低于该值(元)不出条，0=不过滤
        "demote_unknown_gate": True,  # 申赎状态取不到的条目是否往后排（计数与措辞不受它影响）
        "delist_line_wan": 1000,      # 场内资产净值退市线(万元)，2026-08 征求意见稿
        "spread_alert_pct": 0.5,      # 买卖价差超过该比例(%)打提示
        "only_redeemable": False,     # true=申赎暂停的品种整条不出
        "sanity_median_pct": 3.0,     # LOF 折溢价中位数偏离 0 超过该值 → 判净值口径不对
        "stale_days": 3,              # 数据日期落后几个交易日算陈旧
        "fetch_retries": 3,           # 行情接口失败重试次数（退避 2s/5s）
        "jisilu_cookie": "",          # 填了才拉 QDII 实时估值溢价
    },

    "event_arb": {
        "window_days": 7,
        "max_items": 20,
        "keywords": ["要约收购", "现金选择权", "换股吸收合并", "吸收合并"],
        # 走巨潮「基金」栏目，与上面的「沪深京」是两个库。对应 2026-08 的
        # LOF 退出机制征求意见稿：商品期货/QDII LOF 与小规模 LOF 的终止上市公告。
        "fund_keywords": ["终止上市", "基金合同终止", "投资者选择期"],
    },

    "output": {
        "formats": ["console", "markdown"],   # 可加 "html"
        "out_dir": "./reports",
    },
}

_VALID_FORMATS = {"console", "markdown", "html"}


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 合并进 base 的副本；dict 递归，其余整体覆盖。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if v is None:                       # YAML 里键留空 → 当作没写
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        log.warning("未安装 PyYAML，全部使用内置默认配置（pip install PyYAML）")
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:                  # 缩进写错 / 编码问题 —— 别让日报断供
        log.warning("config.yaml 解析失败（%s），改用内置默认配置", e)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        log.warning("config.yaml 顶层不是映射（得到 %s），改用内置默认配置", type(data).__name__)
        return {}
    return data


def _sanitize(cfg: dict) -> dict:
    """轻量校正：把明显写坏的字段拉回可用状态，并说明改了什么。"""
    fmts = cfg.get("output", {}).get("formats")
    if isinstance(fmts, str):               # formats: markdown（漏写列表）
        fmts = [fmts]
    if not isinstance(fmts, (list, tuple)) or not fmts:
        fmts = list(DEFAULTS["output"]["formats"])
    kept = [f for f in fmts if f in _VALID_FORMATS]
    dropped = [f for f in fmts if f not in _VALID_FORMATS]
    if dropped:
        log.warning("忽略无法识别的输出格式：%s（可选 %s）",
                    dropped, sorted(_VALID_FORMATS))
    cfg.setdefault("output", {})["formats"] = kept or list(DEFAULTS["output"]["formats"])

    # types 自 v2 起废弃（东财「类型」列是投资类型口径，不含 QDII/LOF 字样），
    # 这里只做兼容性归一，模块本身已不再读取它。
    types = cfg.get("fund_premium", {}).get("types")
    if isinstance(types, str):
        cfg["fund_premium"]["types"] = [types]

    # 两个关键词列表都要归一。漏掉 fund_keywords 的后果不是「不生效」而是更糟：
    # 下游 `list("终止上市")` 会拆成 ['终','止','上','市'] 四个单字关键词去打巨潮，
    # 每天多打四次无意义检索，而且单字命中会把日报冲垮。
    for key in ("keywords", "fund_keywords"):
        kws = cfg.get("event_arb", {}).get(key)
        if isinstance(kws, str):
            cfg["event_arb"][key] = [kws]
    # cb_approved.keywords 同一个坑：写成字符串会被 list() 拆成单字关键词
    kws = cfg.get("cb_approved", {}).get("keywords")
    if isinstance(kws, str):
        cfg["cb_approved"]["keywords"] = [kws]
    return cfg


def load_config(path: str = "config.yaml") -> dict:
    """读取配置文件并与内置默认值合并；文件不存在也能正常返回默认值。"""
    p = Path(path)
    if not p.is_absolute():
        # 允许从任意目录调用 run.py：先按 cwd 找，找不到再按项目根目录找
        if not p.exists():
            proj_root = Path(__file__).resolve().parent.parent
            if (proj_root / path).exists():
                p = proj_root / path

    if not p.exists():
        log.warning("未找到配置文件 %s，使用内置默认配置", path)
        return _sanitize(copy.deepcopy(DEFAULTS))

    user = _load_yaml(p)
    cfg = _sanitize(_deep_merge(DEFAULTS, user))
    log.info("已加载配置：%s", p)
    return cfg
