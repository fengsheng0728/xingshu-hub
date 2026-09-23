#Requires -Version 5.1
<#
.SYNOPSIS
  星枢 Sync Hub — Windows 任务计划（Task Scheduler）服务化安装/卸载脚本（CD-082）。

.DESCRIPTION
  注册一个「开机自启 + 失败自动重启」的计划任务，替代双击 启动 Hub.bat 的人工拉起：
    - 触发器: AtStartup（开机即启动，无需登录）
    - 失败重启: RestartCount/RestartInterval（进程非零退出 → 按频率重拉，自愈）
    - 运行身份: SYSTEM（服务账号，免密码；需管理员权限注册）
  幂等: 同名任务已存在则先注销再注册（覆盖更新），不产生重复任务。
  本脚本不修改防火墙/注册表/UAC，不删除任何其它既有任务。

.PARAMETER Mode
  启动目标: source = 源码运行(python main.py) | exe = 打包形态(dist\星枢 Hub.exe)。

.PARAMETER ConfigDir
  Hub 配置目录（写入 SYNC_HUB_CONFIG_DIR）。缺省 = <WorkDir>\config。

.PARAMETER WorkDir
  仓库根目录（工作目录/相对路径基准）。缺省 = 本脚本所在仓库根（scripts\..）。

.PARAMETER TaskName
  计划任务名。缺省 XingshuSyncHub。

.PARAMETER PythonExe
  Mode=source 时的 python 命令名或可执行文件路径。缺省 python（安装时解析为全路径）。

.PARAMETER Uninstall
  卸载: 注销同名任务后退出。

.EXAMPLE
  # 管理员 PowerShell 中（exe 形态）:
  powershell -ExecutionPolicy Bypass -File scripts\install_service_windows.ps1 -Mode exe
.EXAMPLE
  # 源码形态 + 自定义配置目录:
  powershell -ExecutionPolicy Bypass -File scripts\install_service_windows.ps1 -Mode source -ConfigDir E:\sync-hub-case\config
.EXAMPLE
  # 卸载:
  powershell -ExecutionPolicy Bypass -File scripts\install_service_windows.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet('source', 'exe')] [string]$Mode = 'source',
    [string]$ConfigDir = '',
    [string]$WorkDir = '',
    [string]$TaskName = 'XingshuSyncHub',
    [string]$PythonExe = 'python',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

function Write-Err([string]$msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red }
function Write-Ok([string]$msg)  { Write-Host "[ OK ] $msg" -ForegroundColor Green }
function Write-Info([string]$msg){ Write-Host "[INFO] $msg" }

# ---------- 0. 权限诚实: 系统级(AtStartup/SYSTEM)任务必须管理员 ----------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $cmdLine = "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Mode $Mode"
    if ($ConfigDir) { $cmdLine += " -ConfigDir `"$ConfigDir`"" }
    Write-Err "需要管理员权限才能注册开机自启(AtStartup)的系统级计划任务。"
    Write-Err "修复: 右键开始菜单 -> Windows PowerShell(管理员) / 终端(管理员)，然后执行:"
    Write-Err "  $cmdLine"
    exit 1
}

# ---------- 1. 参数规整 ----------
if (-not $WorkDir) {
    $WorkDir = Split-Path -Parent $PSScriptRoot   # scripts\.. = 仓库根
}
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path
if (-not $ConfigDir) { $ConfigDir = Join-Path $WorkDir 'config' }
$ConfigDir = [System.IO.Path]::GetFullPath($ConfigDir)

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Info "任务 '$TaskName' 不存在，无需卸载。"
        exit 0
    }
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "已卸载任务 '$TaskName'。"
        exit 0
    } catch {
        Write-Err "注销任务失败: $($_.Exception.Message)"
        exit 1
    }
}

# ---------- 2. 启动目标校验 ----------
$logDir = Join-Path $WorkDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir 'hub-service.log'

switch ($Mode) {
    'source' {
        $py = Get-Command $PythonExe -ErrorAction SilentlyContinue
        if (-not $py) {
            Write-Err "找不到 python（-PythonExe '$PythonExe'）。修复: 安装 Python 并加入 PATH，或用 -PythonExe 指定全路径。"
            exit 1
        }
        $targetExe = $py.Source
        if (-not (Test-Path (Join-Path $WorkDir 'main.py'))) {
            Write-Err "WorkDir 下没有 main.py: $WorkDir"
            exit 1
        }
        $invoke = "& '$($py.Source)' 'main.py'"
    }
    'exe' {
        $targetExe = Join-Path $WorkDir 'dist\星枢 Hub.exe'
        if (-not (Test-Path -LiteralPath $targetExe)) {
            Write-Err "找不到打包产物: $targetExe。修复: 先构建（PyInstaller / sync_hub.spec），或用 -Mode source。"
            exit 1
        }
        $invoke = "& '$targetExe'"
    }
}

# ---------- 3. 构造任务 ----------
# 统一走一层 powershell 包装: 设 SYNC_HUB_CONFIG_DIR、落日志、把子进程退出码
# 透传为任务退出码（失败重启语义靠「任务失败」检测，必须非零退出才触发）。
$inner = "`$env:SYNC_HUB_CONFIG_DIR = '$ConfigDir'; $invoke *>> '$logFile'; exit `$LASTEXITCODE"
$actionArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + $inner + '"'

$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
                -Argument $actionArgs -WorkingDirectory $WorkDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# 0 = 不限制运行时长（服务常驻）；反引号续行必须是行尾最后一个字符
$settings  = New-ScheduledTaskSettingsSet `
                -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -StartWhenAvailable `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# ---------- 4. 幂等注册（同名先注销再注册 = 覆盖更新） ----------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Info "任务 '$TaskName' 已存在，覆盖更新。"
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    catch {
        Write-Err "覆盖前注销旧任务失败: $($_.Exception.Message)"
        exit 1
    }
}

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "星枢 Sync Hub（CD-082 服务化: 开机自启 + 失败重启, Mode=$Mode）" | Out-Null
} catch {
    Write-Err "注册任务失败: $($_.Exception.Message)"
    Write-Err "修复: 确认以管理员身份运行本脚本（见开头指引）。"
    exit 1
}

# ---------- 5. 回读验证 ----------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = $task.Actions | ForEach-Object { "$($_.Execute) $($_.Argument)" }
Write-Ok "任务已注册并回读确认:"
Write-Host "  TaskName : $($task.TaskName)"
Write-Host "  State    : $($task.State)"
Write-Host "  Triggers : $(($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', ')"
Write-Host "  Actions  : $info"
Write-Host "  Settings : RestartCount=$($task.Settings.RestartCount) RestartInterval=$($task.Settings.RestartInterval) ExecutionTimeLimit=$($task.Settings.ExecutionTimeLimit)"
Write-Host "  Log      : $logFile"
Write-Info "卸载: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
exit 0
