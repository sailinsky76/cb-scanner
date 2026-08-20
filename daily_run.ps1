<#
  daily_run.ps1 —— 无人值守日常跑批   (v4)

  流程：工具模式 → 判周末 → 判今日是否已完成 → 等网络 → run.py → notify.py → 关机决策

  【编码】本文件必须存为 **UTF-8 with BOM**。
  Windows PowerShell 5.1 读 .ps1 时，没有 BOM 就按系统 ANSI 代码页（中文机器 = GBK）
  解码，UTF-8 的中文注释会碎成乱码，其中一些字节恰好撞上 ) " } 等语法字符 →
  脚本在解析阶段就崩，报的却是「缺少 )」这种和真实原因毫不相干的错。
  用记事本另存时选「UTF-8 (带 BOM)」；VS Code 右下角选 "UTF-8 with BOM"。
  改完想确认，跑一次本文件末尾注释里那几行。

  一条贯穿全篇的设计原则：
    **推送没成功就不关机。**
  一台自动关机的机器，失败时是完全静默的 —— 报告没生成、推送挂了、
  开机没成功，三种在手机上长得一模一样（都是什么都没有）。
  所以让「机器第二天还亮着」成为唯一的异常信号，你一眼看得见。

  第二条原则（v2 加）：
    **关机前先确认没人在用；关不掉比误关强。**
  BIOS RTC 只能设「每天」开机，所以周末机器照样亮。这个脚本因此改成
  **每天**都被任务计划触发：工作日跑扫描，周末只负责把机器关回去。
  代价是「脚本会在有人坐在机器前的时候执行关机」这件事从理论风险变成日常
  ——于是关机前必须做在用检测，倒计时期间还要再复检。
  所有检测在拿不准的时候一律判「有人」，宁可让机器亮着。

  第三条原则（v4 加）：
    **只关「本脚本自己叫醒的那台机器」。**
  在用检测回答的是「此刻有没有人」，回答不了「这台机器为什么是开着的」。
  你自己中午开机干活、去泡杯咖啡锁了屏，在用检测判「无人」是对的，
  关机却是错的 —— 那台机器根本不是这套自动化开起来的。
  v3 只在周末问这个问题，v4 把它提成**所有关机路径都要过的第一道闸**。

  ── v4 改了什么（按重要性排） ───────────────────────────────
  1. **修：有人登录着却被 `/f` 强关。** v3 用 `$p.Sessions`（只数 explorer）决定要不要
     带 `/f`，而 `Get-Presence` 认「有人登录」用的是两路信号（explorer 会话数 **或**
     `Win32_ComputerSystem.UserName`）。explorer 那一路取不到、只有第二路认出登录用户时，
     `Sessions` 是 0 → 走 `shutdown /s /f` → 未保存的文档**直接丢**。
     而 SETUP_AUTORUN.md 的判定矩阵白纸黑字写着这一档「不带 /f」。
     改法：`Get-Presence` 把 `LoggedIn` 一起返回，`/f` 只在**没有任何人登录**时才用。
  2. **修：`py` 不在 PATH 时，周末关机被一起挡掉。** v3 把 `Get-Command py` 的守卫放在
     周末分支**前面**，于是 py 一旦丢失（Windows 更新动过环境变量、装了新 Python 版本），
     周末机器就再也关不掉 —— 正好是这套东西当初要消灭的那个失败。
     周末那一支根本不跑 python，守卫挪到它后面。
  3. **修：`-ShutdownOnly` 周末绕过了「是不是 RTC 叫醒的」判据。** v3 里周末主分支
     小心翼翼地按开机时间判，而每 30 分钟一次的补关机任务完全不判 →
     你周六自己开机干活、锁屏五分钟，机器就被收走了。两套判据管同一件事，
     这是这个项目反复吃亏的那种「第二份手抄表」。
     改法：判据收进 `Get-BootVerdict`，`Invoke-ShutdownDecision` 里过闸，
     所有关机路径（主任务 / 周末 / 补关机 / 今日已完成）自动共用同一份。
  4. **改：开机判据从「周末专用」提成「全局闸」**（见上面第三条原则）。
     `-ShutdownRegardlessOfBoot` 可以关掉这道闸，回到 v3 行为。
  5. **修：休眠路（第 0 步的 B 路）下开机判据恒为假。** `LastBootUpTime` 跨休眠/唤醒
     不变，B 路用户会被判成「机器一直没关过」→ 永不关机。
     改法：`Get-MachineUpSince` 取 `LastBootUpTime` 与**最近一次唤醒事件**
     （Power-Troubleshooter / Id 1）里更晚的那个。取不到就退回挂钟规则。
  6. 日志治理：`Invoke-Logged` 不再把 tqdm 的进度帧和空 stderr 记录抄进日志
     （一天三跑 = 1589 行 / 159 KB，其中大半是 `33%|███▎ |` 这种），
     折叠了多少行会照说；日志文件按 `-LogKeepMonths`（默认 6 个月）自动清理。

  放在项目根目录（和 run.py 同级）。测试时加 -NoShutdown。

  用法：
      powershell -ExecutionPolicy Bypass -File <项目路径>\daily_run.ps1
      ... -NoShutdown          # 跑完不关机（**第一周就用这个**）
      ... -Force               # 忽略「今天是周末」「今天已跑过」直接跑
      ... -CheckOnly           # 只打印在用检测 + 开机判据就退出，什么都不跑、不关
      ... -ShutdownOnly        # 只做在用检测 + 关机决策，不扫描不推送
      ... -Hold 4              # 挂起自动关机 4 小时（写 logs\NOSHUTDOWN.txt）
      ... -Hold 0              # 取消挂起
      ... -ShutdownRegardlessOfBoot   # 关掉开机判据这道闸（回到 v3 行为）
