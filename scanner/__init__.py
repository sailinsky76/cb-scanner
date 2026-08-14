"""cb_scanner —— 低容量套利每日扫描器。

分层：
  models.py    统一数据模型（Opportunity / SourceResult / Kind / Urgency）
               外加 SOURCE_KEYS：Kind ↔ config.yaml 的 sources 键，**唯一一份**
  utils.py     safe_call 兜底、disk_cache 当日缓存、日期/数值解析、HTML与链接清洗
  config.py    内置默认值 + config.yaml 深合并
  report.py    终端 / Markdown / HTML 三种渲染
  sources/     六个数据源，各自独立，单源失败不影响整体
"""

# 跟着 STATE.md 顶栏的版本走。上一版这里一直是 "0.1.0"，而项目已经到 v5.9 ——
# 没人读它所以没人发现，但它是这个包里唯一一个机器可读的版本号。
__version__ = "5.9.7"
