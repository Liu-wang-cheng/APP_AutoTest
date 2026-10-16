# 一键注册 self-hosted runner(方案 C2, 2026-10-16)
#
# 为什么要有这个脚本: GitHub 页面上给的注册步骤散成好几段(下载/校验/解压/config),
# 而真机冒烟只需要"这台连着模拟器的机器"能被仓库派活。这里把它压成一条命令。
#
# 用法(在**要当 runner 的机器**上, 普通权限即可):
#   1) 网页打开: 仓库 → Settings → Actions → Runners → New self-hosted runner
#      (只是为了看当前版本号; token 用下面 -Token 传)
#   2) 或者直接让 Claude 用 API 生成一个注册 token, 粘进来:
#   .\tools\setup_runner.ps1 -Token <注册TOKEN> [-Name smoke-pc] [-Start]
#
# 参数:
#   -Token   注册 token(GitHub 一次性, 1 小时有效)
#   -Name    runner 名字(默认取机器名 + "-smoke")
#   -Dir     安装目录(默认 C:\actions-runner)
#   -Version runner 版本(默认 2.338.0; 页面上会写当前版本)
#   -Start   配置完立刻前台启动(run.cmd)。不加就只配置好, 你自己再跑 run.cmd
#
# ★ 两个坑(脚本里已处理/提醒):
#   ① 服务化(svc.cmd install)默认跑在 SYSTEM 账户 —— 那会和你的登录会话**不是同一个 adb**,
#      表现为"runner 里 adb devices 看不到设备"。所以默认只做**前台运行**; 真要装服务,
#      用 svc.cmd install --account "<机器名>\<你的用户名>"(会让你输密码)。
#   ② 那台机器要**保持登录** —— 模拟器跑在桌面会话里, 注销/重启就没了。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$Name = "$env:COMPUTERNAME-smoke",
    [string]$Dir = "C:\actions-runner",
    [string]$Version = "2.338.0",
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$repo = "https://github.com/Liu-wang-cheng/APP_AutoTest"
$zip = "actions-runner-win-x64-$Version.zip"
$url = "https://github.com/actions/runner/releases/download/v$Version/$zip"

Write-Host "[1/4] 准备目录: $Dir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Set-Location $Dir

if (-not (Test-Path (Join-Path $Dir "config.cmd"))) {
    Write-Host "[2/4] 下载 runner v$Version ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Write-Host "      解压..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory((Join-Path $Dir $zip), $Dir)
} else {
    Write-Host "[2/4] 目录里已有 runner, 跳过下载" -ForegroundColor Yellow
}

Write-Host "[3/4] 注册到 $repo (名字=$Name, 标签=self-hosted,Windows,X64,realtest,emulator)" -ForegroundColor Cyan
# --unattended: 不交互; --replace: 同名就替换(重装/换 token 时不用手工清理)
& (Join-Path $Dir "config.cmd") --url $repo --token $Token `
    --name $Name --labels "realtest,emulator" --work "_work" --unattended --replace

Write-Host ""
Write-Host "[4/4] 注册完成 ✓" -ForegroundColor Green
Write-Host "  去仓库 Settings → Actions → Runners 应能看到 $Name(Idle)"
Write-Host "  启动:" -NoNewline; Write-Host "  $Dir\run.cmd" -ForegroundColor Yellow -NoNewline
Write-Host "  (前台跑; 关窗口就停。装服务见脚本头部注释的坑①)"
if ($Start) {
    Write-Host "  现在前台启动(按 Ctrl+C 停)..." -ForegroundColor Cyan
    & (Join-Path $Dir "run.cmd")
}