#>

param(
    [switch]$NoShutdown,
    [switch]$Force,
    [switch]$CheckOnly,
    [switch]$ShutdownOnly,
    [switch]$ShutdownRegardlessOfBoot,
    [int]$Hold = -1,
    [int]$ShutdownDelaySec       = 120,
    [int]$LockedShutdownDelaySec = 300,
    [int]$IdleShutdownMin        = 0,
    [string]$RtcBootBefore       = "11:00",
    [string]$WeekendShutdownUntil = "",
    [int]$NetWaitSec             = 300,
    [int]$LogKeepMonths          = 6
)
# ↑ ShutdownDelaySec：无人登录时的关机倒计时（可取消窗口）
# ↑ LockedShutdownDelaySec：有人登录但已锁屏时的倒计时，更长，且**不带 /f**
# ↑ IdleShutdownMin：>0 时，「已登录、未锁屏、但空闲 ≥ N 分钟」也算无人。默认 0 = 关闭
#                    （默认关闭是有意的：人在看视频/开会时不产生键鼠输入）
# ↑ RtcBootBefore：**RTC 开机时刻的上界**。机器是今天在这个时刻之前起来的 = RTC 叫醒的，
#                    可以自动关；晚于它 = 你自己开的，不关。判的是**开机时间**不是当前时间，
#                    所以迟到的那次触发照样能把机器收掉。RTC 设的不是 09:00 就相应调这个值。
# ↑ WeekendShutdownUntil：v3 的旧名，只为兼容而留 —— 填了它就覆盖 -RtcBootBefore。
#                    新脚本请直接用 -RtcBootBefore（v4 起这道闸工作日也生效，旧名已经词不达意）。
# ↑ NetWaitSec：等网络最多等多久（RTC 唤醒后网卡常慢几十秒才拿到 IP）
# ↑ LogKeepMonths：logs\autorun_YYYYMM.log 保留几个月，超期自动删

if (-not [string]::IsNullOrWhiteSpace($WeekendShutdownUntil)) {
    $RtcBootBefore = $WeekendShutdownUntil
}

# ---- 控制台 UTF-8：否则 python 打出来的中文在日志里还是乱码 ----
# 任务计划里没有真正的控制台窗口时这个赋值可能抛异常，包起来，失败也不影响主流程。
try {
    $OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
} catch { }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$Root     = $PSScriptRoot
$LogDir   = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log      = Join-Path $LogDir ("autorun_{0}.log" -f (Get-Date -Format "yyyyMM"))
$HoldFile = Join-Path $LogDir "NOSHUTDOWN.txt"
$DoneFlag = Join-Path $LogDir ("done_{0}.flag" -f (Get-Date -Format "yyyyMMdd"))

