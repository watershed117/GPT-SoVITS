<#
.SYNOPSIS
  一次性获取 Google Drive 的 OAuth 刷新令牌（rclone 客户端），生成可复用的 rclone.conf。

.DESCRIPTION
  背景：Colab 的 `from google.colab import drive; drive.mount()` 依赖前端（浏览器会话）投递
  凭据，纯 SSH 会话里 `TBE_EPHEM_CREDS_ADDR` 不存在，必然 KeyError（见 README §1）。
  因此绕开 Colab，用 rclone 自己的 OAuth：在本机（有浏览器）授权一次，把 refresh_token
  写进 rclone.conf，之后任何 Colab/Kaggle 会话都能直接用，无需浏览器。

  本脚本幂等：rclone 已存在则复用；rclone.conf 已存在会先备份。

.PARAMETER RemoteName
  rclone.conf 里的 remote 名，默认 gdrive。

.PARAMETER ClientId / ClientSecret
  自带 OAuth 客户端（可选）。用 rclone 内置客户端时不用给。若遇到
  "rate limit exceeded"/"quota exceeded" 或 Google 屏蔽共享客户端，就在自己的 GCP 项目里
  启用 Drive API + 建 OAuth「桌面应用」客户端，再用这两个参数重跑。

.PARAMETER OutDir
  产物目录，默认本脚本所在目录。

.EXAMPLE
  pwsh -File .kaggle_stage\colab_drive\mint_token.ps1
  pwsh -File .kaggle_stage\colab_drive\mint_token.ps1 -ClientId xxx.apps.googleusercontent.com -ClientSecret yyy
#>
[CmdletBinding()]
param(
  [string]$RemoteName = 'gdrive',
  [string]$ClientId = '',
  [string]$ClientSecret = '',
  [string]$OutDir = '',
  [switch]$NoBrowser
)

# 注意：[CmdletBinding()] 下 param 默认值里的 $PSScriptRoot 可能是空的（PS 5.1 实测踩到），
# 所以路径在脚本体里显式兜底解析。
if (-not $OutDir) { $OutDir = $PSScriptRoot }
if (-not $OutDir) { $OutDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $OutDir) { $OutDir = (Get-Location).Path }
$OutDir = (Resolve-Path -Path $OutDir).Path

$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "[mint] $m" }
function Die($m) { Write-Host "[mint][FAIL] $m" -ForegroundColor Red; exit 1 }

Info "工作目录: $OutDir"

# ---------- 0. 代理（本机走 127.0.0.1:7890 之类的本地代理才能到 Google） ----------
# rclone 是 Go 程序，只认 HTTP(S)_PROXY 环境变量，不读 Windows 的 WinINET 设置。
# 从注册表读出系统代理并显式设进环境，否则 authorize 的换 token 步骤会卡住/超时。
function Get-SystemProxy {
  try {
    $s = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
    if ($s.ProxyEnable -eq 1 -and $s.ProxyServer) {
      $p = $s.ProxyServer
      if ($p -notmatch '^https?://') { $p = "http://$p" }
      return $p
    }
  } catch { }
  return $null
}

$sysProxy = Get-SystemProxy
if ($sysProxy) {
  if (-not $env:HTTPS_PROXY) { $env:HTTPS_PROXY = $sysProxy }
  if (-not $env:HTTP_PROXY)  { $env:HTTP_PROXY  = $sysProxy }
  if (-not $env:NO_PROXY)    { $env:NO_PROXY    = '127.0.0.1,localhost,::1' }
  Info "代理: $($env:HTTPS_PROXY)（NO_PROXY=$($env:NO_PROXY)）"
} else {
  Info "未检测到系统代理（直连）"
}

# ---------- 1. 找/装 rclone（只放本目录 bin/，不污染系统） ----------
$binDir = Join-Path $OutDir 'bin'
$exe = $null

$onPath = Get-Command rclone -ErrorAction SilentlyContinue
if ($onPath) { $exe = $onPath.Source; Info "使用 PATH 上的 rclone: $exe" }

if (-not $exe) {
  $cand = Join-Path $binDir 'rclone.exe'
  if (Test-Path $cand) { $exe = $cand; Info "使用已下载的 rclone: $exe" }
}

if (-not $exe) {
  New-Item -ItemType Directory -Force -Path $binDir | Out-Null
  $zip = Join-Path $binDir 'rclone-windows-amd64.zip'
  # downloads.rclone.org 在国内常被挡（实测 403）；GitHub release 走代理可用
  $cands = @()
  if ($env:HTTPS_PROXY) { $cands += 'https://github.com/rclone/rclone/releases/latest/download/rclone-current-windows-amd64.zip' }
  $cands += 'https://downloads.rclone.org/rclone-current-windows-amd64.zip'
  $cands += 'https://github.com/rclone/rclone/releases/latest/download/rclone-current-windows-amd64.zip'

  $ok = $false
  foreach ($url in $cands) {
    Info "下载 rclone: $url"
    try {
      $old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
      $iwr = @{ Uri = $url; OutFile = $zip; UseBasicParsing = $true; TimeoutSec = 900 }
      if ($env:HTTPS_PROXY) { $iwr['Proxy'] = $env:HTTPS_PROXY }
      Invoke-WebRequest @iwr
      $ProgressPreference = $old
      $ok = $true; break
    } catch {
      Info "  失败：$($_.Exception.Message)"
      $ProgressPreference = 'Continue'
    }
  }
  if (-not $ok) { Die "三个源都下载失败；可手动放 rclone.exe 到 $binDir 后重跑" }

  Info "解包 → $binDir"
  Expand-Archive -Path $zip -DestinationPath $binDir -Force
  $found = Get-ChildItem -Path $binDir -Recurse -Filter 'rclone.exe' -ErrorAction SilentlyContinue |
           Select-Object -First 1
  if (-not $found) { Die "解包里没有 rclone.exe" }
  $exe = $found.FullName
  Remove-Item $zip -Force -ErrorAction SilentlyContinue
  Info "rclone 就位: $exe"
}

