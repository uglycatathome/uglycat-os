# UglyCat OS 安装脚本（Windows PowerShell 5.1+）——幂等、用户级、无需管理员。
#
#   .\install\setup.ps1 -CheckOnly   只体检，不改动系统
#   .\install\setup.ps1 -DryRun      打印将执行的命令
#   .\install\setup.ps1              正式安装
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Say([string]$msg) { Write-Host $msg }
function Run([string]$exe, [string[]]$argv) {
    if ($DryRun) { Say "[dry-run] $exe $($argv -join ' ')" ; return }
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe 退出码 $LASTEXITCODE" }
}

# ── 1. 找 Python >= 3.11 ────────────────────────────────────────────────
$Py = $null
$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @('3.13', '3.12', '3.11')) { $candidates += @{ Exe = 'py'; Args = @("-$v") } }
}
foreach ($c in @('python', 'python3')) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $candidates += @{ Exe = $c; Args = @() } }
}

foreach ($cand in $candidates) {
    try {
        # 注意：原生命令的双引号会被 PowerShell 吞掉，这里的 Python 片段不能含引号
        $ver = & $cand.Exe @($cand.Args + @('-c', 'import sys;print(sys.version.split()[0])')) 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $ok = & $cand.Exe @($cand.Args + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)')) 2>$null
            if ($LASTEXITCODE -eq 0) {
                $Py = @{ Exe = $cand.Exe; Args = $cand.Args; Version = ($ver | Select-Object -First 1) }
                break
            }
        }
    } catch { }
}

if (-not $Py -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "未发现本机 Python，尝试用 uv 安装 3.11 ..."
    & uv python install 3.11
    if ($LASTEXITCODE -eq 0) { $Py = @{ Exe = 'uv'; Args = @('run', '--python', '3.11'); Version = '3.11 (uv)' } }
}

if (-not $Py) {
    Say "错误：未找到 Python >= 3.11。请先安装：https://www.python.org/downloads/"
    Say "        或安装 uv（https://docs.astral.sh/uv/）后重跑本脚本。"
    exit 1
}
Say "Python：$($Py.Exe) $($Py.Args -join ' ')（$($Py.Version)）"
Say "目录：  $Root"

$HasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
Say "包管理器：$(if ($HasUv) { 'uv' } else { 'pip（未发现 uv，将用 venv + pip）' })"

if ($CheckOnly) { Say "体检完成（未安装）"; exit 0 }

# ── 2. 建虚拟环境 + 安装依赖 ────────────────────────────────────────────
if ($HasUv) {
    Run 'uv' @('venv', '.venv')
    Run 'uv' @('pip', 'install', '--python', '.venv\Scripts\python.exe', '-e', '.[dev]')
} else {
    Run $Py.Exe @($Py.Args + @('-m', 'venv', '.venv'))
    Run '.venv\Scripts\python.exe' @('-m', 'pip', 'install', '-U', 'pip')
    Run '.venv\Scripts\python.exe' @('-m', 'pip', 'install', '-e', '.[dev]')
}

# ── 3. 初始化 .env / 目录 / 启动器 ──────────────────────────────────────
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Say "已生成 .env —— 请填入任意一个 LLM Key（推荐 ZHIPU_API_KEY）"
}
New-Item -ItemType Directory -Force -Path 'data', 'logs' | Out-Null

if (-not $DryRun) {
    $launcher = @'
@echo off
set ROOT=%~dp0
cd /d "%ROOT%"
"%ROOT%.venv\Scripts\python.exe" "%ROOT%run.py" %*
'@
    Set-Content -Path 'uglycat.cmd' -Value $launcher -Encoding ASCII
    Say "已生成启动器：uglycat.cmd"
}

Say ""
Say "下一步："
Say "  1) 编辑 .env 填入 LLM Key"
Say "  2) .\uglycat.cmd doctor --json     # 判据 ok=true"
Say "  3) .\uglycat.cmd news probe        # LLM 连通性"
Say "  4) .\uglycat.cmd install-schedule --time 08:00   # 可选：每天自动跑"