function Say([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $Log -Value $line -Encoding UTF8
}

# 【v4】月度日志此前只切分、从不清理，而每一份里都是**实盘报告全文 + 真实本金**
# （.gitignore 里那条 logs/ 讲的就是这件事）。攒着不看的东西越少越好。
if ($LogKeepMonths -gt 0) {
    Get-ChildItem -Path $LogDir -Filter "autorun_*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddMonths(-$LogKeepMonths) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# 子进程的输出：边打屏边收集，最后**用同一个编码**一次性写日志。
# 不用 Tee-Object -FilePath —— 它在 5.1 上按默认编码写，会和上面 Add-Content 的
# UTF8 混在同一个文件里，日志半截可读半截乱码。这和本文件顶上那个坑是同一类。
#
# 【v4】写日志前折叠两类零信息行：
#   · tqdm 的进度帧（` 33%|███▎      | 1/3 [00:00<00:01,  1.23it/s]` / `0it [00:00, ?it/s]`）
#   · `System.Management.Automation.RemoteException` —— 这是 2>&1 把**空的** stderr 块
#     包成 ErrorRecord 之后 PowerShell 的渲染结果，本来就没有内容可看
#   · tqdm 清行留下的整行空格
# 屏幕上是全的（-NoShutdown 手动跑时你看得见），只有落盘那一份折叠，且折叠了几行会照说。
function Invoke-Logged([string]$label, [string[]]$argv) {
    Say "$label 开始"
    $captured = & py $argv 2>&1 | ForEach-Object { Write-Host $_; $_ }
    $code = $LASTEXITCODE

    $keep  = New-Object System.Collections.Generic.List[string]
    $noise = 0
    foreach ($l in @($captured)) {
        $s = "$l"
        if ($s -match '^\s*\d{1,3}%\|' -or
            $s -match '^\s*\d+it \[' -or
            $s -eq 'System.Management.Automation.RemoteException' -or
            ($s.Length -gt 0 -and [string]::IsNullOrWhiteSpace($s))) {
            $noise++
            continue
        }
        $keep.Add("    $s")
    }
    if ($keep.Count -gt 0) {
        Add-Content -Path $Log -Value $keep.ToArray() -Encoding UTF8
    }
    if ($noise -gt 0) {
        Say "    （日志折叠了 $noise 行进度条/空 stderr 帧；屏幕输出未折叠）"
    }

    # 【v3】拿不到退出码就按失败算。$null 会被 "$code" 变成空串，
    # notify.py --code "" 是 argparse 报错退 2 —— 兜得住，但不如在这里说清楚。
    if ($null -eq $code) {
        Say "!! $label 没有留下退出码（命令根本没跑起来？）—— 按失败处理"
        $code = 1
    }
    return $code
}

# ================================================================
#  在用检测
# ================================================================
#  这个脚本以「不管用户是否登录都要运行」在**会话 0**里跑（SYSTEM 或存了密码的
#  账户），所以下面这些常见做法是**不可用**的，别再往里加：
#    · GetLastInputInfo —— 只报调用方所在会话的空闲，会话 0 里读到的和桌面无关
#    · $host.UI / 弹窗    —— 会话 0 没有可见桌面，弹出来也没人看得见
#  可用的是这三类跨会话信号：进程表、quser.exe、powercfg /requests。

# 【v3】quser / powercfg 是按 **OEM 代码页**（中文机器 936）输出的，
# 而本文件顶上把 [Console]::OutputEncoding 设成了 UTF-8（那是给 python 的）。
# 用 UTF-8 去解 GBK 字节，「无」会变成乱码 —— 于是空闲时间恒为"未知"、
# powercfg 的"无。"被当成有内容 → 屏幕占用恒为 True。这两个函数只在
# -IdleShutdownMin 那一档起作用，所以 v2 一直没暴露，但它是错的。
# 解法：调用前临时切到 OEM 代码页，调用完还原。
function Get-OemEncoding {
    try {
        $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage'
        $cp  = Get-ItemPropertyValue -Path $key -Name 'OEMCP' -ErrorAction Stop
        return [Text.Encoding]::GetEncoding([int]$cp)
    } catch { }
    try {
        return [Text.Encoding]::GetEncoding([int][Globalization.CultureInfo]::InstalledUICulture.TextInfo.OEMCodePage)
    } catch { }
    return $null
}

function Invoke-OemExe([string]$exe, [string[]]$argv) {
    $prev = [Console]::OutputEncoding
    $oem  = Get-OemEncoding
    try {
        if ($null -ne $oem) {
            try { [Console]::OutputEncoding = $oem } catch { }
        }
        if ($argv -and $argv.Count -gt 0) { return @(& $exe @argv 2>$null) }
        return @(& $exe 2>$null)
    } catch {
        return @()
    } finally {
        try { [Console]::OutputEncoding = $prev } catch { }
    }
}

# quser 的「空闲时间」列 → 分钟数。取不到一律返回 $null（= 未知），
# 未知会让上层走保守分支，而不是猜一个数字出来。
function ConvertTo-IdleMinutes([string]$tok) {
    if ([string]::IsNullOrWhiteSpace($tok)) { return $null }
    $t = $tok.Trim()
    if ($t -eq '.' -or $t -eq 'none' -or $t -eq '无') { return 0 }
    if ($t -match '^(\d+)\+(\d{1,2}):(\d{2})$') {
        return ([int]$Matches[1]) * 1440 + ([int]$Matches[2]) * 60 + [int]$Matches[3]
    }
    if ($t -match '^(\d{1,4}):(\d{2})$') { return ([int]$Matches[1]) * 60 + [int]$Matches[2] }
    if ($t -match '^\d+$')               { return [int]$t }
    return $null
}

# 返回所有会话里**最小**的空闲分钟数（= 最活跃的那个人）。
function Get-IdleMinutes {
    $exe = Join-Path $env:SystemRoot "System32\quser.exe"
    if (-not (Test-Path $exe)) { return $null }
    $raw = Invoke-OemExe $exe @()
    if ($null -eq $raw -or $raw.Count -lt 2) { return $null }

    $mins = @()
    foreach ($line in $raw[1..($raw.Count - 1)]) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $cols = ("$line" -replace '^\s*>?\s*', '') -split '\s{2,}'
        if ($cols.Count -lt 3) { continue }
        # 从右往左数：最后一列是登录时间，倒数第二列就是空闲时间。
        # 从左往右数不行 —— 断开的会话「会话名」那一列是空的，列会整体错位。
        $m = ConvertTo-IdleMinutes $cols[$cols.Count - 2]
        if ($null -ne $m) { $mins += $m }
    }
    if ($mins.Count -eq 0) { return $null }
    return ($mins | Measure-Object -Minimum).Minimum
}

# 有没有程序按住屏幕不让它灭（放视频、开会、投屏）。只在 -IdleShutdownMin 分支里用。
function Test-DisplayRequest {
    $out = Invoke-OemExe (Join-Path $env:SystemRoot "System32\powercfg.exe") @("/requests")
    if (-not $out) { return $false }
    $inDisplay = $false
    foreach ($line in $out) {
        $s = "$line".Trim()
        if ($s -match '^([A-Z]+):$') {
            $inDisplay = ($Matches[1] -eq 'DISPLAY')
            continue
        }
        if ($inDisplay -and $s -ne '') {
            if ($s -eq 'None.' -or $s -eq 'None' -or $s -like '无*') { continue }
            return $true
        }
    }
    return $false
}

function Get-Presence {
    param([switch]$Fast)

    $v = [pscustomobject]@{
        Present     = $true
        Reason      = "检测未完成 —— 保守判定为有人"
        Sessions    = 0
        LoggedIn    = $true
        Locked      = $false
        IdleMin     = $null
        DisplayHold = $false
        HoldFile    = $false
    }

    # 0) 人工挂起：文件在且没过期 → 一律不关。第一优先，覆盖后面所有判断。
    if (Test-Path $HoldFile) {
        $until = $null
        try {
            $txt = (Get-Content -Path $HoldFile -TotalCount 1 -ErrorAction Stop)
            $until = [datetime]::ParseExact("$txt".Trim(), 'yyyy-MM-dd HH:mm:ss',
                                            [Globalization.CultureInfo]::InvariantCulture)
        } catch { $until = $null }

        if ($null -eq $until) {
            $v.HoldFile = $true
            $v.Reason   = "存在挂起文件 NOSHUTDOWN.txt（内容不是有效期限，按永久挂起处理）"
            return $v
        }
        if ((Get-Date) -lt $until) {
            $v.HoldFile = $true
            $v.Reason   = "存在挂起文件，有效期至 $($until.ToString('yyyy-MM-dd HH:mm'))"
            return $v
        }
        Remove-Item -Path $HoldFile -Force -ErrorAction SilentlyContinue
        Say "挂起文件已过期（$($until.ToString('yyyy-MM-dd HH:mm'))），已自动删除"
    }

    # 1) 有没有交互式桌面。explorer.exe 只可能跑在会话 >0 里；
    #    RTC 冷启动到锁屏、没人登录时，它根本不存在。
    $explorer = @(Get-Process -Name explorer -ErrorAction SilentlyContinue |
                  Where-Object { $_.SessionId -gt 0 })
    $ids = @($explorer | Select-Object -ExpandProperty SessionId -Unique)
    $v.Sessions = $ids.Count

    # 1b) 第二路信号：控制台登录用户。explorer 被杀掉/换了 shell 时它还在。
    $csUser = $null
    try { $csUser = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).UserName } catch { }
    # 【v4】这个值以前只是个局部变量，`Invoke-ShutdownDecision` 拿不到它，
    # 只能退而用 Sessions 判要不要 /f —— 于是「explorer 那一路没认出、第二路认出了」
    # 的机器会被强关。现在它跟着一起返回。
    $v.LoggedIn = ($ids.Count -gt 0) -or (-not [string]::IsNullOrWhiteSpace($csUser))

    # 2) 锁屏 / 登录界面
    $v.Locked = ([bool]@(Get-Process -Name LogonUI -ErrorAction SilentlyContinue).Count)

    # 3) 空闲时间（拿不到就是 $null）。倒计时里的复检是 -Fast，
    #    没开 -IdleShutdownMin 时空闲值只是日志里的参考，不值得每 5 秒起一次 quser。
    if ($Fast -and $IdleShutdownMin -le 0) { $v.IdleMin = $null } else { $v.IdleMin = Get-IdleMinutes }

    if (-not $v.LoggedIn) {
        $v.Present = $false
        $v.Reason  = "没有任何交互式桌面会话（无人登录）"
        return $v
    }

    if ($v.Locked -and $ids.Count -le 1) {
        $v.Present = $false
        $v.Reason  = "有登录会话，但停在锁屏/登录界面"
        return $v
    }
    if ($v.Locked) {
        $v.Reason = "有 $($ids.Count) 个会话且检测到锁屏 —— 分不清哪个锁了哪个在用，按有人处理"
        return $v
    }

    # 4) 已登录 + 未锁屏：默认就是有人。只有显式开了 -IdleShutdownMin 才继续往下判。
    if ($IdleShutdownMin -gt 0 -and $null -ne $v.IdleMin -and $v.IdleMin -ge $IdleShutdownMin) {
        if (-not $Fast) { $v.DisplayHold = Test-DisplayRequest }
        if ($v.DisplayHold) {
            $v.Reason = "空闲 $($v.IdleMin) 分钟，但有程序按住屏幕不灭（放视频/会议）—— 按有人处理"
            return $v
        }
        $v.Present = $false
        $v.Reason  = "桌面已解锁，但空闲 $($v.IdleMin) 分钟 ≥ 阈值 $IdleShutdownMin 分钟"
        return $v
    }

    $idleTxt = "未知"
    if ($null -ne $v.IdleMin) { $idleTxt = "$($v.IdleMin) 分钟" }
    if ($ids.Count -gt 0) {
        $v.Reason = "有 $($ids.Count) 个已解锁的登录会话（空闲 $idleTxt），判定为正在使用"
    } else {
        $v.Reason = "控制台登录用户 $csUser 在线且未检测到锁屏（空闲 $idleTxt），判定为正在使用"
    }
    return $v
}

