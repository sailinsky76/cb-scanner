<#
  daily_run.ps1 —— 无人值守日常跑批

  流程：等网络 → 判交易日 → run.py → notify.py → 关机

  【编码】本文件必须存为 **UTF-8 with BOM**。
  Windows PowerShell 5.1 读 .ps1 时，没有 BOM 就按系统 ANSI 代码页（中文机器 = GBK）
  解码，UTF-8 的中文注释会碎成乱码，其中一些字节恰好撞上 ) " } 等语法字符 →
  脚本在解析阶段就崩，报的却是「缺少 )」这种和真实原因毫不相干的错。
  用记事本另存时选「UTF-8 (带 BOM)」；VS Code 右下角选 "UTF-8 with BOM"。
  改完想确认，跑一次本文件末尾注释里那三行。

  一条贯穿全篇的设计原则：
    **推送没成功就不关机。**
  一台自动关机的机器，失败时是完全静默的 —— 报告没生成、推送挂了、
  开机没成功，三种在手机上长得一模一样（都是什么都没有）。
  所以让「机器第二天还亮着」成为唯一的异常信号，你一眼看得见。

  放在项目根目录（和 run.py 同级）。测试时加 -NoShutdown。

  用法：
      powershell -ExecutionPolicy Bypass -File <项目路径>\daily_run.ps1
      ... -NoShutdown          # 跑完不关机（**第一周就用这个**）
      ... -Force               # 忽略「今天是周末」直接跑
#>

param(
    [switch]$NoShutdown,
    [switch]$Force,
    [int]$ShutdownDelaySec = 120,
    [int]$NetWaitSec       = 300
)
# ↑ ShutdownDelaySec：关机前的可取消窗口，人在机器前可执行 shutdown /a
# ↑ NetWaitSec：等网络最多等多久（RTC 唤醒后网卡常慢几十秒才拿到 IP）

# ---- 控制台 UTF-8：否则 python 打出来的中文在日志里还是乱码 ----
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$Root   = $PSScriptRoot
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log    = Join-Path $LogDir ("autorun_{0}.log" -f (Get-Date -Format "yyyyMM"))

function Say([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

# 子进程的输出：边打屏边收集，最后**用同一个编码**一次性写日志。
# 不用 Tee-Object -FilePath —— 它在 5.1 上按默认编码写，会和上面 Add-Content 的
# UTF8 混在同一个文件里，日志半截可读半截乱码。这和本文件顶上那个坑是同一类。
function Invoke-Logged([string]$label, [string[]]$argv) {
    Say "$label 开始"
    $captured = & py $argv 2>&1 | ForEach-Object { Write-Host $_; $_ }
    $code = $LASTEXITCODE
    if ($captured) {
        Add-Content -Path $Log -Value ($captured | ForEach-Object { "    $_" }) -Encoding UTF8
    }
    return $code
}

function Stop-Box([string]$reason) {
    Say "$reason —— ${ShutdownDelaySec}s 后关机（人在机器前可执行 shutdown /a 取消）"
    # /c 的内容刻意用 ASCII：这段字符串要穿过 shutdown.exe 和关机对话框，
    # 那一路的编码不由本脚本控制，不值得为一句提示再引入一个变量。
    shutdown /s /f /t $ShutdownDelaySec /c "cb_scanner finished. Shutting down."
}

Say "===================== 开始 ====================="
Say "参数: NoShutdown=$NoShutdown Force=$Force  目录=$Root"

# py 启动器不在 PATH 上时，& py 会抛 CommandNotFoundException 而**不设** $LASTEXITCODE，
# 于是后面读到的是上一条命令的残值 —— 有可能读成 0，然后一路顺利地关机。
# 任务计划以「不管用户是否登录」运行时环境变量和交互式登录不同，这不是理论风险。
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Say "!! PATH 上找不到 py 启动器 —— **不关机**，让机器亮着当告警"
    exit 1
}

# ---------------------------------------------------------------- 1. 交易日
# 只判工作日，不判节假日。节假日照跑一次的代价 = 一份空报告；
# 漏跑一天的代价 = 可能漏掉一个缴款日或最后交易日。两边不对称，宁可多跑。
$dow = (Get-Date).DayOfWeek
if ((-not $Force) -and ($dow -eq 'Saturday' -or $dow -eq 'Sunday')) {
    Say "今天是 $dow，不跑。"
    if (-not $NoShutdown) { Stop-Box "非交易日" }
    exit 0
}

# ---------------------------------------------------------------- 2. 等网络
function Test-Net([string]$target, [int]$port = 443) {
    try {
        $c = [Net.Sockets.TcpClient]::new()
        $ok = $c.ConnectAsync($target, $port).Wait(4000)
        $c.Close()
        return $ok
    } catch { return $false }
}

$deadline = (Get-Date).AddSeconds($NetWaitSec)
$netOK = $false
while ((Get-Date) -lt $deadline) {
    if ((Test-Net "push2delay.eastmoney.com") -or (Test-Net "www.cninfo.com.cn")) {
        $netOK = $true
        break
    }
    Start-Sleep -Seconds 10
}
if ($netOK) {
    Say "网络就绪"
} else {
    # 不 return —— 照跑。run.py 会在「数据源健康」里如实报错并给退出码 2，
    # notify.py 会把这件事推到你手机上。**静默跳过才是最坏的处理。**
    Say "等了 ${NetWaitSec}s 网络仍不通 —— 照跑，让 run.py 自己报告这件事"
}

# ---------------------------------------------------------------- 3. 跑扫描
Set-Location $Root
$scanCode = Invoke-Logged "run.py" @("-3.11", "run.py", "--format", "console", "markdown", "html")
Say "run.py 退出码 = $scanCode  (0=全绿 / 1=有源残缺 / 2=全失败)"

# ---------------------------------------------------------------- 4. 推送
$pushCode = Invoke-Logged "notify.py" @("-3.11", "notify.py", "--code", "$scanCode")
Say "notify.py 退出码 = $pushCode  (0=推送成功 / 1=渠道全失败 / 2=没配渠道)"

# ---------------------------------------------------------------- 5. 关机
if ($NoShutdown) {
    Say "带了 -NoShutdown，不关机"
    Say "===================== 结束 ====================="
    exit $scanCode
}

if ($pushCode -ne 0) {
    # 唯一一处「故意不关机」。让机器亮着，就是那条推不出去的通知本身。
    Say "!! 推送未成功（code=$pushCode）—— **不关机**，让机器亮着当告警"
    Say "===================== 结束 ====================="
    exit 1
}

Say "===================== 结束 ====================="
Stop-Box "推送成功"
exit 0

<#
  确认本文件确实带 BOM（应打印 True）：

      $p = Join-Path $PSScriptRoot 'daily_run.ps1'
      $b = [IO.File]::ReadAllBytes($p)[0..2]
      ($b[0] -eq 0xEF) -and ($b[1] -eq 0xBB) -and ($b[2] -eq 0xBF)

  哪天编辑器把 BOM 弄丢了（症状：又报「缺少 )」），一行加回来：

      $t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
      [IO.File]::WriteAllText($p, $t, [Text.UTF8Encoding]::new($true))
#>
