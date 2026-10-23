<#
.SYNOPSIS
  在本机开一条到 Colab 会话的 SSH 隧道，把 VM 上的监控页映射到本地端口（浏览器直接打开）。

.DESCRIPTION
  原理：colab-cli 的 `ssh --proxy-mode` 就是标准 OpenSSH ProxyCommand（WebSocket 桥），
  所以本地端口转发（-L）由 ssh 客户端自己实现，不依赖 Colab 前端。
  ⚠ Colab 官方的 `google.colab.kernel.proxyPort()` / `eval_js` 走的是前端 JS，
    纯 SSH 会话里会永久阻塞（实测），所以隧道是这条路线的唯一可行方式。

  这个脚本会生成一份 ssh_config（写在工作区，不碰 ~/.ssh）并前台起隧道；
  窗口关掉隧道就断。要后台常驻，用 -Background（走 Start-Job）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Session monitordemo -LocalPort 8010
  powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Session work -LocalPort 8010 -RemotePort 8000
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Session,
  [int]$LocalPort = 8010,
  [int]$RemotePort = 8000,
  [string]$Python = '',
  [string]$Identity = "$env:USERPROFILE\.ssh\id_ed25519",
  [switch]$Background
)

$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }

if (-not $Python) {
  # 默认用本仓库 venv 里的 python（colab-cli 装在那里）
  $cand = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) '.venv\Scripts\python.exe'
  if (Test-Path $cand) { $Python = $cand } else { $Python = 'python' }
}
$pyFwd = $Python.Replace('\', '/')
$cfg = Join-Path $here "ssh_config.$Session"
$kh = Join-Path $here '_known_hosts'
$idFwd = $Identity.Replace('\', '/')

@"
# 由 tunnel.ps1 生成（Session=$Session）——不要手改，改 tunnel.ps1
Host colabmon
  User root
  HostName colab-runtime
  IdentityFile $idFwd
  StrictHostKeyChecking no
  UserKnownHostsFile $($kh.Replace('\','/'))
  LogLevel ERROR
  ServerAliveInterval 30
  ServerAliveCountMax 3
  ProxyCommand $pyFwd -m colab_cli.cli ssh --proxy-mode -s $Session
"@ | Set-Content -Path $cfg -Encoding ASCII

Write-Host "[tunnel] config: $cfg"
Write-Host "[tunnel] 本地 http://127.0.0.1:$LocalPort/  ->  VM 127.0.0.1:$RemotePort"
Write-Host "[tunnel] Ctrl-C 断开（浏览器就不再能访问）"

$sshArgs = @('-F', $cfg, '-o', 'ExitOnForwardFailure=yes', '-N',
             '-L', "${LocalPort}:127.0.0.1:${RemotePort}", 'colabmon')

if ($Background) {
  $job = Start-Job -ScriptBlock {
    param($a) & ssh.exe @a
  } -ArgumentList (, $sshArgs)
  Start-Sleep -Seconds 4
  Write-Host "[tunnel] 后台 job: $($job.Id)  状态: $($job.State)"
  Write-Host "[tunnel] 自检:"
  & curl.exe -s -m 10 -o NUL -w "  health=%{http_code}`n" "http://127.0.0.1:$LocalPort/health"
  Write-Host "[tunnel] 停止: Stop-Job -Id $($job.Id); Remove-Job -Id $($job.Id)"
} else {
  & ssh.exe @sshArgs
  Write-Host "[tunnel] ssh 退出码 $LASTEXITCODE"
}