function Write-Presence($p) {
    $idle = "未知"
    if ($null -ne $p.IdleMin) { $idle = "$($p.IdleMin)m" }
    $fmt = "[在用检测] 有人={0} 已登录={1} 桌面会话={2} 锁屏={3} 空闲={4} 屏幕占用={5} 挂起={6}"
    Say ($fmt -f $p.Present, $p.LoggedIn, $p.Sessions, $p.Locked, $idle, $p.DisplayHold, $p.HoldFile)
    Say "[在用检测] 依据：$($p.Reason)"
}

# ================================================================
#  时间小工具
# ================================================================
# "11:00" → 今天 11:00 的 DateTime。写坏了返回 $null，让调用方自己决定怎么兜。
function Get-TodayAt([string]$hhmm) {
    if ([string]::IsNullOrWhiteSpace($hhmm)) { return $null }
    foreach ($f in @('HH:mm', 'H:mm', 'HH:mm:ss')) {
        try {
            $t = [datetime]::ParseExact($hhmm.Trim(), $f, [Globalization.CultureInfo]::InvariantCulture)
            return (Get-Date).Date.AddHours($t.Hour).AddMinutes($t.Minute)
        } catch { }
    }
    return $null
}

# ================================================================
#  开机判据：这台机器现在开着，是不是本脚本叫醒的
# ================================================================
# 【v4】v3 只在周末问这个问题，而它在工作日同样成立：
#   · 你周三中午自己开机干活 → 任务计划「补跑错过的那次」立刻触发 →
#     扫完推完，你正好去接杯水锁了屏 → 在用检测判「无人」→ 机器被关掉
#   · 补关机任务（每 30 分钟一次）在 done 标记已经落下之后，
#     对任何一台空闲机器都会动手 —— 哪怕那台机器是你 14:00 才开的
# 在用检测回答不了「为什么开着」，只有开机时间能。所以这道闸放在最前面。