$ver = (& $exe version 2>&1 | Select-Object -First 1)
Info "版本: $ver"

# ---------- 2. 跑授权（本机浏览器点「允许」） ----------
$authCfg = ''
if ($ClientId) {
  if (-not $ClientSecret) { Die "给了 -ClientId 就必须给 -ClientSecret" }
  $authCfg = (@{ client_id = $ClientId; client_secret = $ClientSecret } | ConvertTo-Json -Compress)
  Info "使用自带 OAuth 客户端: $ClientId"
} else {
  Info "使用 rclone 内置 OAuth 客户端"
}

$authArgs = @('authorize', 'drive')
if ($authCfg) { $authArgs += $authCfg }
if ($NoBrowser) { $authArgs += '--auth-no-open-browser' }

$log = Join-Path $OutDir 'authorize.out.txt'
Info "启动授权流程（会打印一个 accounts.google.com 链接，复制到浏览器完成，然后回来等它跑完）..."

# PS 5.1 陷阱：$ErrorActionPreference='Stop' 时原生命令往 stderr 写一行就抛终止错误
# （rclone 启动时必打 "NOTICE: Config file not found"）→ 这里改成 Continue，
# 让 stderr 正常流过流水线（Tee 落盘 + 屏幕上直接能看到授权 URL）。
# 另外：受限/沙箱环境里 Start-Process 会被拒（实测 Access is denied），原生重定向最稳。
$ErrorActionPreference = 'Continue'
$authArgs += '--auth-no-open-browser'   # 自动开浏览器在部分环境被拒；统一手动点 URL
$authArgs = $authArgs | Select-Object -Unique

& $exe @authArgs 2>&1 | Tee-Object -FilePath $log
Info "rclone authorize 退出码: $LASTEXITCODE"

$raw = Get-Content $log -Raw -ErrorAction SilentlyContinue
if (-not $raw -or -not $raw.Trim()) { Die "authorize 没有任何输出（看 $log）" }

# ---------- 3. 解析 token JSON ----------
$tokenJson = $null
foreach ($line in ($raw -split "`r?`n")) {
  $t = $line.Trim()
  if ($t.StartsWith('{') -and $t.Contains('refresh_token')) {
    try { $null = $t | ConvertFrom-Json; $tokenJson = $t } catch { }
  }
}
if (-not $tokenJson) {
  $m = [regex]::Match($raw, '\{[^{}]*"refresh_token"[^{}]*\}')
  if ($m.Success) { $tokenJson = $m.Value }
}
if (-not $tokenJson) { Die "没解析到含 refresh_token 的 JSON，看 $log 确认授权是否完成" }

$tok = $tokenJson | ConvertFrom-Json
Info "拿到令牌：refresh_token 长度 $($tok.refresh_token.Length)，expiry $($tok.expiry)"

# ---------- 4. 写 rclone.conf ----------
$conf = Join-Path $OutDir 'rclone.conf'
if (Test-Path $conf) {
  $bak = "$conf.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
  Copy-Item $conf $bak -Force
  Info "旧配置已备份: $bak"
}

$scope = if ($ClientId) { 'drive' } else { 'drive' }
$lines = @(
  "[$RemoteName]",
  "type = drive",
  "scope = $scope",
  "token = $tokenJson"
)
if ($ClientId) {
  $lines += "client_id = $ClientId"
  $lines += "client_secret = $ClientSecret"
}
$lines += "team_drive ="
($lines -join "`n") + "`n" | Set-Content -Path $conf -Encoding UTF8 -NoNewline
Info "写入 $conf"

# ---------- 5. 自检 + 导出给远端用 ----------
# --log-level ERROR：压掉 rclone 的 NOTICE（含"共享 client_id 将被退役"提醒）。
# PS 5.1 会把原生命令的 stderr 记成错误，脚本末尾显式 exit 0 避免假失败退出码。
Info "本机连通性自检 ..."
& $exe --config $conf lsd "${RemoteName}:" --max-depth 1 --log-level ERROR 2>&1 | Select-Object -First 12
Info "自检退出码: $LASTEXITCODE（0 = 读到目录列表）"

$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($conf))
$b64Path = Join-Path $OutDir 'rclone.conf.b64'
[IO.File]::WriteAllText($b64Path, $b64)

Write-Host ''
Info "完成。产物："
Write-Host "  $conf"
Write-Host "  $b64Path  (base64，用于贴给 Colab VM)"
Write-Host ''
Info "下一步（VM 侧）："
Write-Host "  B64=`$(cat rclone.conf.b64)"
Write-Host "  bash colab_drive.sh config `"`$B64`""
Write-Host "  bash colab_drive.sh mount"
exit 0
