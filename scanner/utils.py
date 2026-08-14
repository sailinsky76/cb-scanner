"""通用工具：安全调用、当日磁盘缓存、日期/数值解析、文本与链接清洗。

四条约定，都是被线上坑出来的：

1) **判空必须排在类型判断前面**。`pd.NaT` 是 `datetime` 的子类实例，
   `isinstance(NaT, datetime)` 为 True、`NaT.date()` 返回的还是 `NaT`。
   所以 `parse_date` 里一旦先写 `isinstance(v, datetime)`，NaT 就会被当成
   合法日期放行，下游 `start <= NaT <= end` 直接抛 TypeError。
   （`bond_zh_cov` 里未排配债计划的转债，「原股东配售-股权登记日」就是 NaT，
   配债源必挂 —— 这就是当初那个报错的根因。）

2) **8 位整数不能直接丢给 `pd.to_datetime`**。akshare 偶尔返回 `20260812`
   这种 int，`pd.to_datetime(20260812)` 会按纳秒时间戳解析成 1970-01-01。
   必须先识别成 `%Y%m%d` 字符串再解析。

3) **`within()` 自带类型兜底**。调用方可能把任何东西塞进来，这里统一
   归一到 `date`，归一不了就返回 False —— 单条脏数据不该让整个源挂掉。

4) **safe_call 只吞 `Exception`**，不吞 `KeyboardInterrupt` / `SystemExit`，
   Ctrl-C 仍然能立刻停下来。

5) **safe_call 必须能报出真名**。调用点普遍写成 `safe_call(lambda: disk_cache(...))`，
   于是日志里全是「调用 <lambda> 失败」，出事时根本判断不出是哪个接口挂了。
   现在 `_fn_name()` 会穿透 lambda / functools.partial 找到真实函数名，
   实在找不到就用调用方显式传的 `_label`。

6) **空结果不进缓存**。`disk_cache` 原本会把空 DataFrame 也落盘 900 秒，
   于是「重试」在空表这条路上是假的 —— 第二次直接命中缓存拿回同一张空表。
   现在空结果不写盘，且 `refresh=True` 可强制绕开读缓存（重试用）。

7) **交易日 ≠ 日历日**。缴款窗口原本写成 `today + timedelta(days=2)`，
   周五跑覆盖到周日，下周一的缴款日落在窗口外 —— 而缴款是这套流程里唯一的
   硬性风险。`shift_trading_days()` 改成交易日口径，拿不到交易日历时回落「跳周末」。

8) **回落的方向是「偏短」，不是「偏长」**（v5.9.2 更正，上一版这句写反了）。
   跳周末把节假日**当成了交易日** → 更快数满 n 个 → 终点更早 → 窗口更短。
   而窗口短的代价是漏掉一只债的最后交易日，正是本项目的头号敌人。

   更要紧的是它**不需要「日历拿不到」才触发**：`TradeCalendar.is_trading` 对
   超出覆盖范围的日期走同一条回落，而新浪日历只排到当年年底。拿包里那份
   （8797 行，最晚 2026-12-31）实测：

       2026-12-30 缴款窗口 +2 交易日 → 算出 2027-01-01（元旦当天），实际应为 01-04
       2026-12-29 退出窗口 +5 交易日 → 算出 2027-01-05，        实际应为 01-06

   所以窗口类计算改走 `trading_window_end()`：它返回 (终点, 是否被日历担保)，
   没担保时多开几个工作日，并让调用方在**栏目级**说出来。
   `shift_trading_days()` 的语义不变（「第 n 个交易日是哪天」），别把边际塞进它。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import html as _html
import logging
import math
import os
import pickle
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

try:                      # pandas 是硬依赖，但缺了也不该让 utils 无法导入
    import pandas as pd
except ImportError:       # pragma: no cover
    pd = None             # type: ignore[assignment]

log = logging.getLogger("cb_scanner")

__all__ = [
    "safe_call", "retry_call", "disk_cache", "clear_cache",
    "parse_date", "fmt_date", "within", "days_ago", "to_float",
    "load_trade_calendar", "shift_trading_days", "trading_window_end",
    "WINDOW_UNKNOWN_MARGIN",
    "trading_days_between",
    "strip_html", "clean_url",
]

# 缓存目录：项目根/.cache（与 scanner/ 同级）
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

# 各家接口表示"空"的写法，统一在这里收口
_NULLISH = {
    "", "-", "--", "---", "----", "—", "——", "――", "/", "//",
    "nan", "NaN", "NAN", "none", "None",
    "null", "NULL", "NaT", "nat", "N/A", "n/a", "NA", "暂无", "无", "--%",
}


# ============================ 安全调用 ============================
def _fn_name(fn: Any, _depth: int = 0) -> str:
    """尽力拿到可读的函数名：穿透 lambda 与 functools.partial。

    `safe_call(lambda: disk_cache(key, ak.fund_lof_spot_em))` 这种写法很常见，
    直接 `fn.__name__` 只会得到 '<lambda>'，日志就废了 —— 线上那两条
    「调用 <lambda> 失败」就是这么来的，光看日志根本判断不出挂的是哪个接口。

    优先级：真名 → partial 的 .func → lambda 闭包里捕获的可调用对象 →
    lambda 字节码引用过的名字。
    """
    if _depth > 4:                              # 自引用闭包的守卫
        return "<callable>"

    for attr in ("__qualname__", "__name__"):
        n = getattr(fn, attr, None)
        if n and "<lambda>" not in str(n):
            return str(n)

    inner = getattr(fn, "func", None)           # functools.partial
    if inner is not None and inner is not fn:
        return _fn_name(inner, _depth + 1)

    # 走到这里就是个裸 lambda，两条兜底路线：
    # ① 闭包捕获的函数对象（`fn = ak.xxx; lambda: fn()`）
    for cell in (getattr(fn, "__closure__", None) or ()):
        try:
            v = cell.cell_contents
        except ValueError:                      # 尚未绑定的 cell
            continue
        if callable(v) and v is not fn:
            return f"<lambda→{_fn_name(v, _depth + 1)}>"

    # ② 字节码里引用过的名字（`lambda: ak.fund_lof_spot_em()` → 'fund_lof_spot_em'）
    code = getattr(fn, "__code__", None)
    names = [n for n in (getattr(code, "co_names", ()) or ()) if not n.startswith("_")]
    if names:
        return f"<lambda→{'.'.join(names[-2:])}>"

    return str(getattr(fn, "__name__", None) or repr(fn))


def _is_empty_result(v: Any) -> bool:
    """判断一次调用的返回值是否「等于没拿到」。

    全市场行情表返回空 = 接口异常，不是「今天没数据」；这类结果既不该进缓存，
    也应该触发重试。标量 / 数字不在此列，一律视为有效。
    """
    if v is None:
        return True
    if pd is not None and isinstance(v, pd.DataFrame):
        return v.empty
    if isinstance(v, (list, tuple, dict, set, str, bytes)):
        return len(v) == 0
    return False


def safe_call(fn: Callable, *args, _label: Optional[str] = None,
              **kwargs) -> Tuple[Optional[Any], Optional[str]]:
    """调用 fn，成功返回 (结果, None)，失败返回 (None, 错误描述)。

    约定所有 Source.fetch() 用它包住网络调用，把失败写进 SourceResult.error，
    而不是往上抛 —— 单个源挂掉不该让整份日报消失。

    `_label` 是仅供日志使用的显示名（关键字参数，不会转发给 fn）。包了 lambda
    的调用点建议显式传，比如 `_label="fund_lof_spot_em(LOF)"`。
    """
    try:
        return fn(*args, **kwargs), None
    except Exception as e:                      # 不含 KeyboardInterrupt/SystemExit
        msg = f"{type(e).__name__}: {e}"
        log.warning("调用 %s 失败：%s", _label or _fn_name(fn), msg)
        return None, msg


def retry_call(fn: Callable, label: Optional[str] = None, attempts: int = 3,
               backoff: Tuple[float, ...] = (2.0, 5.0),
               cache_key: Optional[str] = None, ttl_seconds: int = 3600,
               reject_empty: bool = True) -> Tuple[Optional[Any], Optional[str]]:
    """带退避重试的安全调用，返回 (结果, 最后一次错误描述)。

    fn 必须是无参可调用（要传参用 functools.partial 包一层，`_fn_name` 认得它）。

    两个刻意的设计：

    - **第 2 次起自动 `refresh=True`**。否则重试会命中上一次刚落盘的坏缓存，
      «重试» 变成纯粹的空转。
    - **`reject_empty` 要按接口语义配**。全市场行情表空 = 异常，该重试（True）；
      关键词检索无命中是正常结果，不能重试（False），否则每个冷门关键词都要
      白白多打两次请求。
    """
    name = label or _fn_name(fn)
    attempts = max(1, int(attempts or 1))
    last_err: Optional[str] = None

    for i in range(1, attempts + 1):
        if cache_key:
            def call(_i=i):
                return disk_cache(cache_key, fn, ttl_seconds=ttl_seconds, refresh=(_i > 1))
        else:
            call = fn

        value, err = safe_call(call, _label=name)
        if err is None and not (reject_empty and _is_empty_result(value)):
            if i > 1:
                log.info("%s 第 %d 次尝试成功", name, i)
            return value, None

        last_err = err or "空结果（接口返回空表）"
        if i < attempts:
            delay = backoff[min(i - 1, len(backoff) - 1)] if backoff else 0.0
            log.info("重试 %s（第 %d/%d 次），%.1fs 后…", name, i + 1, attempts, delay)
            if delay > 0:
                time.sleep(delay)

    return None, last_err


# ============================ 磁盘缓存 ============================
def _cache_path(key: str) -> Path:
    """文件名 = 可读前缀 + key 的 md5，方便直接 ls .cache 看命中了什么。"""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    prefix = re.sub(r"[^0-9A-Za-z_.\-]", "_", key)[:60]
    return CACHE_DIR / f"{prefix}__{digest}.pkl"


def disk_cache(key: str, fn: Callable[[], Any], ttl_seconds: int = 3600,
               refresh: bool = False, cache_empty: bool = False) -> Any:
    """当日缓存：命中且未过期就读盘，否则调用 fn() 并落盘。

    同一次运行里 cb_ipo 和 cb_allotment 都要 `bond_zh_cov()`，用同一个 key
    可省掉一次拉取。**不做"过期后回退旧缓存"**——宁可这次报错，也不能拿昨天
    的表当今天的申购日历，那会直接漏掉缴款提醒。

    `refresh=True` 跳过读缓存（仍然写），重试时用；否则第二次尝试会直接命中
    第一次刚落盘的结果，重试等于没做。

    `cache_empty=False`（默认）时空结果不落盘：全市场行情表返回空表是接口异常，
    把它缓存 15 分钟等于把故障固化，期间怎么重跑都是空的。
    """
    path = _cache_path(key)
    if not refresh:
        try:
            if path.exists() and (time.time() - path.stat().st_mtime) < ttl_seconds:
                with path.open("rb") as f:
                    data = pickle.load(f)
                log.debug("缓存命中：%s", key)
                return data
        except Exception as e:                  # 缓存损坏就当没有，重新取
            log.debug("缓存读取失败（忽略）：%s -> %s", key, e)

    value = fn()

    if not cache_empty and _is_empty_result(value):
        log.debug("空结果不写缓存：%s", key)
        return value

    try:                                        # 写临时文件再 rename，避免半截 pickle
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        with tmp.open("wb") as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception as e:
        log.debug("缓存写入失败（忽略）：%s -> %s", key, e)
    return value


def clear_cache() -> int:
    """清空缓存目录，返回删除的文件数（接口改版、数据看着不对时手动调用）。"""
    n = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.pkl"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


# ============================ 空值判定 ============================
def _is_null(v: Any) -> bool:
    """统一判空：None / NaT / NaN / 各种"空"字符串。**必须在类型判断之前调用。**"""
    if v is None:
        return True
    if pd is not None and v is pd.NaT:          # NaT 是单例，用 is 判最稳
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if isinstance(v, str):
        return v.strip() in _NULLISH
    if pd is not None:
        try:
            res = pd.isna(v)
            if isinstance(res, bool):
                return res
            return bool(res) if getattr(res, "ndim", 0) == 0 else False
        except (TypeError, ValueError):         # 数组/列表 → 不当作空值
            pass
    return False


def _as_date(v: Any) -> Optional[_dt.date]:
    """把 datetime / Timestamp / date 归一成 date；不是日期就返回 None。

    顺序不能反：datetime 是 date 的子类，先判 datetime 才能拿到 .date()。
    """
    if _is_null(v):
        return None
    if isinstance(v, _dt.datetime):             # 含 pd.Timestamp
        try:
            return v.date()
        except Exception:
            return None
    if isinstance(v, _dt.date):
        return v
    return None


# ============================ 日期解析 ============================
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
    "%Y年%m月%d日", "%y-%m-%d", "%y/%m/%d",
)
_YYYYMMDD_RE = re.compile(r"^\d{8}$")


def parse_date(v: Any) -> Optional[_dt.date]:
    """把 akshare 各种日期写法解析成 `datetime.date`，解析不了返回 None。

    支持：date / datetime / pd.Timestamp / '2026-08-12' / '2026/08/12' /
          '2026年8月12日' / '20260812' / 20260812(int) / '2026-08-12 09:30:00'。
    NaT / NaN / '-' / '' / 'NaT' 一律 → None。
    """
    # ① 判空排第一，NaT 在这里就被拦下（这是当初 bug 的修复点）
    if _is_null(v):
        return None

    # ② 已经是日期类型
    d = _as_date(v)
    if d is not None:
        return d

    # ③ 数值：只认 8 位的 YYYYMMDD，其余不猜（避免被当成纳秒时间戳）
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            iv = int(v)
        except (TypeError, ValueError, OverflowError):
            return None
        if 19000101 <= iv <= 21001231:
            try:
                return _dt.datetime.strptime(str(iv), "%Y%m%d").date()
            except ValueError:
                return None
        return None

    # ④ 字符串
    s = str(v).strip()
    if s in _NULLISH:
        return None
    s = s.replace("T", " ").split(" ")[0].strip()      # 丢掉时间部分
    if not s or s in _NULLISH:
        return None

    for fmt in _DATE_FORMATS:
        if fmt == "%Y%m%d" and not _YYYYMMDD_RE.match(s):
            continue                                   # 别让 '2026-08' 误配
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    if pd is not None:                                  # 兜底交给 pandas（字符串是安全的）
        try:
            ts = pd.to_datetime(s, errors="coerce")
            if ts is not pd.NaT and not _is_null(ts):
                return ts.date()
        except Exception:
            pass
    return None


def fmt_date(d: Any, fmt: str = "%m-%d", empty: str = "—") -> str:
    """日期 → 展示串（默认 MM-DD）；空值返回占位符而不是 'None'。"""
    dd = _as_date(d) if not isinstance(d, str) else parse_date(d)
    return dd.strftime(fmt) if dd else empty


def within(d: Any, start: Any, end: Any) -> bool:
    """d 是否落在 [start, end] 闭区间内。

    三个参数都先归一到 date；任何一个归一不了（None / NaT / 脏字符串）→ False。
    这一层兜底是刻意的：调用方拿到的是接口原始值，不该在这里炸掉整个数据源。
    """
    dd, s, e = _as_date(d), _as_date(start), _as_date(end)
    if dd is None or s is None or e is None:
        return False
    if s > e:                                   # 传反了也别静默给 False
        s, e = e, s
    return s <= dd <= e


def days_ago(n: int, today: Optional[_dt.date] = None) -> _dt.date:
    """today 往前 n 天（n 为负按 0 处理）。today 缺省取系统当天。"""
    base = _as_date(today) or _dt.date.today()
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return base - _dt.timedelta(days=max(0, n))


# ============================ 交易日历 ============================
class TradeCalendar:
    """交易日集合的薄封装，关键在于**覆盖范围之外要老实回落**。

    新浪交易日历通常只排到当年年底。如果直接拿 `d in days` 判断，越界的未来
    日期会全部被判成非交易日，`shift_trading_days` 就会一路往后走到守卫上限，
    返回一个几百天后的日期 —— 那比不用日历还危险。所以越界一律回落到跳周末。
    """

    def __init__(self, days):
        self.days = set(days or ())
        self.min = min(self.days) if self.days else None
        self.max = max(self.days) if self.days else None

    def __bool__(self) -> bool:
        return bool(self.days)

    def covers(self, d: _dt.date) -> bool:
        """这一天在不在日历的覆盖范围内 —— 即 `is_trading(d)` 是查出来的还是猜的。

        新浪日历只排到当年年底，所以每年 12 月下旬开始，跨年的窗口就有一段是猜的，
        而猜的那一段恰好和元旦/春节重合。分不出「查到的」和「猜的」，就没法在
        栏目级说清这件事 —— 于是窗口静默缩短，长得和「近期没有债要退出」一样。
        """
        return bool(self.days) and self.min is not None and self.min <= d <= self.max

    def is_trading(self, d: _dt.date) -> bool:
        if not self.covers(d):
            return d.weekday() < 5
        return d in self.days


_TRADE_CAL: dict = {"loaded": False, "cal": None}


def load_trade_calendar(ttl_seconds: int = 7 * 24 * 3600) -> Optional["TradeCalendar"]:
    """加载 A 股交易日历（akshare 新浪源），失败返回 None。

    进程内只尝试一次：拿不到就一直用回落逻辑，不要每个数据源都去重试一遍。
    """
    if _TRADE_CAL["loaded"]:
        return _TRADE_CAL["cal"]
    _TRADE_CAL["loaded"] = True

    cal = None
    try:
        import akshare as ak
    except ImportError:
        ak = None
    fn = getattr(ak, "tool_trade_date_hist_sina", None) if ak else None
    if fn is not None:
        df, _ = retry_call(fn, label="tool_trade_date_hist_sina", attempts=2,
                           backoff=(2.0,), cache_key="trade_calendar",
                           ttl_seconds=ttl_seconds)
        if df is not None and not getattr(df, "empty", True):
            col = df.columns[0]
            got = {d for d in (parse_date(v) for v in df[col]) if d}
            if got:
                cal = TradeCalendar(got)

    if cal is None:
        log.info("交易日历不可用，交易日窗口回落为「跳周末」估算"
                 "（节假日会被当成交易日 → 窗口**偏短**，长假前可能少提醒一天；"
                 "窗口类计算走 trading_window_end() 会自动补边际并在栏目级说明）")
    _TRADE_CAL["cal"] = cal
    return cal


def _is_trading_day(d: _dt.date, cal: Optional["TradeCalendar"]) -> bool:
    return cal.is_trading(d) if cal else d.weekday() < 5


def shift_trading_days(d: Any, n: int, cal: Optional["TradeCalendar"] = None) -> _dt.date:
    """d 之后的第 n 个交易日（n<=0 原样返回 d）。

    注意语义：只数 d **之后**的日子，d 本身是不是交易日不影响计数。
    周六 + 2 个交易日 = 下周二，所以 [今天, 今天+2交易日] 能盖住周一。

    **当前 `scanner/` 里没有调用点**（v5.9.2 起窗口类计算全部改走
    `trading_window_end`），只有 `tests_utils` / `selftest_fixes` 在验它。
    留着是刻意的：它钉住「第 n 个交易日是哪天」这个**不含安全边际**的语义，
    是 `trading_window_end` 那个偏保守口径的对照物。改它不会影响任何报告 ——
    但也别因此以为它没人管，那两处断言会红。
    """
    base = _as_date(d) or _dt.date.today()
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return base

    cur, left, guard = base, n, 0
    while left > 0 and guard < 400:             # 守卫：日历再怎么坏也不该走出 400 天
        cur += _dt.timedelta(days=1)
        guard += 1
        if _is_trading_day(cur, cal):
            left -= 1
    if left > 0:                                # 撞上守卫 = 日历坏了，别静默返回一个假日期
        log.warning("shift_trading_days 撞上 400 天守卫（还差 %d 个交易日）—— "
                    "交易日历多半是坏的，返回值不可信", left)
    return cur


# 日历担保不到的那一段，多开几个工作日的边际。
# 3 是按最长的一段「连续工作日假期」定的：元旦最多吃掉 3 个工作日，春节/国庆
# 通常 3-5 个但两侧都有调休上班日把它拆开。这个数只影响**没担保**的那一段，
# 平时（日历盖得住）一天都不多开。宁可多提醒一天，不可以少提醒一天。
#
# v5.9.3 起这是个**公开名**：`cb_ipo` / `cb_redeem` 的栏目级说明原来把
# 「已多开 3 个工作日」这个 3 写死在字符串里，改这个常量（或调用方传
# `unknown_margin=`）报告就会印错数 —— 说的和印的不是同一件事，正是纪律 5 拦的。
# 两个源现在从这里取值插进措辞。旧名保留成别名，外部若有引用不至于断。
WINDOW_UNKNOWN_MARGIN = 3
_WINDOW_UNKNOWN_MARGIN = WINDOW_UNKNOWN_MARGIN      # 兼容旧名


def trading_window_end(d: Any, n: int, cal: Optional["TradeCalendar"] = None,
                       unknown_margin: int = WINDOW_UNKNOWN_MARGIN
                       ) -> Tuple[_dt.date, bool]:
    """提醒窗口 [d, 终点] 的终点，外加「这一段是否被交易日历担保」。

    和 `shift_trading_days` 的分工：
      · `shift_trading_days(d, n)` 回答「d 之后的第 n 个交易日是哪天」—— 一个事实，
        语义里不该掺任何安全边际，所以它一个字都没改。
      · `trading_window_end(d, n)` 回答「要盖住未来 n 个交易日，窗口该开到哪天」——
        一个**判断**，方向上必须偏保守。

    为什么非要分开：日历覆盖不到的那一段（新浪只排到当年年底，或整个日历没取到）
    回落成「跳周末」，把节假日当成了交易日 → 终点偏早 → 窗口偏短。而这一栏漏掉
    一只债的最后交易日 = 那只债卖不掉了。所以没担保时多开 `unknown_margin` 个
    工作日，并把 False 一并返回，让调用方在**栏目级**说出来 —— 多出来的那几条
    要有解释，不然读者不知道为什么今天忽然多了两只。

    **边际只在「日历在、但够不着」时补**，整个日历都没取到时一天都不多开。
    两种降级不是一回事：
      · 日历整个没取到 —— 这一路已经在日志和**栏目级**各说了一遍，整份报告本来
        就是降级件；而且这种机器多半一直没网，天天多开会让窗口宽度取决于网络状态，
        自检也跟着不可复现（`_run_source` 走的就是 mock=False 这条路）。
      · 日历在、只是排到年底 —— 报告看起来**完全健康**，只有跨年那一小段是猜的。
        这才是静默的那一种，必须靠边际兜住。

    `unknown_margin=0` 可以关掉边际（`--mock` 用它，自检要可复现，不演日历降级）。
    """
    base = _as_date(d) or _dt.date.today()
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return base, True

    cur, left, guard, sure = base, n, 0, True
    while left > 0 and guard < 400:
        cur += _dt.timedelta(days=1)
        guard += 1
        if cal is None or not cal.covers(cur):
            sure = False
        if _is_trading_day(cur, cal):
            left -= 1
    if left > 0:
        log.warning("trading_window_end 撞上 400 天守卫（还差 %d 个交易日）", left)
        return cur, False

    if not sure and cal is not None and unknown_margin > 0:
        extra = int(unknown_margin)
        while extra > 0 and guard < 400:
            cur += _dt.timedelta(days=1)
            guard += 1
            if _is_trading_day(cur, cal):
                extra -= 1
    return cur, sure


def trading_days_between(start: Any, end: Any,
                         cal: Optional["TradeCalendar"] = None) -> Optional[int]:
    """(start, end] 区间内的交易日数量；参数解析不了返回 None。

    用来判断数据陈旧程度：周一看周五的数据，日历天差 3 天但交易日只差 1 天，
    按日历天算会误报「陈旧」。

    **口径限制**（同 `trading_window_end` 那条，方向相反）：日历覆盖不到的那一段
    把节假日当成了交易日，所以返回值在那一段是**上限**而不是精确值 —— 陈旧检测
    会更容易报警（安全方向），而 `cb_redeem` 的「剩余交易日」会偏大（乐观方向）。
    后者边上就印着最后交易日原值，读者拿日期本身就能核对，所以这里不加边际：
    在一个展示数上做保守调整，会让它和边上那个原始日期对不上。
    """
    s, e = _as_date(start), _as_date(end)
    if s is None or e is None:
        return None
    if s > e:
        return 0
    n, cur, guard = 0, s, 0
    while cur < e and guard < 400:
        cur += _dt.timedelta(days=1)
        guard += 1
        if _is_trading_day(cur, cal):
            n += 1
    if cur < e:                                 # 区间比守卫还长：说出来，别当成算完了
        log.warning("trading_days_between 撞上 400 天守卫（%s → %s），返回值偏小", s, e)
    return n


# ============================ 数值解析 ============================
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# 顺序就是判据：**长的写法必须排在前面**。字典按插入序遍历，「万」排第一时
# '1.2万亿' 会先命中「万」→ 1.2e4，比正确值小 8 个数量级。现实数据里够不到
# 这个量级，但判据靠顺序维持的地方，顺序本身就该写死并说明。
_UNIT_MULT = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3, "百": 1e2}


def to_float(v: Any) -> Optional[float]:
    """把 '3.10%' / '-4.20%' / '1,234.5' / '1.2万' / Decimal / np.float64 转成 float。

    注意口径：**百分比只去掉 % 号，不除以 100** —— 折溢价阈值配的就是 3.0 这种
    百分数，除了 100 会让阈值判断整体错一个量级。
    """
    if _is_null(v):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f

    s = str(v).strip()
    if s in _NULLISH:
        return None
    s = s.replace(",", "").replace("，", "").replace(" ", "").replace("　", "")
    s = s.replace("＋", "+").replace("－", "-").replace("%", "").replace("％", "")

    mult = 1.0
    for unit, m in _UNIT_MULT.items():
        if unit in s:
            mult = m
            s = s.replace(unit, "")
            break

    try:
        f = float(s)
    except ValueError:
        m = _NUM_RE.search(s)                   # 末路兜底：抠出第一个数字
        if not m:
            return None
        try:
            f = float(m.group())
        except ValueError:
            return None
    f *= mult
    return None if (math.isnan(f) or math.isinf(f)) else f


# ====================== 文本 / 链接 清洗 ======================
_TAG_RE = re.compile(r"<[^<>]{0,200}?>")        # 限长，避免病态回溯
_WS_RE = re.compile(r"\s+")


def strip_html(v: Any) -> str:
    """去掉 HTML 标签 + 反转义实体 + 压缩空白。

    巨潮搜索接口会把命中的关键词用 `<em>` 高亮包起来，返回的「公告标题」长这样：
        关于<em>要约收购</em>报告书摘要的提示性公告
    直接进报告会显示成裸标签（markdown 里）或被当成真的斜体（html 里）。

    先删标签再反转义：原文里如果是被转义的 `&lt;em&gt;`（即字面文本），
    这个顺序能把它原样保留，而不会被误当成标签删掉。
    """
    if _is_null(v):
        return ""
    s = str(v)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = s.replace("\u200b", "").replace("\xa0", " ")     # 零宽 / 不换行空格
    return _WS_RE.sub(" ", s).strip()


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# safe 里带上 %，已编码的 %20 不会被二次编码成 %2520
_SAFE_PATH = "/%:@!$&'()*+,;=~-._"
_SAFE_QUERY = "/?%:@!$&'()*+,;=~-._"


def clean_url(v: Any) -> str:
    """规范化链接：清标签实体 + 把空格等非法字符百分号编码。

    akshare 的 `stock_zh_a_disclosure_report_cninfo` 是这样拼公告链接的：
        ...&announcementTime={公告时间}
    而「公告时间」是 `2026-08-01 00:00:00` 这种带空格的字符串，于是查询串里
    直接含一个裸空格。后果不是"不好看"：**markdown 的 `[text](url)` 语法遇到
    空格就截断**，报告里那条公告链接会整个渲染坏掉；HTML 里浏览器虽然多半会
    自动补编码，但那是各家实现的宽容度，不是标准行为。
    这里统一编成 `%20`，语义完全等价。
    """
    s = strip_html(v)
    if not s:
        return ""
    if not _SCHEME_RE.match(s):                 # 相对路径/非 URL：原样返回，不瞎猜
        return s
    try:
        parts = urlsplit(s)
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe=_SAFE_PATH),
            quote(parts.query, safe=_SAFE_QUERY),
            quote(parts.fragment, safe=_SAFE_QUERY),
        ))
    except Exception:
        return s.replace(" ", "%20")