# 机器这次是什么时候起来的。
# 【v4】不能只看 LastBootUpTime：手册第 0 步的 B 路（休眠 + 任务计划唤醒）下
# 它跨休眠/唤醒**不变**，于是永远判成「昨天或更早开的」→ 永不关机。
# 取它和最近一次唤醒事件里更晚的那个。两个都取不到返回 $null。
function Get-MachineUpSince {
    $boot = $null
    try { $boot = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime } catch { }

    $wake = $null
    try {
        $ev = Get-WinEvent -FilterHashtable @{
                  LogName      = 'System'
                  ProviderName = 'Microsoft-Windows-Power-Troubleshooter'
                  Id           = 1
              } -MaxEvents 1 -ErrorAction Stop
        if ($ev) { $wake = $ev.TimeCreated }
    } catch { }

    if ($null -eq $boot) { return $wake }
    if ($null -eq $wake) { return $boot }
    if ($wake -gt $boot) { return $wake }
    return $boot
}

# 返回 Verdict = 'rtc' | 'user' | 'stale'，外加一句给日志的人话。
#   rtc   —— 今天起来的、且早于 RtcBootBefore：是 RTC（或计划唤醒）叫醒的，可以关
#   user  —— 今天起来的、但晚于那个时刻：你自己开的，不关
#   stale —— 昨天或更早就起来了：上一次跑批没关成留下的告警状态，不关（别把告警抹掉）
# 拿不到开机时间时退回挂钟规则（现在早于 RtcBootBefore 就当 rtc），
# 这和 v3 的降级行为一致 —— 不因为读不到一个信号就永久拒绝收尾。
function Get-BootVerdict {
    $up  = Get-MachineUpSince
    $cut = Get-TodayAt $RtcBootBefore

    if ($null -eq $cut) {
        return [pscustomobject]@{
            Verdict = 'rtc'
            Reason  = "-RtcBootBefore（'$RtcBootBefore'）解析不了，这道闸放行，交给在用检测"
        }
    }
    if ($null -eq $up) {
        if ((Get-Date) -lt $cut) {
            return [pscustomobject]@{
                Verdict = 'rtc'
                Reason  = "拿不到开机时间，退回挂钟规则（现在早于 $RtcBootBefore）"
            }
        }
        return [pscustomobject]@{
            Verdict = 'user'
            Reason  = "拿不到开机时间，退回挂钟规则：现在已过 $RtcBootBefore"
        }
    }
    if ($up.Date -ne (Get-Date).Date) {
        return [pscustomobject]@{
            Verdict = 'stale'
            Reason  = "机器从 $($up.ToString('MM-dd HH:mm')) 起就没关过 —— 不是今天叫醒的"
        }
    }
    if ($up -ge $cut) {
        return [pscustomobject]@{
            Verdict = 'user'
            Reason  = "机器是今天 $($up.ToString('HH:mm')) 起来的，晚于 $RtcBootBefore"
        }
    }
    return [pscustomobject]@{
        Verdict = 'rtc'
        Reason  = "机器是今天 $($up.ToString('HH:mm')) 起来的（早于 $RtcBootBefore）—— 判定是 RTC 叫醒的"
    }
}

