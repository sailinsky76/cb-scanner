"""阶段 3 的**第一次**探针：配债的触发点能不能前移到「获批但还没发行」那一段。

**不产出机会条目、不接进 `scanner/`**（纪律 1）。它只回答问题。

──────────────────────────────────────────────────────────────────────
为什么要探这一段

`cb_allotment.py` 靠**股权登记日**出条，而登记日只在**发行公告之后**才有值，
通常只提前 2-3 个交易日 —— 那时正股多半已经高开完了。
所以那一栏做的正是那篇文章自己说会亏钱的事，v5.0 才把它做成了警告标签
（含权量 + 「正股每跌 1%，需上市溢价 X% 才打平」）。

真正的埋伏窗口在**获批 → 发行公告**之间的 1-2 个月。那一段的数据不在
`bond_zh_cov` 里 —— 那是已公告发行的表。这一栏要么找到一张**更早**的表，
要么承认看不到。**要么找到，要么承认，不许猜。**

──────────────────────────────────────────────────────────────────────
一个用词上的坑，先说清楚，免得搜了个空还以为是「这段时间没有获批的债」

待办队列和 README 里写的触发点是「**证监会核准**」—— 那是那篇文章的用词。
2023 年全面注册制之后，转债走的是**交易所审核 + 证监会同意注册**，
批文叫「同意注册批复」，「核准」这个词在近两年的公告标题里**可能根本不出现**。

所以本探针 Q3 **不去数某一个词**，而是把一组候选写法各自的命中数**全都打出来**，
让实测告诉你现在到底叫什么。只搜一个词的话，搜不到会长得和「这段时间没有获批的债」
一模一样 —— 那正是纪律 5 点名的静默归零。

同一个坑还有第二种形态：「**不予注册**」「注册批复**到期失效**」「**终止**本次发行」
都含着「注册」二字，含义却完全相反。`classify_stage()` 里反向写法判在获批**之前**，
和 `diag_redeem.py` 的 `classify_title()`（「不提前赎回」是「提前赎回」的子串）
是同一个坑的第三次出现。混错的代价不是少赚，是照着一只已经黄了的债去埋伏。

──────────────────────────────────────────────────────────────────────
五个问题，一个都不预设答案

  Q1  `bond_cov_issue_cninfo`（巨潮·可转债发行，31 列）里有没有**还没申购**的行？
      公告日期比网上申购日期提前多少天？←—— 这个数直接决定触发点能前移多久
  Q2  `bond_zh_cov_info_ths`（同花顺）有没有「已公告未申购」的行？覆盖多少列？
  Q3  巨潮公告检索：获批那一步在标题里**实际**长什么样？各候选写法各命中多少？
      反向写法（不予注册/终止/失效）实际出现多少条？
  Q4  含权量要用的「拟发行规模」，**单位是什么**？
      拿已发行的债和 `bond_zh_cov` 的「发行规模（亿元）」对一次，把单位钉死。
  Q5  ~~**验收**：能不能复现 08-09 那份日报的四只 ——
      四方科技 23%、丰茂股份 16%、满坤科技 14%、千红制药 11%？~~
      **v5.8 起这一问默认不跑**（`--with-bench` 才跑），见下面「现在问什么」。

Q4 单独立一问，是因为它是这一轮最可能出的那种错：
v4.6.1 那次深市配售单位放大 10 倍，就是一个没对过的单位假设，**害了整整一栏**。
拟发行规模从巨潮拿是「万元」还是「元」还是「亿元」，猜错就是 10⁴ 倍的含权量。

──────────────────────────────────────────────────────────────────────
现在问什么（**v5.8 换过一次 —— 换问题就是换判据**）

原来的验收是**复现四只的含权量**（A 方案）。那条路 v5.7 判死了：
含权量要「已获批未发行」那一段的拟发行规模，而 akshare **没有这张表** ——
不是网络问题，是**源里没有**，等下去是无限期卡住。

所以换成 **B 方案：先只答「谁获批了」**，含权量等有源了再说。
判据只剩两条，都在 `decide()` 里：

  ① **名单拿得到** —— 检索确实通了，且获批档里的转债条数 ≥ `_MIN_APPR`
  ② **截断限制说得全** —— 撞上服务端封顶的那几路，唯一数都数得出来

②看着像附加题，其实是 B 能不能成立的前提：B 给的答案是
「**这些确实获批了**」而不是「**获批的就这些**」。这个降级要有出处，
就得说清哪几路被截、截了之后还剩多少能读 —— 说不清的时候，
「下限」这个词就没有出处，那就不许用（纪律 2）。

──────────────────────────────────────────────────────────────────────
用法

    python diag_cbplan.py                  # 完整探测（联网，约 1-3 分钟）
    python diag_cbplan.py --days 365       # Q1/Q3 往回看更久（默认 180 天）
    python diag_cbplan.py --rows 20        # 多印几行原始数据供肉眼核对
    python diag_cbplan.py --with-bench     # 连 Q5 含权量复现一起跑（A 方案遗留）
    python diag_cbplan.py --bench 四方科技=23,千红制药=11   # 换验收名单（配 --with-bench）
    python diag_cbplan.py --selftest       # 纯函数离线自测，不联网，秒回

退出码（**机械判定，判据写死在 decide() 里，可证伪**）：
    0 = 获批名单拿得到 **且** 截断限制说得全
        → 「谁获批了」答得上来，B 方案成立
        → **但名单是下限不是全集**，只许说「这些确实获批了」
    1 = 至少一项不成立（检索没通 / 名单太短 / 撞了顶又数不出唯一数）
        → 说清是哪一项不成立；**不成立不等于这条路死了**，可能只是换一天再跑
    2 = 连表都拿不到 → 这是取数问题。**别去跑 `diag_sources.py`** ——
        它测的是 fund_premium 那三个基金接口，这几张表一个都不碰（§6.7 ④）。
        本文件自己那一路的连通性用 `--spot-check`，别的表就换一天重跑。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from datetime import date, timedelta
from functools import partial

sys.path.insert(0, ".")

from scanner.utils import (fmt_date, parse_date, retry_call,  # noqa: E402
                           strip_html, to_float)

# ---------------------------------------------------------------------------
# 探测目标。每一张的选取理由都写在旁边 —— 候选表是**扫本机 akshare 的导出面**
# 得到的（`dir(akshare)` + `inspect.getsource` 读列名），不是凭印象列的。
_T_ISSUE = "bond_cov_issue_cninfo"    # 巨潮·可转债发行，31 列，带「计划发行总量」
_T_THS = "bond_zh_cov_info_ths"       # 同花顺·可转债，带「计划发行量/申购日期」
_T_COV = "bond_zh_cov"                # 东财转债总表，用来钉单位 + 交叉核对
_T_SPOT = "stock_zh_a_spot_em"        # 全 A 行情，带「总市值/流通市值/最新价」
_T_DISC = "stock_zh_a_disclosure_report_cninfo"   # 巨潮公告检索，event_arb 已在用

# 顺带记一条**没有**的：akshare 里 `stock_register_*` 那一族全是新股 IPO 审核，
# 与再融资/转债无关。所以「获批」那一段只能走公告检索（Q3），没有现成的状态表。

# Q3 的候选写法。**这一组不是判据，是量表** —— 每个都去搜一次，把命中数打出来。
# 新旧两种制度的写法都放进去了，还放了三个反向写法做对照：
# 如果反向写法一条都搜不到，那多半是检索本身没通，而不是「没有黄掉的债」。
_KW_POSITIVE = ["同意注册", "注册批复", "证监会核准", "核准批复"]
_KW_NEGATIVE = ["不予注册", "终止发行", "到期失效"]
_KW_CONTEXT = ["可转换公司债券", "发行公告"]

# Q5 验收名单：08-09 那份日报的四只（正股简称 → 日报印的含权量 %）。
# 名单会随时间失效（发完就不是待发了），所以脚本同时把当天的完整候选名单打出来。
#
# **v5.8：这四只连同整个 Q5 已经不在默认路径上了**（`--with-bench` 才跑）。
# 去留的理由写在 §6.9 ③：选了 B 之后含权量复现**已不在问题范围内**，
# 而它唯一的输入 `stock_zh_a_spot_em` 到现在 10 次调用一次没通过 ——
# 默认跑它等于每轮花 1-3 分钟换一份「这一问没答」。
# **留着不删**：名单和那套算法都没有错，缺的是源；哪天有了待发债的规模表，
# 打开这个开关就能接着验，删掉则要从头再写一遍。
_BENCH = {"四方科技": 23.0, "丰茂股份": 16.0, "满坤科技": 14.0, "千红制药": 11.0}

# ---------------------------------------------------------------------------
# 判定门槛。**改判据必须改这里 + decide()，别在别处开口子。**
#
# B 方案（v5.8 起的现行判据）—— 这两个数决定退出码：
_MIN_APPR = 3       # 获批档里的**转债**条数至少这么多，才算名单拿得到
#
# _MIN_APPR 为什么是 3：它测的是「这条路produces得出一份名单」，**不是**去够
# 实测那个 22。1-2 条可能是某一家的公告碰巧漏进来，3 条才说明分类和检索都在工作。
# **故意取得远低于实测值** —— 门槛贴着样本设就是拿已知数凑判据（纪律 2），
# 那样它测的就不再是机制，而是「今天这一批还在不在」。
# 形状照 _MIN_PRE 那个老门槛来（下面那个，B 方案里已降级成附注）。

# A 方案（含权量复现）遗留下来的三个数。**已不参与退出码判定** ——
# 只在 `--with-bench` 打开时用来印残差表。§6.9 ③ 记了为什么留着不删。
_MIN_PRE = 3        # 「还没申购」的行至少这么多。B 方案里降级成附注，不再是判据
_TOL_PCT = 1.5      # 含权量残差容忍（个百分点）。日报印的是整数，本身就带 ±0.5
_MIN_HIT = 3        # 四只里至少复现这么多只

# 含权量的候选口径**只有两个，事先注册在这里**：总市值 / 流通市值。
# 两个都算、都印残差。**v5.8 起它不再判退出码**，只印给人看。
# **不再往下搜第三个缩放系数** —— 拿四个已知数去凑一个口径就是造数（纪律 2）。
# 之所以是这两个：优先配售配给全体原股东（对总股本），但部分方案只对
# 无限售条件股东，两种写法都见过。哪个对，让这四只说话，不由我拍。
_CALIB = ("总市值", "流通市值")


# ═══════════════════════ 纯函数（不联网，--selftest 覆盖）═══════════════════════

def norm_code(v) -> str:
    """代码归一：去空白、去交易所后缀，取末尾 6 位数字。

    和 diag_redeem2 里同名函数同一套 —— 按名称匹配已经在齐翔2 上翻过一次车。
    """
    s = re.sub(r"[^0-9]", "", str(v or ""))
    return s[-6:] if len(s) >= 6 else s


def equity_weight_pct(plan_size_yuan, mcap_yuan):
    """含权量(%) = 拟发行规模 ÷ 总市值 × 100。

    这是**恒等式的另一种写法**，不是新模型：

        含权量 = 每股配售额 ÷ 正股价
        每股配售额 = 拟发行规模 ÷ 总股本
        ⇒ 含权量 = 拟发行规模 ÷ (总股本 × 正股价) = 拟发行规模 ÷ 总市值

    两边约掉的是同一个正股价，所以它和 `cb_allotment.py` 那一栏印的是**同一个数**，
    只是把「等发行公告才有的每股配售额」换成了「预案里就有的拟发行规模」。

    但它比那一栏**多带一个假设**：原股东可优先配售的比例是 100%。
    这个假设本探针不替它成立 —— Q5 拿四只已知的数去验，验不过就是验不过。
    """
    a, b = to_float(plan_size_yuan), to_float(mcap_yuan)
    if a is None or b is None or b <= 0 or a <= 0:
        return None
    return a / b * 100.0


def breakeven_premium_pct(weight_pct):
    """正股每跌 1%，需上市溢价 X% 才打平 = 100 ÷ 含权量。纯算术，沪深通用。"""
    w = to_float(weight_pct)
    if w is None or w <= 0:
        return None
    return 100.0 / w


def unit_label(factor):
    """把「原始值 ÷ 亿元值」这个比值翻译成单位名。

    巨潮那张表的「计划发行总量」单位没写在列名里，猜错就是 10⁴ 倍的含权量
    （v4.6.1 那次深市 10 倍放大是同一类错，害了整整一栏）。
    所以不猜：拿已发行的债和 bond_zh_cov 的「发行规模（亿元）」对一次，
    比值落在哪个数量级就是哪个单位。**对不出来就说对不出来。**
    """
    f = to_float(factor)
    if f is None or f <= 0:
        return "判不了"
    for exp, name in ((0, "亿元"), (4, "万元"), (8, "元")):
        lo, hi = 10 ** exp * 0.5, 10 ** exp * 2.0
        if lo <= f <= hi:
            return name
    return f"非常规（比值 ≈ {f:.3g}，不是 1/1e4/1e8）"


# 阶段分类规则。**顺序就是判据**，反向写法必须排在最前面。
#
# 「不予注册」含「注册」，「注册批复到期失效」含「注册批复」，「终止本次发行」含「发行」——
# 顺序反了方向就反了，而这里方向反了的代价是：照着一只已经黄了的债去埋伏正股。
# 和 diag_redeem.py 的 classify_title()（「不提前赎回」vs「提前赎回」）
# 是同一个坑的第三次出现，所以这一次直接把顺序写成数据结构，让 selftest 钉住它。
_STAGE_RULES = [
    ("终止/失效（反向）", ("不予注册", "不予核准", "未获", "终止", "中止", "撤回", "失效")),
    ("获批（注册/核准）", ("同意注册", "注册批复", "予以注册", "证监会核准", "核准批复")),
    ("发行/申购", ("发行公告", "发行提示", "网上路演", "中签率", "中签结果", "上市公告")),
    ("过会/审核通过", ("审核通过", "审议通过", "上市委", "上会")),
    ("在审（受理/问询）", ("受理", "问询", "反馈意见")),
    ("预案/董事会", ("预案", "发行方案", "董事会决议", "股东大会")),
]
_STAGE_ORDER = [name for name, _ in _STAGE_RULES] + ["其他（未分类）"]


def classify_stage(title: str) -> str:
    """把一条公告标题归到发行流程的哪一步。**证据分组，不是结论。**

    命中即返回，所以规则表的顺序就是优先级。未命中一律进「其他」——
    宁可多一堆未分类让你肉眼看，也不硬塞进某一档。

    **先 strip_html 再匹配**（probe3 第一次跑之后加的）。巨潮检索把命中的
    每个词各自裹一层 `<em>`，`同意注册` 回来是 `<em>同意</em><em>注册</em>` ——
    四字关键词被标签劈成两半，`"同意注册" in s` 直接为假。
    后果是**有方向的**：反向写法（终止/中止/失效/撤回）都是两字，裹一层照样命中；
    获批写法（同意注册/注册批复/予以注册/核准批复）全是四字，全被劈开。
    → 正向漏判、反向照判，正是「照着一只已经黄了的债去埋伏」那个方向。
    probe3 的实测形态：四条「同意注册」样例标题全被判进「其他（未分类）」，
    而「获批」那 604 条其实来自两个上下文检索（标签裹在别的词上）。
    和 probe2 里「强赎天计数带 HTML」是同一件事的第二次出现 —— 删标签，文本不动。
    """
    s = strip_html(title or "")
    for name, words in _STAGE_RULES:
        if any(w in s for w in words):
            return name
    return "其他（未分类）"


def is_pre_issue(apply_date, today=None) -> bool:
    """「还没申购」= 网上申购日期为空、或者还在未来。

    这一档就是埋伏窗口。为空的那一批尤其要紧 —— 它可能是「还没定申购日」
    （正是我们要的），也可能是「这一列没取到」。两者本探针分不开，
    所以 Q1 会把两种分开计数，不合并成一个数（纪律 5：可以少印，不可以少说）。
    """
    d = parse_date(apply_date)
    t = today or date.today()
    return d is None or d > t


def lead_days(announce, apply_date):
    """网上申购日 − 公告日，单位自然日。这就是「触发点能前移多久」的直接度量。"""
    a, b = parse_date(announce), parse_date(apply_date)
    if a is None or b is None:
        return None
    return (b - a).days


# 输出流编码的修复**搬进了 `diag_common.py`** —— v5.5 只修在这一个文件里，
# 结果 v5.6 那轮 `diag_sources.py` 照原样崩了一次（`docs/probes/probe7.txt` 是现场）。
# 六个探针现在一律从那里拿，见 `diag_common.py` 的文件头。
from diag_common import apply_stream_fix, stream_fix_plan  # noqa: E402


# 巨潮检索的翻页在 akshare 内部，本探针拿到多少算多少 —— 少拉了几页
# 长得和「就这么多」一模一样。probe3/probe4 是**同一天**（2026-08-10）、
# 同一窗口、同一参数跑的两次，八个关键词命中数一字不差，只有
# 「可转换公司债券」从 3810 掉到 3270，差整 540 条 = 18 × 30。
# 三个 >3000 的数（3810 / 3270 / 8880）都能被 30 整除，
# 而小数（198 / 235 / 182 / 11 / 5）一个都不能 —— 大结果集是**按整页回的**。
# 机制没探明（丢页 / 服务端封顶 / 相关性截断都可能），所以这里
# **v5.6 更新：方向已经量出来了，而且原来那句写反了。** probe6 的重复行实测：
#   可转换公司债券 3300 行 / 唯一 **3000** / 重复 300
#   发行公告       8880 行 / 唯一 **3000** / 重复 5880
#   七路小数（203/240/9/181/5）**一条重复都没有**
# 两路大数的唯一数**都是整整 3000 = 100 整页** —— 服务端在 **100 页封顶**，
# 而 akshare 越过封顶继续翻页，回来的是**重复行**。
# 所以行数 = 30 × 翻的页数（127/109/110/296 页），**它不是任何东西的计数**：
#   · 当「取回了多少条不同的公告」看 → 3300/8880 **偏大**（真值 3000），是上界
#   · 当「窗口里实际有多少条」看     → 3000 本身就是封顶，只是个**下限**
# 原来那句「可能是下界而非全量」把这两件事混成了一句，而且方向取错了那一半。
# 只报**已经量到的**：封顶在哪、重复多少、唯一多少。重复行的具体形态（哪几页在重）没量。
_PAGE_SIZE = 30
_PAGE_FLAG_MIN = _PAGE_SIZE * 10        # 300 条以下，%30==0 更可能是巧合


# ── 存档提示：**一个具体文件名都不许写死** ──
# 本探针跑第三次时（probe5），这里印的还是「存档命名：docs/probes/probe4.txt」——
# 照它做就会**覆盖掉 docs/probes/probe4.txt**，而「同一天两次跑少了整 18 页」那 540 条，
# 靠的正是包里同时存着 probe3 和 probe4（HANDOFF §10）。
# 探针不知道自己是第几次跑，也不该猜；所以只印**规则**，不印编号。
# 写死编号会随轮次腐烂，而这一处腐烂的后果是**销毁证据**，比印错一个数重。
def archive_hint() -> str:
    """存档提示。**不含任何具体文件名** —— 编号会腐烂，规则不会。"""
    return ("存档：另存为一个**没用过的**编号（probeN.txt，N 取现有最大号 +1）。"
            "**已有的 probe*.txt 一份都不许覆盖** —— "
            "多份对着看才看得见「同一个查询两次跑回来不一样」这类事。")


# 上面那两种读法（下界 / 上界）分得开，而且**不用再联网一次** ——
# 每一路的结果已经在手里了，数一下**同一路里有多少行是重复的**就行：
#   全不重复 → 少的那些页是真的没回来（**下界**）
#   有重复   → 是拿重复行把整页补满的（**上界**）
# 按「公告链接」数，不按标题 —— 不同公司的公告标题**大量重名**
#（probe5 里「关于向特定对象发行股票方案到期失效的公告」前五条就撞了三条），
# 拿标题数会把不同公司的公告误判成重复。链接里带 announcementId，是唯一的。
def dup_rows(df, key_col) -> tuple:
    """数同一路结果里的重复行。返回 `(总行数, 唯一数, 重复数)`。

    `key_col` 缺失时返回 `(n, None, None)` —— **不拿标题去凑**，
    「数不了」和「没有重复」是两件事（纪律 5）。
    """
    if df is None:
        return (0, None, None)
    n = len(df)
    if not key_col or key_col not in [str(c) for c in df.columns]:
        return (n, None, None)
    uniq = df[key_col].astype(str).nunique()
    return (n, uniq, n - uniq)


def page_bound_note(n: int, page: int = _PAGE_SIZE,
                    floor: int = _PAGE_FLAG_MIN) -> str | None:
    """命中数看着像「整页数」时给一句提醒；否则返回 None。

    **返回 None 不等于「这个数是全量」**，只等于「没有整页数这个迹象」。
    """
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    if n < floor or page <= 0 or n % page:
        return None
    return (f"⚠ {n} = {n // page} 整页 —— **这个数不是计数**，"
            f"是 30 × 翻的页数（见下面的重复行实测）")


def decide(tables_ok: bool, appr: dict | None, *,
           n_pre_dated: int = 0, n_pre_blank: int = 0,
           lead_stats: dict | None = None) -> tuple:
    """机械判定退出码。**判据全在这里，改判据必须改这一个函数。**

    v5.8 起判的是 **B 方案的问题**：「谁获批了」答不答得上来，
    而不再是「含权量口径复现了几只」（那是 A 方案，已搁置，见 §6.9）。
    换问题就是换判据 —— 旧判据整段拿掉了，没有留在别处当暗门。

    appr: Q3 归类出来的获批情况，`probe_announcements()` 直接返回：
        {"searched": bool,      # 检索真的通了没有（正反两边不是都为 0）
         "n_cb":     int,       # 获批档里标题含转债字样的条数 ←—— 这就是名单长度
         "n_all":    int,       # 获批档合计（含定增，六分之五是它）
         "capped":   [kw…],     # 命中数是整页数的那几路（撞上服务端封顶）
         "uncountable": [kw…]}  # 唯一数**数不了**的那几路（没有公告链接列）
        `None` 表示 Q3 整段没跑成 —— 和「跑了但一条没有」是两件事（纪律 5）。
    lead_stats: Q1 的提前量分布 {"n":.., "max":.., "n_long":..}。
        **B 方案里它已经不是判据**，只在附注里说一句 Q1 那条路为什么关了。
    n_pre_dated / n_pre_blank: 同上，附注用，不参与判定。
    返回 (退出码, [理由行…])。
    """
    if not tables_ok:
        return 2, [f"连 {_T_ISSUE} / {_T_THS} 都没拿到 —— 这是取数问题，不是策略问题。",
                   "  **别去跑 diag_sources.py 查这两张表** —— 它不碰它们（§6.7 ④）。",
                   "  换一天重跑；连着两天都拿不到，再当成源出了问题。"]

    why = []

    # ── 判据①：名单拿得到 ────────────────────────────────────────────────
    # 「拿得到」= 检索确实通了 **且** 获批档里的转债条数够得上 _MIN_APPR。
    # 两个条件缺一不可：检索没通时 n_cb 也是 0，但那个 0 的含义是「没问着」，
    # 不是「没有」—— 正是纪律 5 那条，probe3 已经栽过一次。
    if appr is None:
        why.append("名单：Q3 **整段没跑成** —— 公告检索一路都没回来。")
        why.append("  **这不等于「没有获批的债」**（纪律 5）：这一次什么都没问着。")
        why.append("  换一天重跑；连着两天都这样，再往「检索接口变了」上想。")
        return 1, why + ["", "→ 阶段 3 先不写实现。上面哪一项不成立就先补哪一项。"]

    searched = bool(appr.get("searched"))
    n_cb = int(appr.get("n_cb") or 0)
    n_all = int(appr.get("n_all") or 0)
    capped = list(appr.get("capped") or [])
    uncountable = list(appr.get("uncountable") or [])

    ok_list = searched and n_cb >= _MIN_APPR

    if not searched:
        why.append("名单：正反两边命中都是 0 —— 这更像**检索本身没通**，"
                   "而不是「这段时间没有获批的债」。")
        why.append("  **两者在输出里长得一模一样，所以只能报没问着，不许报没有**（纪律 5）。")
    elif ok_list:
        why.append(f"名单：获批档 {n_all} 条，其中标题含转债字样 **{n_cb}** 条"
                   f"（要求 ≥{_MIN_APPR}）。")
    else:
        why.append(f"名单：检索通了，但获批档里只有 {n_cb} 条转债"
                   f"（获批档合计 {n_all} 条），不足 {_MIN_APPR} 条。")
        why.append("  **这不等于这条路死了** —— 也可能是这个窗口里真的就这么少。"
                   "把 --days 放宽再跑一次。")

    # ── 判据②：截断限制说得全 ────────────────────────────────────────────
    # B 方案答的是「这些确实获批了」，**不是「获批的就这些」**。
    # 这个降级要成立，前提是能说清哪几路被截、截在哪 —— 而说清截断靠的是
    # 唯一数（v5.6 定的读法：唯一数 < 行数 → 越顶回的是重复行）。
    # 撞了顶又数不出唯一数的那一路，**截了多少无从说起**，那时候「下限」
    # 这个词就没有出处了，只能退 1。
    blind = [kw for kw in capped if kw in uncountable]
    ok_limits = not blind

    if not capped:
        why.append("截断：这一轮没有哪一路的命中数是整页数 —— **没有撞顶的迹象**。")
        why.append("  但**「没有迹象」不等于「拿到了全量」**：服务端封顶只在大结果集上"
                   "咬人，小结果集看不出来。名单仍然按**下限**用。")
    elif ok_limits:
        why.append(f"截断：{len(capped)} 路（{'/'.join(capped)}）撞上服务端封顶，"
                   f"每一路的唯一数都数得出来 —— **截断限制说得清**。")
        why.append("  读法（v5.6 定）：唯一数小于行数 → 越过封顶的页回的是重复行；"
                   "唯一数是 30 的整数倍且几路相同 → 那个数就是封顶值，不是真实条数。")
    else:
        why.append(f"截断：{len(blind)} 路（{'/'.join(blind)}）**撞了顶又数不出唯一数**"
                   f"（没有公告链接列）。")
        why.append("  撞顶要靠唯一数才说得清截了多少，**这几路截了多少无从说起** ——"
                   "那「下限」这个词就没有出处了。")
        why.append("  **数不了 ≠ 没重复**（纪律 5）。先确认这一路的列名变了没有。")

    if n_pre_blank or n_pre_dated:
        why.append(f"（附注：Q1 那张表这一轮 {n_pre_dated} 行申购日在未来、"
                   f"{n_pre_blank} 行为空。**B 方案里它已经不是判据** ——"
                   f"§6.6 ② 早已关门：这张表结构上就晚。）")
    if lead_stats and lead_stats.get("n") and not lead_stats.get("n_long"):
        why.append(f"（附注：提前量 {lead_stats['n']} 行里最大 {lead_stats['max']} 天、"
                   f"8 天以上 0 行 —— 换一天跑从来就不是扩样本的路子。）")

    if ok_list and ok_limits:
        why.append("")
        why.append("→ **「谁获批了」这一问答得上来**，阶段 3 的 B 方案成立。")
        why.append("  **但这份名单是下限，不是全集** —— 上面那几路被服务端截在封顶之上，"
                   "窗口里实际有多少条不知道，**截掉的那部分有没有偏也没量过**。")
        why.append("  所以只许说「这些确实获批了」，**不许说「获批的就这些」**。")
        why.append("  接线之前先写好栏目级说明里的这一句（§6.5 B 的风险）："
                   "**上了名单 ≠ 值得埋伏** —— 这一栏给的是时点，不是方向（纪律 8）。")
        why.append("  含权量这一半仍然缺源，**照 §6.9 搁置，不在这一问的范围内**。")
        return 0, why

    why.append("")
    why.append("→ 阶段 3 先不写实现。上面哪一项不成立就先补哪一项。")
    return 1, why


# ═══════════════════════════ 联网部分 ═══════════════════════════

def _hr(title: str) -> None:
    print(f"\n{'─' * 68}\n▎{title}")


def _fetch(ak, name: str, label=None, **kw):
    """取一张表。取不到就如实说，**不返回空表冒充成功**。"""
    fn = getattr(ak, name, None)
    if fn is None:
        print(f"  ✗ 本机 akshare 没有 {name} —— 版本太旧？pip install -U akshare")
        return None
    call = partial(fn, **kw) if kw else fn
    t0 = time.time()
    df, err = retry_call(call, label=label or name, attempts=2, backoff=(2.0,),
                         reject_empty=False)
    sec = time.time() - t0
    if df is None:
        print(f"  ✗ {label or name}：调用失败（{sec:.1f}s）{err}")
        return None
    print(f"  ✓ {label or name}：{len(df)} 行 × {len(df.columns)} 列（{sec:.1f}s）")
    return df


def _pick(df, candidates):
    """按候选顺序挑一个存在的列名。"""
    if df is None:
        return None
    cols = [str(c) for c in df.columns]
    return next((c for c in candidates if c in cols), None)


def _columns(df, rows: int, title: str) -> None:
    """逐列印非空数 + dtype + 两个样例。列名改版时这一段就是出处。"""
    print(f"  ── {title}：{len(df.columns)} 列 ──")
    for i, col in enumerate(df.columns, 1):
        s = df[col]
        nn = int(s.notna().sum())
        samples = [repr(v)[:26] for v in s.dropna().head(2).tolist()]
        tail = "，".join(samples) if samples else "（全空）"
        print(f"  {i:>3}. {str(col):<16} 非空 {nn:>5}/{len(df)}　"
              f"dtype={s.dtype}　样例 {tail}")
    if rows > 0:
        print(f"  ── 前 {rows} 行原始值（供肉眼核对）──")
        with_pd(df.head(rows))


def with_pd(df) -> None:
    import pandas as pd
    with pd.option_context("display.max_columns", None, "display.width", 200,
                           "display.max_colwidth", 22):
        print(df.to_string())


def probe_issue_table(ak, days: int, rows: int, today: date):
    """Q1：巨潮那张发行表里有没有「还没申购」的行，公告能提前多久。"""
    _hr(f"Q1  {_T_ISSUE}：有没有「还没申购」的行？公告能提前多久？")
    start = (today - timedelta(days=days)).strftime("%Y%m%d")
    end = (today + timedelta(days=90)).strftime("%Y%m%d")
    print(f"  取数窗口 {start} ~ {end}（--days {days}，末端多留 90 天接未来的）")
    df = _fetch(ak, _T_ISSUE, label=f"{_T_ISSUE}({start}~{end})",
                start_date=start, end_date=end)
    if df is None or df.empty:
        print("  → 这一问没答成。表拿不到 / 空表，看上面那行错误。")
        return None, 0, 0, None

    _columns(df, rows, "列清单")

    c_apply = _pick(df, ["网上申购日期", "申购日期"])
    c_ann = _pick(df, ["公告日期"])
    c_plan = _pick(df, ["计划发行总量", "计划发行量"])
    c_code = _pick(df, ["债券代码", "代码"])
    c_name = _pick(df, ["债券简称", "债券名称", "名称"])
    print(f"\n  关键列：申购日={c_apply}　公告日={c_ann}　"
          f"拟发行规模={c_plan}　代码={c_code}　简称={c_name}")
    if c_apply is None or c_plan is None:
        print("  ✗ 缺关键列 —— 这一问答不了，不是「没有待发的债」。")
        return df, 0, 0, None

    n_dated = n_blank = n_past = 0
    for _, r in df.iterrows():
        d = parse_date(r.get(c_apply))
        if d is None:
            n_blank += 1
        elif d > today:
            n_dated += 1
        else:
            n_past += 1
    print(f"\n  ── 申购日分布（共 {len(df)} 行）──")
    print(f"    已过（{fmt_date(today, '%Y-%m-%d')} 之前）  {n_past:>5} 行")
    print(f"    还在未来（**埋伏窗口**）              {n_dated:>5} 行")
    print(f"    为空                                  {n_blank:>5} 行"
          f"  ← 「还没定申购日」和「这列没取到」长得一样，本探针分不开")

    lead_stats = None
    if c_ann:
        leads = [lead_days(r.get(c_ann), r.get(c_apply)) for _, r in df.iterrows()]
        leads = [x for x in leads if x is not None]
        if leads:
            lead_stats = {"n": len(leads), "max": max(leads),
                          "n_long": sum(1 for x in leads if x >= 8)}
            leads.sort()
            n = len(leads)
            print(f"\n  ── 提前量：网上申购日 − 公告日（{n} 行有两个日期）──")
            print(f"    最小 {leads[0]} 天　中位 {leads[n // 2]} 天　最大 {leads[-1]} 天")
            for lo, hi, tag in ((-10 ** 9, 7, "≤7 天"), (8, 30, "8-30 天"),
                                (31, 90, "31-90 天"), (91, 10 ** 9, ">90 天")):
                k = sum(1 for x in leads if lo <= x <= hi)
                mark = "  ← 这一档才够埋伏" if lo >= 8 else ""
                print(f"    {tag:<10}{k:>5} 行{mark}")
            print("    **这个数就是触发点能前移多久**：如果全落在 ≤7 天，"
                  "这张表和 bond_zh_cov 一样晚，前移不了。")
        else:
            print("\n  ? 没有一行同时有公告日和申购日，提前量算不出来。")

    if n_dated:
        print(f"\n  ── 申购日在未来的 {n_dated} 行 ──")
        shown = 0
        for _, r in df.iterrows():
            d = parse_date(r.get(c_apply))
            if d is None or d <= today:
                continue
            print(f"    {fmt_date(d, '%Y-%m-%d')}  "
                  f"{str(r.get(c_code, '')):<8}{str(r.get(c_name, '')):<10}"
                  f"拟发行规模原始值={r.get(c_plan)!r}")
            shown += 1
            if shown >= max(rows, 10):
                print(f"    …（只印了 {shown} 行，总数见上）")
                break
    return df, n_dated, n_blank, lead_stats


def probe_ths(ak, rows: int, today: date):
    """Q2：同花顺那张表有没有「已公告未申购」的行。"""
    _hr(f"Q2  {_T_THS}：同花顺这一路有没有更早的行？")
    df = _fetch(ak, _T_THS)
    if df is None or df.empty:
        print("  → 这一路没取到。**不等于同花顺没有这个数**，可能是接口改版。")
        return None
    _columns(df, min(rows, 8), "列清单")
    c_apply = _pick(df, ["申购日期", "网上申购日期"])
    if c_apply is None:
        print("  ✗ 没有申购日期列，这一路判不了。")
        return df
    fut = sum(1 for v in df[c_apply] if is_pre_issue(v, today) and parse_date(v))
    blank = sum(1 for v in df[c_apply] if parse_date(v) is None)
    print(f"\n  申购日在未来 {fut} 行　为空 {blank} 行　共 {len(df)} 行")
    print("  → 和 Q1 对着看：两边都拿得到的话，实现时选覆盖率高的那一路。")
    return df


def probe_announcements(ak, days: int, today: date):
    """Q3：获批那一步在标题里实际长什么样。**量表，不是判据。**"""
    _hr("Q3  巨潮公告检索：获批那一步实际叫什么？")
    start = (today - timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    print(f"  检索窗口 {start} ~ {end}　market='沪深京'　symbol=''（全市场）")
    print("  **每个候选写法都搜一次，把命中数全打出来** ——")
    print("  只搜一个词的话，搜不到会长得和「这段时间没有获批的债」一模一样。")

    allkw = [(k, "正向") for k in _KW_POSITIVE] + \
            [(k, "反向对照") for k in _KW_NEGATIVE] + \
            [(k, "上下文") for k in _KW_CONTEXT]
    titles_by_kw = {}
    paged = []
    n_ok = 0                     # 真的回来了的路数（df is not None）
    for kw, tag in allkw:
        df = _fetch(ak, _T_DISC, label=f"检索「{kw}」", symbol="", market="沪深京",
                    keyword=kw, start_date=start, end_date=end)
        n = 0 if df is None else len(df)
        if df is not None:
            n_ok += 1
        titles_by_kw[kw] = (tag, df)
        note = page_bound_note(n)
        if note:
            paged.append(kw)
        print(f"      「{kw}」（{tag}）命中 {n} 条" + (f"　{note}" if note else ""))

    # 唯一数**数不了**的那几路。撞了顶又数不了的，截了多少无从说起 ——
    # 那正是 decide() 判据②咬的东西，所以这一份要带出这个函数。
    uncountable: list = []

    if paged:
        print(f"\n  ⚠ 上面 {len(paged)} 路（{'/'.join(paged)}）的命中数是 "
              f"{_PAGE_SIZE} 的整数倍。翻页在 akshare 内部，本探针拿到多少算多少 ——")
        print("    实测三次（08-10 两次、08-11 一次）：「可转换公司债券」回 "
              "3810 / 3270 / 3810，")
        print("    「发行公告」三次都是 8880。08-11 那次**窗口整体后移了一天**，"
              "七路小数里五路跟着变了")
        print("      （同意注册 198→203、注册批复 235→240、证监会核准 1→0、"
              "核准批复 11→9、终止发行 182→181），")
        print("    而这两路大数**一行都没变**。→ 大结果集的计数**不随查询窗口变**，"
              "这不是「点了一遍数」的样子。")
        print("    **机制已经量出来了**（probe6 的重复行实测，见下表）："
              "服务端在 **100 页 / 3000 条封顶**，")
        print("    akshare 越过封顶继续翻页，回来的是**重复行** —— "
              "所以行数 = 30 × 翻的页数，**不是计数**。")
        print("    → 这几路只有**唯一数**能读，而唯一数正好等于封顶值，"
              "所以它只是「窗口里至少有这么多」的**下限**。")
        print("    没打标记的也只是「没这个迹象」，不等于全量。")

        # 上面说的那个分辨方法，就在这里做掉 —— 结果都在手里，不用再联网。
        # 小数那几路是**对照组**：它们不是整页数，重复情况应该和大路不一样。
        print("\n  ── 重复行实测（按**公告链接**数，不按标题 —— 标题大量重名）──")
        c_link = None
        for _kw, (_tag, _df) in titles_by_kw.items():
            c_link = c_link or _pick(_df, ["公告链接", "链接", "adjunctUrl"])
        for kw, (tag, df) in titles_by_kw.items():
            n, uniq, dup = dup_rows(df, c_link)
            if uniq is None:
                uncountable.append(kw)
                print(f"    {kw:<12}{n:>6} 行　唯一数**数不了**（没有公告链接列）"
                      f"　—— 数不了 ≠ 没重复")
            else:
                mark = "　← 整页数那一路" if kw in paged else ""
                print(f"    {kw:<12}{n:>6} 行　唯一 {uniq:>6}　重复 {dup:>5}{mark}")
        print("    读法（v5.6 已定）：唯一数**小于**行数 → 越过封顶的页回的是重复行，"
              "行数偏大；")
        print("    　　　唯一数是 30 的整数倍且几路**相同** → 那个数就是**服务端的封顶值**，"
              "不是真实条数。")
        print("    小数那几路重复为 0，是对照组 —— **封顶只在大结果集上咬人**。")

    hit_pos = sum(len(df) for kw, (tag, df) in titles_by_kw.items()
                  if tag == "正向" and df is not None)
    hit_neg = sum(len(df) for kw, (tag, df) in titles_by_kw.items()
                  if tag == "反向对照" and df is not None)
    print(f"\n  正向合计 {hit_pos} 条　反向对照合计 {hit_neg} 条")
    if hit_pos == 0 and hit_neg == 0:
        print("  ⚠ 两边都是 0 —— 这更像是**检索本身没通**，而不是「没有获批的债」。")
        print("    别当成结论，也**别去跑 diag_sources.py**（它不碰巨潮检索）——")
        print("    换一天重跑；连着两天两边都是 0，再往「检索接口变了」上想。")

    # 九次检索的结果**互相重叠**（一条标题同时含「同意注册」和「可转换公司债券」
    # 就会被数两次），所以归类前先按去标签后的标题去重，去重前后的数都印出来。
    c_title = None
    stages, stages_raw = Counter(), Counter()
    seen = set()
    # 这两个要带出函数（decide() 的判据①用），所以在 if 之外初始化 ——
    # `stages` 为空（一条标题都没归到类）时它们保持 0/空，
    # 而 `searched` 那一位会把「没问着」和「问了没有」分开，不会混成同一个 0。
    appr_cb_titles: list = []
    n_appr = 0
    for kw, (tag, df) in titles_by_kw.items():
        if df is None or df.empty:
            continue
        c_title = c_title or _pick(df, ["公告标题", "标题", "announcementTitle"])
        if c_title is None:
            continue
        for t in df[c_title]:
            clean = strip_html(t)
            stages_raw[classify_stage(clean)] += 1
            if clean in seen:
                continue
            seen.add(clean)
            stages[classify_stage(clean)] += 1

    if stages:
        print(f"\n  ── 标题按流程阶段归类（**证据分组，不是结论**）──")
        print(f"  九次检索共回 {sum(stages_raw.values())} 条，"
              f"去重后 {sum(stages.values())} 条唯一标题。下表按**去重后**算。")
        for name in _STAGE_ORDER:
            if stages.get(name):
                print(f"    {name:<18}{stages[name]:>5} 条"
                      f"　（去重前 {stages_raw.get(name, 0)}）")
        print("  注意「终止/失效（反向）」判在「获批」**之前** ——")
        print("  「不予注册」含「注册」、「注册批复到期失效」含「注册批复」，"
              "顺序反了方向就反了。")

        # 这一栏要的是**转债**的获批，而检索是全市场的：定增（向特定对象发行股票）
        # 的注册批复长得几乎一样。不筛出来的话，「获批 N 条」这个数会大得没有意义。
        cb_words = ("可转换公司债券", "可转债", "转换公司债券")
        for t in seen:
            if classify_stage(t) == "获批（注册/核准）":
                n_appr += 1
                if any(w in t for w in cb_words):
                    appr_cb_titles.append(t)
        n_appr_cb = len(appr_cb_titles)
        print(f"\n  ── 「获批」档里有多少是**转债**的（其余多为定增）──")
        print(f"    获批档合计 {n_appr} 条，其中标题含「可转债/可转换公司债券」"
              f"{n_appr_cb} 条")
        print("    这个比例决定公告检索这条路要不要先过一道转债筛 —— "
              "**它是个计数，不是结论**。")
        if "可转换公司债券" in paged:
            # v5.6 更正：§6.4 原来那句「少掉的整页 → 分子比分母更吃亏 → 这个比例不稳」
            # **被 probe6 证伪了**。probe5 原始 13328 条、probe6 原始 12818 条（差 510），
            # 而两次**去重后都是 4286 条唯一标题、获批 133、含转债 22**，一个不差 ——
            # 差的那 510 条全是重复行，对唯一集合没有贡献。所以这个比例**是稳的**。
            # 但它仍然不能当总体比例用，原因换了：样本被**服务端封顶**截在 3000 条。
            print("    这个比例**跨轮是稳的**（probe4/5/6 三次都是 133 / 22，"
                  "尽管原始行数一次比一次不同）——")
            print("      因为差出来的那些行全是**重复行**，对唯一集合没有贡献。"
                  "（§6.4 原来说它「不稳」，那句已被 probe6 证伪。）")
            print("    ⚠ 但它**仍然不是总体比例**：上面那两路被服务端截在 **3000 条**，")
            print("      窗口里实际有多少条不知道，**截掉的那部分有没有偏，也没量过**。"
                  "→ 当计数看，不当比例用。")

    for kw in _KW_POSITIVE + _KW_NEGATIVE:
        tag, df = titles_by_kw.get(kw, (None, None))
        if df is None or df.empty or c_title is None:
            continue
        print(f"\n  ── 「{kw}」前 5 条标题原文（已删 <em> 标签，文本一字未动）──")
        for t in df[c_title].head(5):
            clean = strip_html(t)
            print(f"    [{classify_stage(clean)}] {clean[:76]}")

    # ── B 方案要答的就是这一份：**谁获批了** ────────────────────────────
    # v5.8 之前这里只数了个数（22），名单本身从来没印出来过 ——
    # 「谁」答不上来的话，B 方案等于没答。标题**一字未动**（只删 <em> 标签）。
    #
    # **v5.8.1 更正**：这里原来印着「不从标题里猜正股代码 —— 要映射得另有出处，
    # 这一轮没有」。**那句话是错的，而且它把一个不存在的阻塞写成了真的。**
    # 这张表回的是 5 列：代码 / 简称 / 公告标题 / 公告时间 / 公告链接 ——
    # `scanner/sources/event_arb.py` 从 v4 起就在实盘用它的「代码」「简称」出条目。
    # 所以正股代码**一直都有出处**，就在同一张表的另一列里，根本不用从标题猜。
    # 教训：写「这一轮没有」之前，先去看一眼同一个接口别处怎么用的。
    if appr_cb_titles:
        print(f"\n  ── **获批的转债名单（{len(appr_cb_titles)} 条，B 方案的答案）** ──")
        print("    标题原文，只删了 <em> 标签。**标题里多数不含公司名** ——")
        print("    正股代码/简称在这张表的「代码」「简称」列里（event_arb 已在用），")
        print("    本探针只印标题，接线时直接取那两列，**不要从标题猜**。")
        for t in sorted(appr_cb_titles):
            print(f"    · {t[:88]}")
        if paged:
            print(f"    ⚠ **这是下限，不是全集**：{len(paged)} 路被服务端截在封顶之上，"
                  f"截掉的部分有没有偏没量过。")
        print("    ⚠ **上了名单 ≠ 值得埋伏** —— 这一栏给的是时点，不是方向（纪律 8）。")

    return titles_by_kw, {
        "searched": bool(hit_pos or hit_neg) and n_ok > 0,
        "n_cb": len(appr_cb_titles),
        "n_all": n_appr,
        "capped": list(paged),
        "uncountable": list(uncountable),
        "titles": list(appr_cb_titles),
    }


def probe_scale(issue_df, cov_df):
    """Q4：把「拟发行规模」的单位钉死。**这一问不做完，下一问不许算。**"""
    _hr("Q4  拟发行规模的单位是什么？（钉不住就不许算含权量）")
    if issue_df is None or cov_df is None:
        print("  ✗ 缺表，对不了。单位未钉住。")
        return None, False
    c_plan = _pick(issue_df, ["计划发行总量", "计划发行量"])
    c_code = _pick(issue_df, ["债券代码", "代码"])
    c_cov_code = _pick(cov_df, ["债券代码", "代码"])
    c_cov_size = _pick(cov_df, ["发行规模", "发行规模(亿元)"])
    print(f"  巨潮：{c_plan} / {c_code}　东财：{c_cov_size}（已知单位=亿元）/ {c_cov_code}")
    if not all((c_plan, c_code, c_cov_code, c_cov_size)):
        print("  ✗ 缺关键列，对不了。单位未钉住。")
        return None, False

    ref = {}
    for _, r in cov_df.iterrows():
        v = to_float(r.get(c_cov_size))
        if v:
            ref[norm_code(r.get(c_cov_code))] = v

    ratios = []
    for _, r in issue_df.iterrows():
        a = to_float(r.get(c_plan))
        b = ref.get(norm_code(r.get(c_code)))
        if a and b and b > 0:
            ratios.append(a / b)
    if not ratios:
        print("  ✗ 两张表没有一只对得上（代码交集为空），单位钉不住。")
        return None, False

    ratios.sort()
    med = ratios[len(ratios) // 2]
    lab = unit_label(med)
    print(f"  {len(ratios)} 只对上：比值（巨潮原始值 ÷ 东财亿元值）"
          f"最小 {ratios[0]:.4g}　中位 {med:.4g}　最大 {ratios[-1]:.4g}")
    print(f"  → 单位判定：**{lab}**")
    spread = ratios[-1] / ratios[0] if ratios[0] > 0 else float("inf")
    if spread > 5:
        print(f"  ⚠ 但最大/最小差了 {spread:.1f} 倍 —— 同一列里混着不同量纲？"
              "先别信这个中位数，把上面几行原始值拉出来看。")
        return med, False
    if lab.startswith("非常规"):
        print("  ⚠ 比值不落在 1 / 1e4 / 1e8 附近 —— 两列可能根本不是一回事。")
        return med, False
    print("  这一问单独立着，是因为它最可能出那种**整栏都错**的错：")
    print("  v4.6.1 深市配售单位放大 10 倍，就是一个没对过的单位假设。")
    return med, True


# ── Q5 里其实是**两个独立的子问题**，v5.3 那次只修了措辞，没修早退 ──
#
#   A「那四只在不在这两张表里」 —— 只要 issue_df / cov_df，**不碰行情**
#   B「总市值还是流通市值」     —— 才要 stock_zh_a_spot_em
#
# 原来 `spot is None` 就整个 Q5 早退，A 跟着一起被吞。后果是 probe3/4/5
# **连着三次**都没答成 A，而 A 的两个输入这三次**每次都取到了**
# （probe5：巨潮 44 行、东财 1047 行，全 ✓）。6.1 ③ 那个「缺一张待发债的规模来源」
# 因此三轮没被一次实跑验过 —— 卡在一个它**根本不需要**的接口上。
# 「少印可以，少说不可以」的另一面：**能答的那一半不许跟着没答的一起沉默**（纪律 5）。
#
# 另一处：`_T_ISSUE` 这张表**没有「正股简称/正股名称」列**（probe5 自己印的 31 列里
# 一个都没有，它给的是「转股代码」和「债券名称」）。所以按正股名找的那一支
# **永远走不到**，而原来那句话说的是「两张表都没有它的拟发行规模」——
# 把**没搜过**说成了搜过。缺列而跳过的表必须单独说，不许并进「都没找到」里。
def plan_size_lookup(issue_df, cov_df, bench, scale_to_yuan) -> tuple:
    """A 半问：在两张表里按正股名找拟发行规模。**纯表内查找，不碰行情。**

    返回 `(rows, searched, skipped)`：

    * `rows`     —— `[{"name", "plan_yuan", "src", "raw"}]`，顺序同 `bench`
    * `searched` —— **真的搜过**的表名
    * `skipped`  —— `[(表名, 原因)]`，缺列/缺表而没搜的。**不许并进「都没找到」**
    """
    plans = ((issue_df, _pick(issue_df, ["正股简称", "正股名称"]),
              _pick(issue_df, ["计划发行总量", "计划发行量"]), scale_to_yuan, _T_ISSUE),
             (cov_df, _pick(cov_df, ["正股简称", "正股名称"]),
              _pick(cov_df, ["发行规模"]), 1e8, _T_COV))

    searched, skipped = [], []
    for df, c_stock, c_size, _fac, src in plans:
        if df is None:
            skipped.append((src, "这张表没取到"))
        elif c_stock is None:
            skipped.append((src, "这张表没有「正股简称/正股名称」列，按正股名找不了"))
        elif c_size is None:
            skipped.append((src, "这张表没有拟发行规模那一列"))
        else:
            searched.append(src)

    rows = []
    for name in bench:
        hit = (None, None, None)
        for df, c_stock, c_size, unit_fac, src in plans:
            if df is None or c_stock is None or c_size is None:
                continue
            for _, r in df.iterrows():
                if str(r.get(c_stock, "")).strip() == name:
                    v = to_float(r.get(c_size))
                    if v:
                        hit = (v * (unit_fac or 0), src, r.get(c_size))
                        break
            if hit[0] is not None:
                break
        rows.append({"name": name, "plan_yuan": hit[0], "src": hit[1], "raw": hit[2]})
    return rows, searched, skipped


def report_plan_sizes(rows, searched, skipped) -> None:
    """把 A 半问印出来。**行情取到没取到都要印这一段。**"""
    print("\n  ── A 半问：那四只的拟发行规模在这两张表里找得到吗（**不需要行情**）──")
    for src, why in skipped:
        print(f"    ⚠ {src}：**没搜** —— {why}")
    print(f"    真正搜过的表：{ ' / '.join(searched) if searched else '**一张都没搜到**' }")
    n_hit = sum(1 for r in rows if r["plan_yuan"] is not None)
    for r in rows:
        if r["plan_yuan"] is None:
            print(f"    {r['name']:<10}✗ 搜过的表里没有它的拟发行规模")
        else:
            print(f"    {r['name']:<10}✓ {r['src']}（原始 {r['raw']!r}）")
    if not searched:
        print("    → 一张表都没搜成，**这半问也没答** —— 不是「不在表里」（纪律 5）。")
    elif n_hit == 0:
        print(f"    → {len(rows)} 只在**搜过的表**里一只都没有。这两张表装的都是"
              "**已公告发行**的债，")
        print("      而名单上的正是「已获批、还没发行」的 —— 结构上就不该在里面。")
        print("      → 这不是口径错，是**缺一张待发债的规模来源**（6.1 ③）。")
        if skipped:
            print("      ⚠ 但上面那张**没搜**的表不算进这个结论 —— 它一次都没被查过。")


def probe_bench(ak, issue_df, cov_df, bench: dict, scale_to_yuan, today: date):
    """Q5：验收 —— 复现 08-09 日报那四只的含权量。"""
    _hr("Q5  验收：复现 08-09 日报的四只（数对得上说明口径推对了）")
    print("  含权量 = 拟发行规模 ÷ 总市值 —— 是恒等式换了个写法，不是新模型：")
    print("    含权量 = 每股配售额 ÷ 正股价，而 每股配售额 = 拟发行规模 ÷ 总股本")
    print("  但它比 cb_allotment 那一栏**多一个假设**：原股东可优先配售比例 100%。")
    print(f"  候选口径**事先只注册两个**：{ ' / '.join(_CALIB) }。"
          "两个都算、都印残差，不再往下搜第三个。")

    # A 半问先答 —— 它不需要行情，所以**不许被行情的失败带着一起沉默**。
    plan_rows, searched, skipped = plan_size_lookup(
        issue_df, cov_df, bench, scale_to_yuan)
    report_plan_sizes(plan_rows, searched, skipped)
    plan_by_name = {r["name"]: r for r in plan_rows}

    print("\n  ── B 半问：口径复现（**这一半才要行情**）──")
    spot = _fetch(ak, _T_SPOT)
    calib = {k: [] for k in _CALIB}
    if spot is None:
        print(f"  ✗ {_T_SPOT} 没取到，总市值拿不到 —— **B 这一半没答**。")
        print("    没答 ≠ 口径对不上（纪律 5）。**别把这一次记成「口径被证伪」** ——")
        print("    这一次没有证伪任何口径，它根本没被检验到。")
        # v5.8：原来这里写的是「结论里会把这两件事分开说」，而 v5.8 之后
        # decide() 已经不看 calib 了 —— 那句话会变成一张空头支票（§6.8 ⑤ 第二种形状）。
        # 所以这个区分改由**本函数自己就地说清**，不再往下游甩。
        print("    （这一段**不进退出码** —— v5.8 起判的是 B 方案那两条，见 §6.9。）")
        print("    A 那一半在上面已经答了 —— 两半的成败**分开记**。")
        return calib, False

    c_nm = _pick(spot, ["名称"])
    c_cd = _pick(spot, ["代码"])
    mcap = {}
    for _, r in spot.iterrows():
        nm = str(r.get(c_nm, "")).strip()
        if nm:
            mcap[nm] = {"代码": str(r.get(c_cd, "")).strip(),
                        "总市值": to_float(r.get("总市值")),
                        "流通市值": to_float(r.get("流通市值")),
                        "最新价": to_float(r.get("最新价"))}

    print(f"\n  {'正股':<10}{'日报值':>7}{'拟发行规模来源':<24}"
          f"{'总市值口径':>11}{'残差':>8}{'流通市值口径':>13}{'残差':>8}")
    for name, ref in bench.items():
        info = mcap.get(name)
        _p = plan_by_name.get(name, {})
        plan_yuan, src, raw = _p.get("plan_yuan"), _p.get("src"), _p.get("raw")
        if info is None:
            print(f"  {name:<10}{ref:>7.1f}  ✗ 全 A 行情里没有这只正股（改名/停牌？）")
            for k in _CALIB:
                calib[k].append({"name": name, "calc": None, "ref": ref, "diff": None})
            continue
        if plan_yuan is None:
            print(f"  {name:<10}{ref:>7.1f}  ✗ 两张表都没有它的拟发行规模 —— "
                  f"待发债在这两张表里**看不到**，这本身就是答案")
            for k in _CALIB:
                calib[k].append({"name": name, "calc": None, "ref": ref, "diff": None})
            continue
        line = f"  {name:<10}{ref:>7.1f}  {src}(原始 {raw!r})".ljust(52)
        for k in _CALIB:
            w = equity_weight_pct(plan_yuan, info.get(k))
            d = None if w is None else w - ref
            calib[k].append({"name": name, "calc": w, "ref": ref, "diff": d})
            line += (f"{w:>10.2f}{d:>+8.2f}" if w is not None else f"{'—':>10}{'—':>8}")
        print(line)

    print(f"\n  容忍度 ±{_TOL_PCT} 个百分点（日报印的是整数，本身就带 ±0.5），"
          f"要求 ≥{_MIN_HIT}/{len(bench)} 只。")
    print("  两个口径都算残差，是为了让**数据**选口径；"
          "但只在这两个里选 —— 再往下搜就是拿四个已知数凑口径了。")
    print("  另注：总市值取的是**今天**的最新价，日报是 08-09 的，"
          "隔夜价格漂移会吃掉一点残差预算。")

    n_noplan = sum(1 for r in calib[_CALIB[0]] if r["calc"] is None)
    if n_noplan:
        where = " / ".join(searched) if searched else "（一张表都没搜成）"
        print(f"\n  ⚠ {n_noplan}/{len(bench)} 只**没算成**，"
              f"因为在 {where} 里找不到它的拟发行规模。")
        for src, why in skipped:
            print(f"    （{src} **没搜**：{why} —— 它不算进下面这个结论）")
        print("    搜过的这些表装的都是**已公告发行**的债，而验收名单上的正是"
              "「已获批、还没发行」的 —— 结构上就不该在里面。")
        print("    → 这不是口径错，是**缺一张待发债的规模来源**。"
              "在补上它之前，Q5 这条验收路走不通。")
    return calib, True


# ═══════════════════════════ 离线自测 ═══════════════════════════

def selftest() -> int:
    """纯函数离线自测。联网前先跑这个 —— 语法/逻辑错不用等到联网才发现。"""
    n = [0]

    def ok(msg):
        n[0] += 1
        print(f"  [PASS] {msg}")

    assert norm_code("113065.SH") == "113065"
    assert norm_code(" 113065 ") == "113065"
    assert norm_code(113065) == "113065"
    ok("代码归一：去后缀去空白，取末尾 6 位")

    # 含权量恒等式：两条路必须给同一个数
    plan, shares, price = 6e8, 3e8, 8.0            # 6 亿规模 / 3 亿股 / 8 元
    per_share = plan / shares                       # 每股配售额 = 2.0 元
    assert abs(equity_weight_pct(plan, shares * price) - per_share / price * 100) < 1e-9
    assert abs(equity_weight_pct(plan, shares * price) - 25.0) < 1e-9
    ok("含权量恒等式：规模÷总市值 与 每股配售额÷正股价 给同一个数")

    assert equity_weight_pct(None, 1e9) is None
    assert equity_weight_pct(1e8, 0) is None
    assert equity_weight_pct(1e8, None) is None
    ok("含权量：缺任一边或市值为 0 时返回 None，不返回 0（0 会被当成「没含权」）")

    assert abs(breakeven_premium_pct(25.0) - 4.0) < 1e-9
    assert breakeven_premium_pct(0) is None
    ok("打平溢价 = 100 ÷ 含权量，含权量为 0 时不除零")

    assert unit_label(1.0) == "亿元"
    assert unit_label(1e4) == "万元"
    assert unit_label(1e8) == "元"
    assert unit_label(None) == "判不了"
    assert unit_label(1e2).startswith("非常规")
    ok("单位判定：比值 1 / 1e4 / 1e8 各对一个单位，落在别处明说非常规")

    # ★ 本轮的核心取证：反向写法必须排在获批之前
    assert classify_stage("关于可转换公司债券发行申请获中国证监会同意注册的公告") == "获批（注册/核准）"
    assert classify_stage("关于公司申请不予注册的公告") == "终止/失效（反向）"
    assert classify_stage("关于向不特定对象发行可转债注册批复到期失效的公告") == "终止/失效（反向）"
    assert classify_stage("关于终止向不特定对象发行可转换公司债券的公告") == "终止/失效（反向）"
    assert classify_stage("关于中止审查可转换公司债券发行申请的公告") == "终止/失效（反向）"
    ok("阶段分类先排反向写法：不予注册 / 到期失效 / 终止 / 中止 都不算「获批」")

    assert classify_stage("可转换公司债券发行公告") == "发行/申购"
    assert classify_stage("可转债发行申请获上市委审核通过的公告") == "过会/审核通过"
    assert classify_stage("关于收到审核问询函的公告") == "在审（受理/问询）"
    assert classify_stage("董事会决议公告") == "预案/董事会"
    assert classify_stage("关于变更注册地址的公告") == "其他（未分类）"
    ok("其余各档各钉一条，认不出的进「其他」而不是硬塞")

    t = date(2026, 8, 10)
    assert is_pre_issue(None, t)                       # 空 = 还没定，进埋伏档
    assert is_pre_issue("2026-09-01", t)
    assert not is_pre_issue("2026-08-01", t)
    assert not is_pre_issue("2026-08-10", t)           # 今天申购，已经不是埋伏了
    ok("埋伏档判据：空 / 未来算进，今天和过去不算")

    assert lead_days("2026-06-10", "2026-08-10") == 61
    assert lead_days(None, "2026-08-10") is None
    ok("提前量 = 申购日 − 公告日，缺任一边返回 None")

    # ── 退出码：B 方案的判据（v5.8 换的问题，判据跟着换）────────────────
    def _appr(n_cb=22, n_all=133, searched=True, capped=(), uncountable=()):
        return {"searched": searched, "n_cb": n_cb, "n_all": n_all,
                "capped": list(capped), "uncountable": list(uncountable)}

    code, _ = decide(False, None)
    assert code == 2
    code, why = decide(True, _appr(capped=["可转换公司债券"]))
    assert code == 0, why
    code, why = decide(True, _appr(n_cb=1))            # 名单太短
    assert code == 1 and any("不足" in w for w in why), why
    # 撞了顶又数不出唯一数 → 「下限」没有出处 → 退 1
    code, why = decide(True, _appr(capped=["可转换公司债券"],
                                   uncountable=["可转换公司债券"]))
    assert code == 1 and any("无从说起" in w for w in why), why
    ok("退出码判据（B 方案）：名单+截断限制都过→0，任一不过→1，表拿不到→2")

    # 检索没通时那个 0 的含义是「没问着」，不是「没有」——
    # 这是 probe3 栽过的那一处，换了问题之后它换了个位置又出现了。
    code, why = decide(True, _appr(n_cb=0, n_all=0, searched=False))
    assert code == 1, why
    assert any("检索本身没通" in w for w in why), why
    assert not any("这个窗口里真的就这么少" in w for w in why), why
    # Q3 整段没跑成，和「跑了但没有」同样不许混
    code, why = decide(True, None)
    assert code == 1 and any("没跑成" in w for w in why), why
    assert any("不等于「没有获批的债」" in w for w in why), why
    ok("「没问着」「问了太少」「整段没跑成」三种 0 分开说，不许互相冒充")

    code, why = decide(True, _appr(n_cb=1))
    assert any("不等于这条路死了" in w for w in why), why
    ok("退 1 时说清是哪一项、且不把「这个窗口少」说成「这条路死了」")

    # ★ B 方案的命根子：**答得上来也只是下限**，不是全集。
    #   这一条钉的是 §6.5 当初写下的 B 风险 —— 只给名单不给含权量，
    #   读者容易把「上了名单」读成「值得埋伏」，那就滑向纪律 8 了。
    code, why = decide(True, _appr(capped=["可转换公司债券"]))
    assert code == 0
    blob = "".join(why)
    assert "下限，不是全集" in blob, why
    assert "不许说「获批的就这些」" in blob, why
    assert "上了名单 ≠ 值得埋伏" in blob, why
    assert "纪律 8" in blob, why
    # 没撞顶那一支同样不许把「没迹象」说成「拿到了全量」
    code, why = decide(True, _appr())
    blob0 = "".join(why)
    assert code == 0 and "不等于「拿到了全量」" in blob0, why
    ok("B 方案答得上来时也只报下限：不许说成全集，且带上「上了名单≠值得埋伏」")

    # A 方案那三个数已经**不参与判定**了 —— 判据里不许再出现口径复现的话术。
    for _c in (_appr(), _appr(n_cb=1), _appr(searched=False, n_cb=0, n_all=0)):
        for line in decide(True, _c)[1]:
            assert "口径复现" not in line and "只落在 ±" not in line, line
    ok("含权量口径复现已从判据里整段拿掉，没有留在别处当暗门")

    # ★ 以下三条是 probe3 第一次跑之后补的，钉的都是那一次踩到的东西

    # 1) 巨潮把命中的每个词各裹一层 <em>，四字关键词会被劈开。
    #    危险的是它**有方向**：反向写法两字、裹了照样命中，正向写法四字、全被劈开。
    em_appr = "关于向不特定对象发行可转换公司债券申请获得中国证监会<em>同意</em><em>注册</em>批复的公告"
    em_dead = "关于向不特定对象发行可转债注册批复<em>到期</em><em>失效</em>的公告"
    assert classify_stage(em_appr) == "获批（注册/核准）", classify_stage(em_appr)
    assert classify_stage(em_dead) == "终止/失效（反向）"
    # 去掉标签后判定不许变（strip_html 只删标签、不动文本）
    assert classify_stage(strip_html(em_appr)) == classify_stage(em_appr)
    assert classify_stage(strip_html(em_dead)) == classify_stage(em_dead)
    ok("标题带 <em> 高亮标签时分类不被打穿：正向四字词劈开后仍判「获批」")

    # 2) 「行情没取到 ≠ 口径对不上」这一条**换了归属**：v5.8 起 decide() 不看
    #    calib 了，所以这个区分由 probe_bench 自己就地说清。原来那句
    #    「结论里会把这两件事分开说」在换判据之后会变成空头支票（§6.8 ⑤ 第二种形状），
    #    所以这里钉的是**源码**：那句承诺必须已经被就地说明取代。
    #    针照第 13 条的老办法拼出来，免得这几行自己把自己切出来当样本。
    _self_src = open(__file__, encoding="utf-8").read()
    _mark = "def " + "probe_bench"
    _bench_seg = _self_src.split(_mark)[1].split("\ndef ")[0]
    _stale = "结论里会把这两件事" + "分开说"
    for _ln in _bench_seg.splitlines():
        # 历史叙述放行（照第 15 条的老办法），**印出去的那一句不放行** ——
        # 空头支票的害处在于它被印给读者看，写在注释里当史料是对的。
        if _stale in _ln and "原来" not in _ln:
            raise AssertionError(
                f"probe_bench 还在把区分甩给 decide()，而 decide() 已经不看 calib 了："
                f"{_ln.strip()[:60]}")
    assert "别把这一次记成「口径被证伪」" in _bench_seg, _bench_seg[:200]
    assert "不进退出码" in _bench_seg, _bench_seg[:200]
    ok("行情没取到时由 probe_bench 自己说清「没答≠被证伪」，不甩给已经不看它的 decide()")

    # 3) 提前量那条老结论**降级成附注**：B 方案里 Q1 已经不是判据（§6.6 ② 关门），
    #    但「换一天从来不是扩样本的路子」这个教训不许丢 —— 丢了下一轮又会有人去换一天。
    short = {"n": 44, "max": 5, "n_long": 0}       # 全 ≤7 天：换哪天都一样
    code, why = decide(True, _appr(), lead_stats=short)
    assert code == 0, why                          # 它不再影响退出码
    assert any("换一天跑从来就不是扩样本的路子" in w for w in why), why
    long_ = {"n": 44, "max": 62, "n_long": 9}      # 有够长的：那就不提这句
    code, why = decide(True, _appr(), lead_stats=long_)
    assert not any("扩样本" in w for w in why), why
    ok("提前量降级成附注：不再判退出码，但「换一天不是扩样本的路子」那句留着")

    # 4) 整页数标记：钉住 probe3/probe4 那一次同日不一致的真实数字。
    #    3810 → 3270 差整 18 页，另外八路一字不差。
    assert page_bound_note(3810) is not None
    assert page_bound_note(3270) is not None
    assert page_bound_note(8880) is not None
    assert (3810 - 3270) % _PAGE_SIZE == 0 and (3810 - 3270) // _PAGE_SIZE == 18
    for n_small in (198, 235, 182, 11, 5, 0):          # 那八路里的真实命中数
        assert page_bound_note(n_small) is None, n_small
    assert page_bound_note(60) is None                 # 300 以下不标，%30 更可能是巧合
    ok("整页数标记：>300 且是 30 的整数倍才提醒，实测的小命中数一个都不误报")

    # 5) 措辞：v5.6 起**不许再说方向**。原来那句「可能是下界」被 probe6 证伪了 ——
    #    唯一数 3000（=100 整页）小于行数，说明越过封顶的页回的是重复行，行数偏**大**。
    #    「下界」这个词现在只许用在**唯一数**上，不许用在行数上。
    note = page_bound_note(3270)
    assert "整页" in note and "不是计数" in note, note
    assert "下界" not in note and "上界" not in note, note      # 行数不许贴方向
    assert "截断" not in note and "丢页" not in note, note      # 机制的字面也不硬塞进这一行
    ok("整页数那一行只说「不是计数」，不贴方向 —— 「下界」那句 v5.6 已证伪")

    # 6) 重定向到文件时 stdout 走 GBK —— probe5 就死在第一个 ✓ 上。
    assert stream_fix_plan("gbk", is_tty=False)[0] == "utf-8"        # 正是 probe5 那一幕
    assert stream_fix_plan("cp936", is_tty=False)[0] == "utf-8"
    assert stream_fix_plan(None, is_tty=False)[0] == "utf-8"         # 编码问不出来也当要修
    assert stream_fix_plan("gbk", is_tty=True)[0] is None            # 控制台不动它的编码
    assert stream_fix_plan("utf-8", is_tty=False)[0] is None         # 已是 UTF-8 不折腾
    assert stream_fix_plan("UTF8", is_tty=False)[0] is None
    ok("重定向到文件时把流改成 UTF-8；控制台和已是 UTF-8 的流一个字节都不动")

    # 7) errors 策略只许 backslashreplace：'replace' 会把 ✓ 和 ✗ 一起变成 ?，
    #    而这两个符号是「取到 / 没取到」的唯一区分（纪律 5）。
    for enc, tty in (("gbk", False), ("gbk", True), ("utf-8", False)):
        assert stream_fix_plan(enc, tty)[1] == "backslashreplace", (enc, tty)
    assert "\u2713".encode("ascii", "backslashreplace") != \
           "\u2717".encode("ascii", "backslashreplace")   # 降级了也还分得开
    ok("兜底用 backslashreplace 不用 replace：✓ 和 ✗ 降级之后仍然分得开")

    # ★ 以下四条是 probe5（第三次跑）读完之后补的

    # 8) 存档提示里**一个具体文件名都不许有**。这一处腐烂的后果是销毁证据：
    #    产出 probe5 的那次跑动，自己印的还是「存档命名：docs/probes/probe4.txt」。
    hint = archive_hint()
    assert not re.search(r"probe\d*\.txt", hint), hint
    assert "probeN.txt" in hint and "不许覆盖" in hint, hint
    for line in decide(True, _appr(capped=["可转换公司债券"]))[1]:  # 退出码 0 那一支也不许有
        assert not re.search(r"probe\d*\.txt", line), line
    ok("存档提示只印规则不印编号：输出里没有任何写死的 probeN.txt 文件名")

    # 9) A 半问（拟发行规模在不在表里）**不许依赖行情**，而且
    #    「缺列没搜过」不许被并进「搜过但没找到」——那是把没搜说成搜了（纪律 5）。
    import pandas as _pd
    issue_no_stockcol = _pd.DataFrame({"债券代码": ["111026"],       # 巨潮那张表的真实形状：
                                       "计划发行总量": [158000.0],   # 有规模列，**没有正股简称列**
                                       "转股代码": ["605123"]})
    cov_with = _pd.DataFrame({"正股简称": ["派克新材"], "发行规模": [15.8]})
    rows, searched, skipped = plan_size_lookup(
        issue_no_stockcol, cov_with, {"四方科技": 23.0}, 1e4)
    assert searched == [_T_COV], searched                    # 只搜了东财这一张
    assert len(skipped) == 1 and skipped[0][0] == _T_ISSUE, skipped
    assert "正股简称" in skipped[0][1], skipped              # 说清为什么没搜
    assert rows[0]["plan_yuan"] is None                      # 四方科技确实不在里面
    ok("A 半问不碰行情；缺列而没搜的表单独说，不算进「搜过也没找到」")

    # 10) 单位换算和取值来源要跟着走：东财那张表按亿元、巨潮按 scale_to_yuan。
    rows2, searched2, _ = plan_size_lookup(
        issue_no_stockcol, cov_with, {"派克新材": 1.0}, 1e4)
    assert rows2[0]["src"] == _T_COV and abs(rows2[0]["plan_yuan"] - 15.8e8) < 1
    issue_with = _pd.DataFrame({"正股简称": ["某正股"], "计划发行总量": [158000.0]})
    rows3, searched3, skipped3 = plan_size_lookup(
        issue_with, None, {"某正股": 1.0}, 1e4)
    assert searched3 == [_T_ISSUE] and skipped3[0][0] == _T_COV
    assert abs(rows3[0]["plan_yuan"] - 158000.0 * 1e4) < 1   # 万元 → 元
    ok("拟发行规模按各表自己的单位换算，来源如实标出；表没取到也单独说")

    # 11) 重复行按**公告链接**数，不按标题 —— 不同公司的公告标题大量重名。
    #     这一项是用来分辨「整页数」是下界还是上界的，所以「数不了」不许冒充「没重复」。
    same_title = "关于向特定对象发行股票方案到期失效的公告"
    df_dup = _pd.DataFrame({"公告标题": [same_title] * 3,
                            "公告链接": ["a", "b", "c"]})     # 三家公司、同一个标题
    assert dup_rows(df_dup, "公告链接") == (3, 3, 0)          # 按链接：一条重复都没有
    assert dup_rows(df_dup, "公告标题") == (3, 1, 2)          # 按标题：误判成两条重复
    df_real = _pd.DataFrame({"公告链接": ["a", "b", "a"]})
    assert dup_rows(df_real, "公告链接") == (3, 2, 1)
    assert dup_rows(df_real, "缺这列") == (3, None, None)     # 数不了就说数不了
    assert dup_rows(None, "公告链接") == (0, None, None)
    ok("重复行按公告链接数；「数不了」返回 None 不返回 0（0 会被读成「没重复」）")

    # 12) 早退回归：行情失败时 A 半问**必须照样印出来**。
    #     这是 probe3/4/5 连着三次没答成 A 的那个早退，纯函数测不到它 ——
    #     它是 probe_bench 的控制流，所以在这里离线跑一遍真正的函数。
    import contextlib
    import io as _io

    class _NoSpotAk:
        def stock_zh_a_spot_em(self):
            raise ConnectionError("RemoteDisconnected")   # 正是那三次的报错

    _buf = _io.StringIO()
    with contextlib.redirect_stdout(_buf), \
            contextlib.redirect_stderr(_io.StringIO()):   # 这次失败是故意的，别刷屏
        _calib, _spot_ok = probe_bench(
            _NoSpotAk(), issue_no_stockcol, cov_with, {"四方科技": 23.0},
            1e4, date(2026, 8, 11))
    _out = _buf.getvalue()
    assert _spot_ok is False                              # 退出码逻辑一个字没变
    assert not _calib[_CALIB[0]]                          # 没算就是没算，不填 0
    assert "A 半问" in _out and "B 半问" in _out, _out     # 两半都要出现
    assert "四方科技" in _out, _out                        # A 的结果真的印了
    assert "**没搜**" in _out, _out                        # 缺列那张表单独说
    _a, _b = _out.index("A 半问"), _out.index("B 半问")
    assert _a < _b, "A 半问必须印在取行情之前，否则行情一失败它又被吞掉"
    ok("行情失败时 A 半问照样答：两半分开印，A 印在取行情之前（钉住那个早退）")

    # ★ 以下两条是 probe6 / probe7 读完之后补的

    # 13) **每个 diag_*.py 都得接上流编码修复。** v5.5 只修了本文件，
    #     §9 写了一句「别的还没修，留意」—— v5.6 那轮 diag_sources.py 就照原样崩了，
    #     整次跑动白费（docs/probes/probe7.txt 是现场）。把坑写进文档不算修好，所以这里钉住它。
    import glob as _glob
    import os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _diags = sorted(_os.path.basename(f) for f in _glob.glob(_os.path.join(_here, "diag_*.py"))
                    if _os.path.basename(f) != "diag_common.py")
    assert len(_diags) >= 6, _diags
    for _f in _diags:
        _src = open(_os.path.join(_here, _f), encoding="utf-8").read()
        assert "from diag_common import" in _src, f"{_f} 没接 diag_common"
        assert "apply_stream_fix()" in _src, f"{_f} 没调 apply_stream_fix()"
        # 老那种「自己动一把流」的写法一律不许再出现 —— diag_lof_coverage 原来就是那样，
        # 而且用的是被禁的降级策略（✓ 和 ✗ 会一起变成 ?）。
        # 针不写成字面量，免得这一行自己把自己判成违规。
        _needle = "sys.stdout." + "reconfigure"
        assert _needle not in _src, f"{_f} 还在自己改流，应该走 diag_common"
    ok(f"{len(_diags)} 个 diag_*.py 全部接上 diag_common，没有谁再自己改流")

    # 14) `--spot-check` 是给那个从没通过的 host 用的。原来指的 diag_sources.py
    #     **根本不测它** —— 这一条同时钉住「那条指令别再指错地方」。
    _src_srcs = open(_os.path.join(_here, "diag_sources.py"), encoding="utf-8").read()
    assert _T_SPOT not in _src_srcs, "diag_sources.py 不测这个接口，别再往那儿指"
    assert "spot_check" in globals() or callable(spot_check)
    ok(f"{_T_SPOT} 的连通性检查在本文件（--spot-check）；diag_sources.py 确实不测它")

    # 15) v5.7：§6.7 ④ 宣布「已修：检查放到依赖它的地方」，**而代码里没修** ——
    #     `decide()` 和另外三处仍在把人往那个测不到这些 host 的脚本上指，
    #     等于下一次跑动会把那条错了四轮的指令原样再印一遍。
    #     加 `--spot-check` 是**新增**了一条对的路，没有**拔掉**那条错的路。
    #     这一条钉的就是「拔掉了」。
    #     针照第 13 条的老办法拼出来，免得这几行自己把自己判成违规。
    _src_self = open(_os.path.join(_here, "diag_cbplan.py"), encoding="utf-8").read()
    _verb = ("先" + "跑", "去" + "跑")
    _obj = "diag_" + "sources"
    for _ln in _src_self.splitlines():
        if any((v + " python " + _obj) in _ln or (v + " " + _obj) in _ln
               for v in _verb):
            assert "别" in _ln or "原来" in _ln, f"又把人往那个脚本上指了：{_ln.strip()[:60]}"
    ok("本文件不再把人往那个测不到这些 host 的脚本上指（错了四轮的指令，v5.7 从代码里拔掉）")

    print(f"\n全部通过（{n[0]} 条）。")
    return 0


# ═══════════════════════════ 入口 ═══════════════════════════

def _parse_bench(s):
    out = {}
    for part in str(s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            print(f"  ⚠ --bench 里「{part}」没有 = 号，跳过")
            continue
        k, v = part.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            print(f"  ⚠ --bench 里「{part}」的值不是数，跳过")
    return out


# `stock_zh_a_spot_em` 是 Q5 的 B 半问唯一的输入，**到 v5.7 为止一次都没通过**：
# probe3/4/5/6 四次跑动，每次 `_fetch` 重试 2 次 = 8 次调用全失败；
# v5.7 的 `--spot-check` 又 2 次，累计 10 次。每次都是
# `ConnectionError: RemoteDisconnected`，而同一次跑动里别的接口全通。
# （旧稿写的「四次跑动挂六次」在 probe3-6 里数不出来，v5.7 对账时按实测改掉。）
#
# 原来 STATE / §6.3 让你「先跑 diag_sources.py 看这个 host 通不通」—— **那条指令是错的**：
# `diag_sources.py` 测的是 fund_premium 依赖的三个**基金**接口，
# 它从头到尾没碰过 `stock_zh_a_spot_em`（v5.6 才发现，`grep -c` = 0）。
# 而 probe7 那次跑动更是连第一张表都没走完就崩了（GBK，见 diag_common）。
#
# 所以检查放在**依赖它的这个文件里**。顺便也就近说清一件事：
# **`scanner/` 里没有任何模块用它** —— 这个 host 挂了不影响实盘，只堵住 Q5 的 B 半问。
def spot_check() -> int:
    """只打一次 `stock_zh_a_spot_em`，报通不通。**不做任何别的事。**"""
    print("=" * 68)
    print(" stock_zh_a_spot_em 单接口连通性检查")
    print("=" * 68)
    print("  它只被 diag_cbplan 的 Q5（B 半问）用；**scanner/ 里没有人用它**，")
    print("  所以这一项挂掉不影响实盘，只让「总市值 / 流通市值」那个口径没法验。")
    try:
        import akshare as ak
    except ImportError:
        print("  ✗ 未安装 akshare：pip install -U akshare")
        return 2
    print(f"  akshare {getattr(ak, '__version__', '?')} | "
          f"python {sys.version.split()[0]}")
    df = _fetch(ak, _T_SPOT)
    if df is None:
        print("  → 没取到。**这不等于「口径对不上」**，也不等于「A 股行情拿不到」——")
        print("    只等于这一个接口这一次没回来（纪律 5）。")
        print("    要往下走有两条路，都得你拍板：换一个带总市值的接口，"
              "或者把 B 半问的口径改成不需要总市值的写法。")
        return 1
    cols = [str(c) for c in df.columns]
    want = [c for c in cols if c in ("总市值", "流通市值", "最新价")]
    print(f"  → 通了：{len(df)} 行 × {len(cols)} 列，"
          f"其中 Q5 要的列到齐了 {len(want)}/3：{want}")
    if len(want) == 3:
        print("  → 三列都在，**下一次跑 diag_cbplan.py 时 B 半问就能答**。")
        return 0
    print("  ⚠ 列不全 —— 接口通了不等于有用。缺的那几列名字可能改版了。")
    return 1


def main() -> int:
    apply_stream_fix()          # 必须在**任何 print 之前**：probe5 就是倒在第一个 ✓ 上
    ap = argparse.ArgumentParser(description="配债触发点前移探针（阶段 3）")
    ap.add_argument("--days", type=int, default=180, help="往回看多少天（默认 180）")
    ap.add_argument("--rows", type=int, default=10, help="印几行原始数据（默认 10）")
    ap.add_argument("--bench", default=None,
                    help="验收名单，形如 四方科技=23,千红制药=11（只在 --with-bench 下有用）")
    ap.add_argument("--with-bench", action="store_true",
                    help="跑 Q5 含权量口径复现（A 方案遗留，默认不跑，见 §6.9）")
    ap.add_argument("--selftest", action="store_true", help="纯函数离线自测，不联网")
    ap.add_argument("--spot-check", action="store_true",
                    help="只打一次 stock_zh_a_spot_em，报通不通（Q5 的 B 半问就卡在它）")
    args = ap.parse_args()

    if args.spot_check:
        return spot_check()

    if args.selftest:
        print("=" * 68)
        print(" diag_cbplan 纯函数自测（不联网）")
        print("=" * 68)
        return selftest()

    bench = _parse_bench(args.bench) if args.bench else dict(_BENCH)
    today = date.today()

    print("=" * 68)
    print(" 配债触发点前移探针　—— 只答「能不能在发行公告之前就看到它」")
    print("=" * 68)
    try:
        import akshare as ak
    except ImportError:
        print("\n✗ 没装 akshare。pip install -U akshare")
        return 2
    print(f"akshare {getattr(ak, '__version__', '?')} | "
          f"python {sys.version.split()[0]} | 今天 {fmt_date(today, '%Y-%m-%d')}")
    print("本探针**不产出机会条目、不接进 scanner/**（纪律 1）。它只回答问题。")

    issue_df, n_dated, n_blank, lead_stats = probe_issue_table(ak, args.days, args.rows, today)
    probe_ths(ak, args.rows, today)
    _titles, appr = probe_announcements(ak, args.days, today)

    _hr(f"取 {_T_COV}（东财转债总表，用来钉单位 + 兜底找拟发行规模）")
    cov_df = _fetch(ak, _T_COV)

    scale, scale_known = probe_scale(issue_df, cov_df)
    scale_to_yuan = None
    if scale_known:
        lab = unit_label(scale)
        scale_to_yuan = {"亿元": 1e8, "万元": 1e4, "元": 1.0}.get(lab)
        print(f"  → 换算：原始值 × {scale_to_yuan:g} = 元")

    # Q5（含权量口径复现）**默认不跑** —— 选了 B 之后它已不在问题范围内（§6.9）。
    # 它唯一的输入 stock_zh_a_spot_em 到现在 10 次调用一次没通过，默认跑它
    # 等于每轮花 1-3 分钟换回一份「这一问没答」。要跑就 --with-bench。
    if not args.with_bench:
        _hr("Q5  含权量口径复现：**这一轮不问**（B 方案，§6.9）")
        print("  选了 B 之后这一问已不在范围内 —— 它**不是失败，是没问**（纪律 5）。")
        print("  缺的是「已获批未发行」那张规模表，不是网络：换写法救不了，等源。")
        print(f"  真要问：`py -3.11 diag_cbplan.py --with-bench`"
              f"（先确认 --spot-check 通，否则 {_T_SPOT} 那一路照样是白跑）。")
    elif scale_known:
        probe_bench(ak, issue_df, cov_df, bench, scale_to_yuan, today)
        print("\n  ⚠ 上面这张残差表**不进退出码判定**（v5.8 起）——"
              "它是留档用的，不是判据。")
    else:
        _hr("Q5  验收：跳过")
        print("  单位没钉住（Q4 没过），**不许算含权量** —— 猜错就是 10⁴ 倍。")

    tables_ok = issue_df is not None
    code, why = decide(tables_ok, appr, n_pre_dated=n_dated, n_pre_blank=n_blank,
                       lead_stats=lead_stats)

    _hr(f"结论（退出码 {code}）")
    for line in why:
        print(f"  {line}")
    print("\n  把这份输出整个贴回来 —— 阶段 3 每个列名和每句措辞都要能指回它。")
    print(f"  {archive_hint()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())