# ================================================================
#  关机
# ================================================================
# 【v3】把 shutdown.exe 的调用单独摘出来，就为了拿它的**退出码**。
# 原来不看退出码：排程失败（最常见 1190「已经排了一次关机」，其次是权限不足）
# 时脚本照样走完倒计时、照样打「关机继续」—— 日志上是一次成功的关机，
# 而机器还亮着。这类假成功比直接失败更难查。
function Invoke-Shutdown([string[]]$argv) {
    $out = & (Join-Path $env:SystemRoot "System32\shutdown.exe") @argv 2>&1
    $code = $LASTEXITCODE
    foreach ($l in @($out)) {
        $s = "$l".Trim()
        if ($s -ne '') { Say "    shutdown: $s" }
    }
    if ($null -eq $code) { return 1 }
    return $code
}

function Stop-Box {
    param([string]$reason, [int]$delaySec, [bool]$useForce)

    # /c 的内容刻意用 ASCII：这段字符串要穿过 shutdown.exe 和关机对话框，
    # 那一路的编码不由本脚本控制，不值得为一句提示再引入一个变量。
    $msg = "cb_scanner finished. Auto shutdown in $delaySec sec. Run 'shutdown /a' to cancel."
    $a = @("/s", "/t", "$delaySec", "/c", $msg)
    if ($useForce) { $a = @("/s", "/f", "/t", "$delaySec", "/c", $msg) }

    Say "$reason —— ${delaySec}s 后关机（force=$useForce；人在机器前可执行 shutdown /a 取消）"

    $code = Invoke-Shutdown $a
    if ($code -ne 0) {
        Say "shutdown 排程失败（exit=$code）—— 先撤销可能已排队的那次，再试一次"
        [void](Invoke-Shutdown @("/a"))
        Start-Sleep -Seconds 1
        $code = Invoke-Shutdown $a
    }
    if ($code -ne 0) {
        Say "!! shutdown 两次都没排上（exit=$code）—— 放弃关机。机器留着亮，这本身就是告警"
        return $false
    }

    # 倒计时期间每 5 秒复检一次。解决的是这一类：关机对话框弹在锁屏后面 /
    # 人回到座位先解锁再看屏幕 / 根本来不及敲 shutdown /a。
    # 只要检测到人回来了就自己撤销，不指望人去操作。
    $deadline = (Get-Date).AddSeconds([Math]::Max($delaySec - 8, 0))
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $q = Get-Presence -Fast
        if ($q.Present) {
            [void](Invoke-Shutdown @("/a"))
            Say "!! 倒计时中检测到有人回来（$($q.Reason)）—— 已 shutdown /a 撤销关机"
            return $false
        }
    }
    Say "倒计时走完，关机继续"
    return $true
}

# 唯一的关机入口：先过开机判据、再做在用检测，然后决定用哪种关法。
# 任何路径都不许直接调 Stop-Box。
function Invoke-ShutdownDecision([string]$reason) {
    if ($NoShutdown) {
        Say "带了 -NoShutdown，不关机（$reason）"
        return $false
    }

    # ---- 第一道闸：这台机器是不是本脚本叫醒的 ----------------------
    # 【v4】v3 只在周末问这一句，且写在主流程里，所以补关机那条路径绕过了它。
    # 现在收在这里，所有关机路径共用同一份判据。
    if (-not $ShutdownRegardlessOfBoot) {
        $bv = Get-BootVerdict
        Say "[开机判据] $($bv.Reason)"
        if ($bv.Verdict -eq 'user') {
            Say "!! 这台机器不是本脚本叫醒的 —— **不关机**，把它留给你（$reason）"
            Say "   （确实想让它关：加 -ShutdownRegardlessOfBoot，或把 -RtcBootBefore 调晚）"
            return $false
        }
        if ($bv.Verdict -eq 'stale') {
            Say "!! 这是上一次跑批没关成留下的告警状态 —— **不关机**，别把告警抹掉（$reason）"
            Say "   （去看上一个工作日的日志尾部；确认无碍后手动关一次即可）"
            return $false
        }
    } else {
        Say "[开机判据] 带了 -ShutdownRegardlessOfBoot，这道闸跳过"
    }

    # ---- 第二道闸：此刻有没有人在用 --------------------------------
    $p = Get-Presence
    Write-Presence $p

    if ($p.Present) {
        Say "!! 机器正在被使用 —— **不关机**，把它留给你（$reason）"
        return $false
    }

    if ($p.LoggedIn) {
        # 有人登录着、只是锁了屏或走开了：倒计时给长的，而且**不带 /f**。
        # 不带 /f 意味着有未保存文档的程序能把关机顶回去 —— 机器留着亮，
        # 这正好落在本文件顶上那条「亮着 = 有事发生」的约定里。
        # 【v4】判据从 $p.Sessions 换成 $p.LoggedIn：Sessions 只数 explorer，
        # 而「有没有人登录」是两路信号判出来的。只认一路会把
        # 「explorer 没认出但确实有人登录」的机器错送进 /f 那一档。
        return (Stop-Box -reason $reason -delaySec ([Math]::Max($ShutdownDelaySec, $LockedShutdownDelaySec)) -useForce $false)
    }
    return (Stop-Box -reason $reason -delaySec $ShutdownDelaySec -useForce $true)
}

# ================================================================
#  主流程
# ================================================================
Say "===================== 开始 ====================="
Say "参数: NoShutdown=$NoShutdown Force=$Force ShutdownOnly=$ShutdownOnly RtcBootBefore=$RtcBootBefore  目录=$Root"

$dow       = (Get-Date).DayOfWeek
$isWeekend = ($dow -eq 'Saturday' -or $dow -eq 'Sunday')

# ---------------------------------------------------------------- 0. 三个工具模式
if ($Hold -ge 0) {
    if ($Hold -eq 0) {
        if (Test-Path $HoldFile) {
            Remove-Item -Path $HoldFile -Force
            Say "已取消关机挂起（删除 $HoldFile）"
        } else {
            Say "当前没有挂起文件，无需取消"
        }
    } else {
        $until = (Get-Date).AddHours($Hold)
        Set-Content -Path $HoldFile -Encoding ASCII -Value $until.ToString("yyyy-MM-dd HH:mm:ss")
        Say "已挂起自动关机 $Hold 小时，至 $($until.ToString('yyyy-MM-dd HH:mm:ss'))"
    }
    exit 0
}

if ($CheckOnly) {
    $bv = Get-BootVerdict
    Say "[开机判据] $($bv.Reason) → $($bv.Verdict)"
    Write-Presence (Get-Presence)
    Say "（-CheckOnly：只检测，不跑扫描也不关机）"
    exit 0
}

# 【v3 新增】-ShutdownOnly：只补一次关机决策。
# 为什么需要它：v2 里任何一次判「有人」之后就没有第二次机会了 —— 周六 09:05 你正好
# 在用，机器就亮到周一。把这个开关配成一个「每 30 分钟重复」的独立任务，
# 机器一空下来就会被收走，而且它不跑 run.py / notify.py，**不占 Server酱 额度**。
# 工作日的保护：当天的 done 标记不存在就拒绝关机 —— 否则这个任务可能抢在
# 主任务前面把机器关掉，当天的报告就没了。
# 【v4】周末不再是「直接放行」：`Invoke-ShutdownDecision` 里的开机判据会替它把关，
# 和主任务周末分支用的是同一份判据（v3 是两套，补关机那套等于没判）。
if ($ShutdownOnly) {
    Say "（-ShutdownOnly：只做在用检测与关机决策，不扫描、不推送）"
    if ((-not $isWeekend) -and (-not (Test-Path $DoneFlag)) -and (-not $Force)) {
        Say "!! 今天是 $dow，但 logs\$([IO.Path]::GetFileName($DoneFlag)) 还不存在"
        Say "   —— 今天的报告还没推成功过，**不关机**，把机器留给主任务（无视这条：加 -Force）"
        Say "===================== 结束 ====================="
        exit 0
    }
    [void](Invoke-ShutdownDecision "-ShutdownOnly 补关机")
    Say "===================== 结束 ====================="
    exit 0
}

# ---------------------------------------------------------------- 1. 周末
# 任务计划的触发器是**每天**，不是周一至周五 —— 因为 BIOS RTC 只能设「每天开机」，
# 周末没有任务来收尾的话，机器会一直亮到周一。所以周末这一支照样要被触发，
# 它只做一件事：确认这台机器是 RTC 叫醒的、且没人在用，然后把它关回去。
#
# 只判工作日，不判节假日。节假日照跑一次的代价 = 一份空报告；
# 漏跑一天的代价 = 可能漏掉一个缴款日或最后交易日。两边不对称，宁可多跑。
#
# 【v4】原来写在这里的那一大段开机时间判定挪进了 `Invoke-ShutdownDecision`
# —— 同一个判据不该有两份实现（周末一份、补关机一份、工作日没有）。
if ((-not $Force) -and $isWeekend) {
    Say "今天是 $dow，不跑扫描（这一支只负责把 RTC 开起来的机器关回去）"
    [void](Invoke-ShutdownDecision "非交易日（$dow）")
    Say "===================== 结束 ====================="
    exit 0
}

# ---------------------------------------------------------------- 1b. py 在不在
# py 启动器不在 PATH 上时，& py 会抛 CommandNotFoundException 而**不设** $LASTEXITCODE，
# 于是后面读到的是上一条命令的残值 —— 有可能读成 0，然后一路顺利地关机。
# 任务计划以「不管用户是否登录」运行时环境变量和交互式登录不同，这不是理论风险。
#
# 【v4】这道守卫从周末分支**前面**挪到了后面。原来的位置有个洞：
# py 一旦丢了（Windows 更新动过环境变量 / 装了新 Python），
# **周末的关机也一起被挡掉** —— 而周末那一支根本不跑 python。
# 症状正是这套东西当初要消灭的那个：周末机器亮两天，日志里只有一句「找不到 py」。
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Say "!! PATH 上找不到 py 启动器 —— **不关机**，让机器亮着当告警"
    Say "===================== 结束 ====================="
    exit 1
}

# ---------------------------------------------------------------- 1c. 今天是不是已经跑过了
# 任务属性里同时勾了「过了计划开始时间立即启动」和「失败后重启 3 次」，
# 所以一天之内被触发两三次是完全可能的。Server酱 免费额度只有 5 条/天，
# 重复推送会把额度吃光，所以推送成功过一次就落一个当日标记，之后只做关机判断。
if ((Test-Path $DoneFlag) -and (-not $Force)) {
    Say "今天已经跑完并推送成功过（logs\$([IO.Path]::GetFileName($DoneFlag))）—— 跳过扫描与推送"
    Say "（要强制重跑：加 -Force）"
    [void](Invoke-ShutdownDecision "今日任务此前已完成")
    Say "===================== 结束 ====================="
    exit 0
}

# ---------------------------------------------------------------- 2. 等网络
function Test-Net([string]$target, [int]$port = 443) {
    $c = $null
    try {
        $c = [Net.Sockets.TcpClient]::new()
        return $c.ConnectAsync($target, $port).Wait(4000)
    } catch {
        return $false
    } finally {
        if ($null -ne $c) { try { $c.Dispose() } catch { } }
    }
}

$netDeadline = (Get-Date).AddSeconds($NetWaitSec)
$netOK = $false
while ((Get-Date) -lt $netDeadline) {
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

# ---------------------------------------------------------------- 5. 落当日标记
# 【v3】这一步从「-NoShutdown 分支之后」提到了它**前面**。
# 原来带 -NoShutdown 跑不写标记，而末尾 exit $scanCode 在 scanCode=1/2 时会被
# 任务计划当成失败 → 按「失败后重启 3 次」重试 → 每次都重新扫描并**重新推送**。
# Server酱 免费额度 5 条/天，一天就能吃光；而文档要求的头一周恰恰是带 -NoShutdown 跑的。
if ($pushCode -eq 0) {
    Set-Content -Path $DoneFlag -Encoding ASCII -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    Get-ChildItem -Path $LogDir -Filter "done_*.flag" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-10) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 6. 关机
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

[void](Invoke-ShutdownDecision "推送成功")
Say "===================== 结束 ====================="
exit 0

<#
  改完先做这三步自查（都不会关机）：

  1) 语法过一遍（**任何改动之后都先跑这条**，比等明早发现崩了便宜得多）：

      $e = $null
      [void][System.Management.Automation.Language.Parser]::ParseFile(
          (Join-Path $PWD 'daily_run.ps1'), [ref]$null, [ref]$e)
      $e        # 什么都不打印 = 语法没问题

  2) 确认本文件确实带 BOM（应打印 True）：

      $p = Join-Path $PWD 'daily_run.ps1'
      $b = [IO.File]::ReadAllBytes($p)[0..2]
      ($b[0] -eq 0xEF) -and ($b[1] -eq 0xBB) -and ($b[2] -eq 0xBF)

     哪天编辑器把 BOM 弄丢了（症状：又报「缺少 )」），一行加回来：

      $t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
      [IO.File]::WriteAllText($p, $t, [Text.UTF8Encoding]::new($true))

  3) 在用检测 + 开机判据的五条（都不会关机）：

      # a. 你正坐在机器前跑 → 应打印 有人=True，开机判据多半是 user 或 rtc
      powershell -ExecutionPolicy Bypass -File .\daily_run.ps1 -CheckOnly

      # b. 锁屏后**从任务计划**跑同样的命令 → 应打印 有人=False、已登录=True、锁屏=True
      #    （直接在 PowerShell 窗口里跑是会话 1，测不出会话 0 的行为）
      #    v4 关注点：已登录=True 时不该走 /f 那一档

      # c. 挂起 4 小时再检测 → 应打印 有人=True、挂起=True
      powershell -ExecutionPolicy Bypass -File .\daily_run.ps1 -Hold 4
      powershell -ExecutionPolicy Bypass -File .\daily_run.ps1 -CheckOnly
      powershell -ExecutionPolicy Bypass -File .\daily_run.ps1 -Hold 0

      # d. 工作日、当天还没跑过时的 -ShutdownOnly → 应打印「不关机，把机器留给主任务」
      powershell -ExecutionPolicy Bypass -File .\daily_run.ps1 -ShutdownOnly

      # e. 【v4 必验】你自己开机的那天下午跑 -CheckOnly → 开机判据应当是 user。
      #    如果这时它报 rtc，说明 -RtcBootBefore 设得太晚（默认 11:00），
      #    或者这台机器的 LastBootUpTime / 唤醒事件读不到（会退回挂钟规则）。
#>
